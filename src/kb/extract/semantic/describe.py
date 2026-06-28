"""LLM-grounded NL descriptions over a snapshot — a separate, key-gated pass (DESIGN.md §4, §9).

For each ``api_route`` / ``entity`` artifact, each first-party module (file), AND each first-party
package, an LLM writes a short summary plus structured claims; each claim is validated against the
target's own grounding spans (``grounding.validate_claims``), unvalidated claims are dropped, and —
if anything survives — a ``description`` artifact is stored grounded on the SAME spans (role
``describes``, ``is_deterministic=False``). Modules are grounded on ALL of the file's spans; a
package overview is grounded on its own and its direct-child modules' spans and synthesizes richer
*context* (import edges + public surface + member-module summaries) while its claims stay
code-grounded. Never on the ``kb index`` path. Idempotent per (model, prompt): ``artifact_id`` folds
in model_id + prompt.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, Engine, select

from kb.extract.base import DerivedEdge, ExtractedArtifact
from kb.extract.semantic.grounding import validate_claims
from kb.llm.providers import LLMProvider
from kb.store import models as m
from kb.store.queries import (
    ArtifactSpanRow,
    PackageTarget,
    module_targets,
    package_targets,
    spans_for_artifact,
)
from kb.store.writer import write_grounded_artifact, write_snapshot_entry

EXTRACTOR_ID = "llm_describe"
EXTRACTOR_VERSION = "1"
PROMPT_VERSION = "1"
DESCRIBE_KINDS = ("api_route", "entity")
_BODY_CAP = 6000  # prompt source-span body cap (validation still runs over every span)
_PACKAGE_FACTS_CAP = 4000  # larger facts budget for rich package overviews (context only)
_FACT_LIST_CAP = 40  # cap each import/surface fact list (context only; validation runs over spans)

_SYSTEM = (
    "You describe a code artifact using ONLY the provided source spans. Respond with STRICT JSON "
    'and nothing else: {"summary": "<= 2 sentences", "claims": [{"text": "...", "symbol": '
    '"<one identifier that appears verbatim in the code>"}]}. Every claim must cite a real '
    "identifier from the code (a function, class, field, or parameter name); never invent names."
)


@dataclass(frozen=True)
class DescribeResult:
    sha: str
    described: int
    dropped_claims: int


def describe_snapshot(engine: Engine, sha: str, provider: LLMProvider) -> DescribeResult:
    """Generate grounded descriptions for the snapshot's api_route/entity artifacts and modules."""
    join = m.snapshot_entry.join(
        m.artifact, m.artifact.c.artifact_id == m.snapshot_entry.c.artifact_id
    )
    described = 0
    dropped_total = 0
    with engine.begin() as conn:
        targets = conn.execute(
            select(m.artifact.c.logical_key, m.artifact.c.kind, m.artifact.c.payload)
            .select_from(join)
            .where(
                m.snapshot_entry.c.sha == sha,
                m.artifact.c.kind.in_(list(DESCRIBE_KINDS)),
            )
            .order_by(m.artifact.c.logical_key)
        ).all()
        for target in targets:
            spans = spans_for_artifact(conn, sha, target.logical_key)
            if not spans:
                continue
            stored, dropped = _describe_one(
                conn,
                sha,
                provider,
                logical_key=f"desc:{target.logical_key}",
                target_logical_key=target.logical_key,
                target_kind=target.kind,
                facts=target.payload,
                spans=spans,
            )
            described += int(stored)
            dropped_total += dropped

        for module in module_targets(conn, sha):
            stored, dropped = _describe_one(
                conn,
                sha,
                provider,
                logical_key=f"desc:module:{module.module}",
                target_logical_key=module.module,
                target_kind="module",
                facts={"module": module.module, "file_path": module.file_path},
                spans=module.spans,
            )
            described += int(stored)
            dropped_total += dropped

        # Package overviews run LAST so they can synthesize the module descriptions written above
        # (visible on this same transaction's connection).
        for package in package_targets(conn, sha):
            if not package.spans:
                continue
            stored, dropped = _describe_one(
                conn,
                sha,
                provider,
                logical_key=f"desc:package:{package.package}",
                target_logical_key=package.package,
                target_kind="package",
                facts=_package_facts(conn, sha, package),
                spans=package.spans,
                facts_cap=_PACKAGE_FACTS_CAP,
            )
            described += int(stored)
            dropped_total += dropped
    return DescribeResult(sha=sha, described=described, dropped_claims=dropped_total)


