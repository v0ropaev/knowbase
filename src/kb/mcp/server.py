"""Read-only MCP server: find_provenance + get_knowledge over the store (DESIGN.md §10).

``build_server(engine)`` returns a FastMCP server bound to an engine; each tool call opens a short
read connection. Every unit carries provenance + method + confidence + freshness.
"""

from __future__ import annotations

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from sqlalchemy import Connection, Engine

from kb.embed.providers import EmbeddingProvider, default_provider
from kb.mcp.records import (
    ExtractionMethod,
    FindProvenanceResult,
    GetKnowledgeResult,
    KnowledgeUnit,
    Provenance,
    SearchKnowledgeResult,
    SpanHit,
    estimate_tokens,
    summarize,
)
from kb.store import queries as q
from kb.store.queries import GroundedArtifactRow

_READ_ONLY = ToolAnnotations(readOnlyHint=True)


def build_server(engine: Engine) -> FastMCP:
    mcp = FastMCP(name="knowbase")
    provider_cache: dict[str, EmbeddingProvider] = {}

    def _provider() -> EmbeddingProvider:
        # Lazy + cached: the embedding model (torch) loads only on the first search_knowledge call,
        # so `kb serve` stays torch-free until semantic search is actually used.
        if "p" not in provider_cache:
            provider_cache["p"] = default_provider()
        return provider_cache["p"]

    @mcp.tool(annotations=_READ_ONLY)
    def find_provenance(file: str, line: int, sha: str | None = None) -> FindProvenanceResult:
        """Knowledge grounded at ``file:line@sha``: the spans there + artifacts derived from them.

        ``sha`` defaults to the latest-ingested snapshot. Unknown location -> empty hits.
        """
        with engine.connect() as conn:
            resolved = sha or q.latest_ingested_sha(conn)
            if resolved is None:
                return FindProvenanceResult(sha="", hits=[])
            hits = [
                SpanHit(
                    span_kind=span.span_kind,
                    fq_symbol_path=span.fq_symbol_path,
                    file=span.file_path,
                    start_line=span.start_line,
                    end_line=span.end_line,
                    knowledge=[
                        _unit(conn, resolved, row)
                        for row in q.artifacts_grounded_on_span(conn, resolved, span.span_id)
                    ],
                )
                for span in q.spans_at_location(conn, file, line, resolved)
            ]
            return FindProvenanceResult(sha=resolved, hits=hits)

    @mcp.tool(annotations=_READ_ONLY)
    def get_knowledge(
        target: str, sha: str | None = None, token_budget: int = 2000
    ) -> GetKnowledgeResult:
        """Knowledge units for a ``target`` (a logical key, file path, or module), ranked + trimmed.

        Resolution: exact logical key, else prefix, else by file, else by module. Ranked by
        confidence then kind then key; greedily trimmed to ``token_budget`` (>=1 unit kept), with
        the dropped count reported as ``omitted`` (no silent truncation).
        """
        with engine.connect() as conn:
            resolved = sha or q.latest_ingested_sha(conn)
            if resolved is None:
                return GetKnowledgeResult(sha="", units=[], total_matched=0, returned=0, omitted=0)
            keys = q.resolve_target_logical_keys(conn, resolved, target)
            units = [
                _unit(conn, resolved, row)
                for row in q.units_for_logical_keys(conn, resolved, keys)
            ]
            units.sort(key=lambda u: (-u.confidence, u.kind, u.logical_key))
            kept: list[KnowledgeUnit] = []
            used = 0
            for unit in units:
                cost = estimate_tokens(unit)
                if kept and used + cost > token_budget:
                    break
                used += cost
                kept.append(unit)
            return GetKnowledgeResult(
                sha=resolved,
                units=kept,
                total_matched=len(units),
                returned=len(kept),
                omitted=len(units) - len(kept),
            )

    @mcp.tool(annotations=_READ_ONLY)
    def search_knowledge(
        query: str, sha: str | None = None, k: int = 8, token_budget: int = 2000
    ) -> SearchKnowledgeResult:
        """Semantic search over grounded knowledge: embed ``query``, cosine-rank artifacts in the
        snapshot, return budget-trimmed grounded units (each with provenance/method/confidence/
        freshness). ``sha`` defaults to the latest-ingested snapshot. Requires ``kb embed`` to have
        populated embeddings; otherwise returns empty.
        """
        with engine.connect() as conn:
            resolved = sha or q.latest_ingested_sha(conn)
            if resolved is None:
                return SearchKnowledgeResult(
                    sha="", units=[], total_matched=0, returned=0, omitted=0
                )
            [vector] = _provider().embed([query])
            units = [
                _unit(conn, resolved, row)
                for row in q.similar_artifacts_by_embedding(conn, resolved, vector, k)
            ]
            kept: list[KnowledgeUnit] = []
            used = 0
            for unit in units:  # already cosine-ranked; trim to budget, never silently
                cost = estimate_tokens(unit)
                if kept and used + cost > token_budget:
                    break
                used += cost
                kept.append(unit)
            return SearchKnowledgeResult(
                sha=resolved,
                units=kept,
                total_matched=len(units),
                returned=len(kept),
                omitted=len(units) - len(kept),
            )

    return mcp


def _unit(conn: Connection, sha: str, row: GroundedArtifactRow) -> KnowledgeUnit:
    method: ExtractionMethod = "deterministic" if row.is_deterministic else "llm_grounded"
    provenance = [
        Provenance(file=p.file_path, line=p.start_line, sha=sha, role=p.role)
        for p in q.provenance_for_artifact(conn, sha, row.logical_key)
    ]
    latest = q.latest_sha_for_logical_key(conn, row.logical_key)
    freshness = "current" if latest is None or latest == sha else f"stale@{latest[:12]}"
    return KnowledgeUnit(
        logical_key=row.logical_key,
        kind=row.kind,
        summary=summarize(row.kind, row.payload),
        payload=row.payload,
        extraction_method=method,
        confidence=row.confidence,
        freshness=freshness,
        provenance=provenance,
    )
