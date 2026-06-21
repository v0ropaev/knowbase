"""LLM-grounded NL descriptions over a snapshot — a separate, key-gated pass (DESIGN.md §4, §9).

For each ``api_route`` / ``entity`` artifact, an LLM writes a short summary plus structured claims;
each claim is validated against the artifact's own grounding spans (``grounding.validate_claims``),
unvalidated claims are dropped, and — if anything survives — a ``description`` artifact is stored
grounded on the SAME spans (role ``describes``, ``is_deterministic=False``). Never on the
``kb index`` path. Idempotent per (model, prompt): ``artifact_id`` folds in model_id + prompt.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, select

from kb.extract.base import DerivedEdge, ExtractedArtifact
from kb.extract.semantic.grounding import validate_claims
from kb.llm.providers import LLMProvider
from kb.store import models as m
from kb.store.queries import spans_for_artifact
from kb.store.writer import write_grounded_artifact, write_snapshot_entry

EXTRACTOR_ID = "llm_describe"
EXTRACTOR_VERSION = "1"
PROMPT_VERSION = "1"
DESCRIBE_KINDS = ("api_route", "entity")

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
    """Generate grounded descriptions for the snapshot's api_route / entity artifacts."""
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
            prompt = _build_prompt(target.kind, target.payload, spans)
            data = _parse_json(provider.complete(_SYSTEM, prompt, max_tokens=600))
            if data is None:
                continue
            raw_claims = [c for c in data.get("claims", []) if isinstance(c, dict)]
            kept, dropped = validate_claims(
                raw_claims, [s.raw_text for s in spans], [s.fq_symbol_path for s in spans]
            )
            dropped_total += len(dropped)
            if not kept:
                continue  # nothing grounded survives -> store nothing (anti-hallucination)
            artifact = ExtractedArtifact(
                kind="description",
                logical_key=f"desc:{target.logical_key}",
                payload={
                    "target_logical_key": target.logical_key,
                    "target_kind": target.kind,
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
            described += 1
    return DescribeResult(sha=sha, described=described, dropped_claims=dropped_total)


def _build_prompt(kind: str, payload: dict[str, Any], spans: list[Any]) -> str:
    facts = json.dumps(payload, default=str)[:800]
    body = "\n\n".join(f"# {s.fq_symbol_path}\n{s.raw_text}" for s in spans)
    return f"Artifact kind: {kind}\nKnown facts: {facts}\n\nSource spans:\n{body}"


def _parse_json(raw: str) -> dict[str, Any] | None:
    match = re.search(r"\{.*\}", raw, re.S)
    if match is None:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None
