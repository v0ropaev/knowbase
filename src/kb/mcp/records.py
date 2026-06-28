"""Provenance-carrying records served over MCP (DESIGN.md §10).

Every ``KnowledgeUnit`` ALWAYS carries provenance (file:line@sha), the extraction method, a
confidence, and freshness — so an agent can trust deterministic facts and weigh future
model-grounded summaries accordingly.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel

ExtractionMethod = Literal["deterministic", "llm_grounded"]


class Provenance(BaseModel):
    file: str
    line: int
    sha: str
    role: str | None = None


class KnowledgeUnit(BaseModel):
    logical_key: str
    kind: str
    summary: str
    payload: dict[str, Any]
    extraction_method: ExtractionMethod
    confidence: float
    freshness: str  # "current" | "stale@<sha12>"
    provenance: list[Provenance]


class SpanHit(BaseModel):
    span_kind: str
    fq_symbol_path: str
    file: str
    start_line: int
    end_line: int
    knowledge: list[KnowledgeUnit]


class FindProvenanceResult(BaseModel):
    sha: str
    hits: list[SpanHit]


class GetKnowledgeResult(BaseModel):
    sha: str
    units: list[KnowledgeUnit]
    total_matched: int
    returned: int
    omitted: int


class SearchKnowledgeResult(BaseModel):
    sha: str
    units: list[KnowledgeUnit]  # cosine-ranked
    total_matched: int
    returned: int
    omitted: int


def summarize(kind: str, payload: dict[str, Any]) -> str:
    if kind == "import_edge":
        return f"{payload.get('importer', '?')} -> {payload.get('imported', '?')}"
    if kind == "api_route":
        model = payload.get("response_model_base") or "?"
        return f"{payload.get('method', '?')} {payload.get('path', '?')} -> {model}"
    if kind == "entity":
        framework = payload.get("framework", "?")
        n_fields = len(payload.get("fields", []))
        return f"{payload.get('qualified_name', '?')} ({framework}, {n_fields} fields)"
    if kind == "public_symbol":
        via = "re-export" if payload.get("is_reexport") else payload.get("exported_via", "?")
        kind_label = payload.get("symbol_kind") or "?"
        return f"{payload.get('public_qualified_name', '?')} ({kind_label}, {via})"
    if kind == "description":
        summary = str(payload.get("summary", "")).strip().splitlines()
        return summary[0] if summary else f"description of {payload.get('target_logical_key', '?')}"
    return kind


def estimate_tokens(unit: KnowledgeUnit) -> int:
    return len(json.dumps(unit.model_dump(), default=str)) // 4