def _describe_one(
    conn: Connection,
    sha: str,
    provider: LLMProvider,
    *,
    logical_key: str,
    target_logical_key: str,
    target_kind: str,
    facts: dict[str, Any],
    spans: list[ArtifactSpanRow],
    facts_cap: int = 800,
) -> tuple[bool, int]:
    """Describe one target (artifact / module / package) from its grounding spans.

    Returns ``(stored, dropped_count)``. A ``description`` artifact is stored (grounded on the
    spans, role ``describes``) only if >= 1 claim survives span-validation; otherwise nothing is
    stored (anti-hallucination). Idempotent per (model, prompt).
    """
    prompt = _build_prompt(target_kind, facts, spans, facts_cap=facts_cap)
    data = _parse_json(provider.complete(_SYSTEM, prompt, max_tokens=600))
    if data is None:
        return False, 0
    raw_claims = [c for c in data.get("claims", []) if isinstance(c, dict)]
    kept, dropped = validate_claims(
        raw_claims, [s.raw_text for s in spans], [s.fq_symbol_path for s in spans]
    )
    if not kept:
        return False, len(dropped)  # nothing grounded survives -> store nothing
    artifact = ExtractedArtifact(
        kind="description",
        logical_key=logical_key,
        payload={
            "target_logical_key": target_logical_key,
            "target_kind": target_kind,
            "summary": str(data.get("summary", ""))[:500],
            "claims": kept,
            "dropped_claims": len(dropped),
        },
        derived_from=[DerivedEdge(s.span_id, "describes") for s in spans],
        extractor_id=EXTRACTOR_ID,
        extractor_version=EXTRACTOR_VERSION,
        prompt_version=PROMPT_VERSION,
        model_id=provider.model_id,
        is_deterministic=False,
        confidence=len(kept) / (len(kept) + len(dropped)),
    )
    artifact_id = write_grounded_artifact(conn, artifact)
    write_snapshot_entry(conn, sha, artifact.logical_key, artifact_id)
    return True, len(dropped)


def _build_prompt(
    kind: str, facts: dict[str, Any], spans: list[ArtifactSpanRow], *, facts_cap: int = 800
) -> str:
    facts_json = json.dumps(facts, default=str)[:facts_cap]
    parts: list[str] = []
    used = 0
    for s in spans:
        block = f"# {s.fq_symbol_path}\n{s.raw_text}"
        if parts and used + len(block) > _BODY_CAP:
            break
        parts.append(block)
        used += len(block)
    body = "\n\n".join(parts)
    return f"Artifact kind: {kind}\nKnown facts: {facts_json}\n\nSource spans:\n{body}"


def _snapshot_artifacts(conn: Connection, sha: str, kinds: tuple[str, ...]) -> list[Any]:
    """(logical_key, kind, payload) rows for the snapshot's artifacts of the given kinds."""
    join = m.snapshot_entry.join(
        m.artifact, m.artifact.c.artifact_id == m.snapshot_entry.c.artifact_id
    )
    return list(
        conn.execute(
            select(m.artifact.c.logical_key, m.artifact.c.kind, m.artifact.c.payload)
            .select_from(join)
            .where(m.snapshot_entry.c.sha == sha, m.artifact.c.kind.in_(list(kinds)))
        ).all()
    )


def _package_facts(conn: Connection, sha: str, target: PackageTarget) -> dict[str, Any]:
    """Rich *context* for a package overview: import edges (internal/cross-package), public surface,
    and member-module summaries. Context only — claims still validate against the code spans."""
    members = set(target.member_modules)
    internal: list[str] = []
    outgoing: list[str] = []
    incoming: list[str] = []
    for row in _snapshot_artifacts(conn, sha, ("import_edge",)):
        importer, imported = row.payload.get("importer"), row.payload.get("imported")
        if importer is None or imported is None:
            continue
        imp_in, impd_in = importer in members, imported in members
        if imp_in and impd_in:
            internal.append(f"{importer}->{imported}")
        elif imp_in:
            outgoing.append(f"{importer}->{imported}")
        elif impd_in:
            incoming.append(f"{importer}->{imported}")
    surface: list[dict[str, Any]] = []
    for row in _snapshot_artifacts(conn, sha, ("public_symbol",)):
        payload = row.payload
        if payload.get("exporting_module") in members or payload.get("defining_module") in members:
            surface.append(
                {"name": payload.get("public_qualified_name"), "kind": payload.get("symbol_kind")}
            )
    member_summaries: dict[str, str] = {}
    for row in _snapshot_artifacts(conn, sha, ("description",)):
        payload = row.payload
        if payload.get("target_kind") == "module" and payload.get("target_logical_key") in members:
            key = str(payload.get("target_logical_key"))
            member_summaries[key] = str(payload.get("summary", ""))
    return {
        "package": target.package,
        "member_modules": sorted(members),
        "internal_imports": sorted(internal)[:_FACT_LIST_CAP],
        "outgoing_imports": sorted(outgoing)[:_FACT_LIST_CAP],
        "incoming_imports": sorted(incoming)[:_FACT_LIST_CAP],
        "public_surface": surface[:_FACT_LIST_CAP],
        "member_summaries": member_summaries,
    }


def _parse_json(raw: str) -> dict[str, Any] | None:
    match = re.search(r"\{.*\}", raw, re.S)
    if match is None:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None
