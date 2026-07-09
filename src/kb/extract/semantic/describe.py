"""LLM-grounded NL descriptions over a snapshot — a separate, key-gated pass (DESIGN.md §4, §9).

For each ``api_route`` / ``entity`` / ``process_path`` / ``event_handler`` artifact, each
first-party module (file), each first-party package, AND the repo as a whole, an LLM writes a
short summary plus structured claims; each claim is validated against the target's own grounding
spans (``grounding.validate_claims``), unvalidated claims are dropped, and — if anything
survives — a ``description`` artifact is stored grounded on the SAME spans (role ``describes``,
``is_deterministic=False``). Modules are grounded on ALL of the file's spans; a package overview
is grounded on its own and its direct-child modules' spans and synthesizes richer *context*
(import edges + public surface + member-module summaries) while its claims stay code-grounded; a
process-path label is grounded on every span along the materialized path; the whole-repo overview
is grounded on the bounded top-level surface (``queries.repo_target``) and — under its own system
prompt — synthesizes the package overviews written just before it. Confidence is Laplace add-one,
``kept / (kept + dropped + 1)``: the +1 prices unknown-unknowns, so an llm_grounded artifact stays
< 1.0 by construction (1.0 remains reserved for the deterministic layer). Never on the
``kb index`` path. Idempotent per (model, prompt): ``artifact_id`` folds in model_id + prompt.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, Engine, func, select

from kb.extract.base import DerivedEdge, ExtractedArtifact
from kb.extract.semantic.grounding import validate_claims
from kb.llm.providers import LLMProvider
from kb.store import models as m
from kb.store.queries import (
    ArtifactSpanRow,
    PackageTarget,
    RepoTarget,
    module_targets,
    package_targets,
    repo_target,
    spans_for_artifact,
)
from kb.store.writer import write_grounded_artifact, write_snapshot_entry

EXTRACTOR_ID = "llm_describe"
EXTRACTOR_VERSION = "2"  # v2: Laplace add-one confidence (kept/(kept+dropped+1))
PROMPT_VERSION = "1"  # unchanged kinds keep their exact prompts; the repo path carries its own
_REPO_PROMPT_VERSION = "2"  # v2: repo overview synthesizes facts (see _REPO_SYSTEM)
DESCRIBE_KINDS = ("api_route", "entity", "process_path", "event_handler")
_BODY_CAP = 6000  # prompt source-span body cap (validation still runs over every span)
_DEFAULT_FACTS_CAP = 800  # facts budget for route/entity prompts (kept byte-identical)
_PACKAGE_FACTS_CAP = 4000  # larger facts budget for rich package overviews (context only)
_PROCESS_FACTS_CAP = 4000  # larger facts budget for process payloads (steps/edges/sink context)
_EVENT_FACTS_CAP = 2000  # event payloads (registrations list) overflow the default 800 budget
_REPO_FACTS_CAP = 8000  # repo overview facts are dominated by up to 40 package summaries
_REPO_SPAN_CAP = 400  # per-span body slice for the repo prompt: no single top file may
# monopolize _BODY_CAP (a src-layout repo's top surface is mostly empty __init__ files plus a
# few large direct children — dogfooding showed one of them eating the whole budget)
_FACT_LIST_CAP = 40  # cap each import/surface fact list (context only; validation runs over spans)
_FACTS_CAPS = {"process_path": _PROCESS_FACTS_CAP, "event_handler": _EVENT_FACTS_CAP}

_SYSTEM = (
    "You describe a code artifact using ONLY the provided source spans. Respond with STRICT JSON "
    'and nothing else: {"summary": "<= 2 sentences", "claims": [{"text": "...", "symbol": '
    '"<one identifier that appears verbatim in the code>"}]}. Every claim must cite a real '
    "identifier from the code (a function, class, field, or parameter name); never invent names."
)

# The repo overview is the one target whose substance lives in the FACTS (the package/module
# summaries written just before it), not in its grounding spans (the mostly-empty top-level
# surface) — so its system prompt demands synthesis while claims still validate against spans.
# Module/package names ARE valid symbols: the validator accepts any component of a grounding
# span's fully-qualified path, and the top-level surface spans carry exactly those names.
_REPO_SYSTEM = (
    "You write a whole-repository architecture overview. Synthesize the provided facts - the "
    "package and module summaries, import edges, external dependencies, and artifact counts - "
    "into a bird's-eye view of what the repository does and how its parts fit together; the "
    "source spans are the repo's top-level surface, not its substance. Respond with STRICT "
    'JSON and nothing else: {"summary": "<= 2 sentences", "claims": [{"text": "...", '
    '"symbol": "<one identifier>"}]} with AT MOST 8 claims. Each claim\'s symbol must appear '
    "verbatim in the source spans OR be one of the module/package names listed in the facts "
    "(e.g. a top_packages / package_summaries key, or its last dotted segment); never invent "
    "names."
)
_REPO_MAX_TOKENS = 1000  # the synthesized overview + up to 8 claims overflow the default 600


@dataclass(frozen=True)
class DescribeResult:
    sha: str
    described: int
    dropped_claims: int


def describe_snapshot(engine: Engine, sha: str, provider: LLMProvider) -> DescribeResult:
    """Grounded descriptions: route/entity/process/event artifacts, modules, packages, the repo."""
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
                facts_cap=_FACTS_CAPS.get(target.kind, _DEFAULT_FACTS_CAP),
            )
            described += int(stored)
            dropped_total += dropped

        modules = module_targets(conn, sha)
        for module in modules:
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

        # Package overviews run AFTER modules so they can synthesize the module descriptions
        # written above (visible on this same transaction's connection).
        packages = package_targets(conn, sha)
        for package in packages:
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

        # The whole-repo overview runs LAST: it synthesizes the package overviews written above,
        # grounded on the bounded top-level surface (queries.repo_target).
        repo = repo_target(modules, packages)
        if repo.spans:
            stored, dropped = _describe_one(
                conn,
                sha,
                provider,
                logical_key="desc:repo",
                target_logical_key="repo",
                target_kind="repo",
                facts=_repo_facts(conn, sha, repo),
                spans=repo.spans,
                facts_cap=_REPO_FACTS_CAP,
                system=_REPO_SYSTEM,
                prompt_version=_REPO_PROMPT_VERSION,
                span_cap=_REPO_SPAN_CAP,
                max_tokens=_REPO_MAX_TOKENS,
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
    facts_cap: int = _DEFAULT_FACTS_CAP,
    system: str = _SYSTEM,
    prompt_version: str = PROMPT_VERSION,
    span_cap: int | None = None,
    max_tokens: int = 600,
) -> tuple[bool, int]:
    """Describe one target (artifact / module / package / repo) from its grounding spans.

    Returns ``(stored, dropped_count)``. A ``description`` artifact is stored (grounded on the
    spans, role ``describes``) only if >= 1 claim survives span-validation; otherwise nothing is
    stored (anti-hallucination). Idempotent per (model, prompt); the defaults keep every non-repo
    prompt byte-identical.
    """
    prompt = _build_prompt(target_kind, facts, spans, facts_cap=facts_cap, span_cap=span_cap)
    data = _parse_json(provider.complete(system, prompt, max_tokens=max_tokens))
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
        prompt_version=prompt_version,
        model_id=provider.model_id,
        is_deterministic=False,
        # Laplace add-one: the +1 prices unknown-unknowns, so llm_grounded never reaches 1.0
        confidence=len(kept) / (len(kept) + len(dropped) + 1),
    )
    artifact_id = write_grounded_artifact(conn, artifact)
    write_snapshot_entry(conn, sha, artifact.logical_key, artifact_id)
    return True, len(dropped)


def _build_prompt(
    kind: str,
    facts: dict[str, Any],
    spans: list[ArtifactSpanRow],
    *,
    facts_cap: int = _DEFAULT_FACTS_CAP,
    span_cap: int | None = None,
) -> str:
    """Pack facts + span bodies under the budget caps.

    ``span_cap`` slices each span's body so no single large file can monopolize ``_BODY_CAP``
    (the repo overview needs a fair sample of the top-level surface); ``None`` keeps the
    historical greedy packing byte-identical for every other kind.
    """
    facts_json = json.dumps(facts, default=str)[:facts_cap]
    parts: list[str] = []
    used = 0
    for s in spans:
        text = s.raw_text if span_cap is None else s.raw_text[:span_cap]
        block = f"# {s.fq_symbol_path}\n{text}"
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


def _repo_facts(conn: Connection, sha: str, target: RepoTarget) -> dict[str, Any]:
    """Rich *context* for the whole-repo overview: package/top-module summaries (written earlier
    in this same transaction), package-level import edges, external dependencies, and artifact
    counts. Context only — claims still validate against the repo's top-level code spans."""
    first_party_tops = set(target.top_packages) | set(target.top_modules)
    cross_package: set[str] = set()
    external: set[str] = set()
    for row in _snapshot_artifacts(conn, sha, ("import_edge",)):
        importer, imported = row.payload.get("importer"), row.payload.get("imported")
        if importer is None or imported is None:
            continue
        importer_top = str(importer).split(".", 1)[0]
        imported_top = str(imported).split(".", 1)[0]
        if importer_top not in first_party_tops:
            continue
        if imported_top in first_party_tops:
            if imported_top != importer_top:
                cross_package.add(f"{importer_top}->{imported_top}")
        else:
            external.add(imported_top)
    package_summaries: dict[str, str] = {}
    top_module_summaries: dict[str, str] = {}
    for row in _snapshot_artifacts(conn, sha, ("description",)):
        payload = row.payload
        key = str(payload.get("target_logical_key"))
        if payload.get("target_kind") == "package":
            package_summaries[key] = str(payload.get("summary", ""))
        elif payload.get("target_kind") == "module" and key in target.top_modules:
            top_module_summaries[key] = str(payload.get("summary", ""))
    counts = conn.execute(
        select(m.artifact.c.kind, func.count())
        .select_from(
            m.snapshot_entry.join(
                m.artifact, m.artifact.c.artifact_id == m.snapshot_entry.c.artifact_id
            )
        )
        .where(m.snapshot_entry.c.sha == sha, m.artifact.c.kind != "description")
        .group_by(m.artifact.c.kind)
    ).all()
    return {
        "top_packages": target.top_packages,
        "top_modules": target.top_modules,
        "package_summaries": dict(sorted(package_summaries.items())[:_FACT_LIST_CAP]),
        "top_module_summaries": dict(sorted(top_module_summaries.items())[:_FACT_LIST_CAP]),
        "cross_package_imports": sorted(cross_package)[:_FACT_LIST_CAP],
        "external_imports": sorted(external)[:_FACT_LIST_CAP],
        "artifact_counts": {str(kind): int(count) for kind, count in counts},
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
