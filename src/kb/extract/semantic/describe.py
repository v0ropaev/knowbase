"""LLM-grounded NL descriptions over a snapshot — a separate, key-gated pass (DESIGN.md §4, §9).

For each ``api_route`` / ``entity`` artifact AND each first-party module (file), an LLM writes a
short summary plus structured claims; each claim is validated against the target's own grounding
spans (``grounding.validate_claims``), unvalidated claims are dropped, and — if anything survives —
a ``description`` artifact is stored grounded on the SAME spans (role ``describes``,
``is_deterministic=False``). Modules are grounded on ALL of the file's spans. Never on the
``kb index`` path. Idempotent per (model, prompt): ``artifact_id`` folds in model_id + prompt.
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
from kb.store.queries import ArtifactSpanRow, module_targets, spans_for_artifact
from kb.store.writer import write_grounded_artifact, write_snapshot_entry

EXTRACTOR_ID = "llm_describe"
EXTRACTOR_VERSION = "1"
PROMPT_VERSION = "1"
DESCRIBE_KINDS = ("api_route", "entity")
_BODY_CAP = 6000  # prompt source-span body cap (validation still runs over every span)

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
) -> tuple[bool, int]:
    """Describe one target (artifact or module) from its grounding spans.

    Returns ``(stored, dropped_count)``. A ``description`` artifact is stored (grounded on the
    spans, role ``describes``) only if >= 1 claim survives span-validation; otherwise nothing is
    stored (anti-hallucination). Idempotent per (model, prompt).
    """
    prompt = _build_prompt(target_kind, facts, spans)
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


def _build_prompt(kind: str, facts: dict[str, Any], spans: list[ArtifactSpanRow]) -> str:
    facts_json = json.dumps(facts, default=str)[:800]
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


def _parse_json(raw: str) -> dict[str, Any] | None:
    match = re.search(r"\{.*\}", raw, re.S)
    if match is None:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None
