"""Read-side queries: invalidation and (next push) MCP retrieval (DESIGN.md §6, §10)."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import Connection, and_, or_, select

from kb.store import models as m


def invalidated_artifact_ids(
    conn: Connection, sha: str, changed_span_ids: Collection[bytes]
) -> set[bytes]:
    """Artifacts in ``sha``'s snapshot grounded in a changed span (one-hop invalidation).

    The MVP dependency DAG is one hop (span -> artifact); the recursive artifact->artifact walk
    arrives with the semantic layer (DESIGN.md §6, deferred).
    """
    if not changed_span_ids:
        return set()
    join = m.snapshot_entry.join(
        m.artifact_derived_from,
        m.artifact_derived_from.c.artifact_id == m.snapshot_entry.c.artifact_id,
    )
    rows = conn.execute(
        select(m.snapshot_entry.c.artifact_id)
        .select_from(join)
        .where(
            m.snapshot_entry.c.sha == sha,
            m.artifact_derived_from.c.span_id.in_(list(changed_span_ids)),
        )
        .distinct()
    ).scalars()
    return set(rows)


def logical_keys_for_artifacts(
    conn: Connection, sha: str, artifact_ids: Collection[bytes]
) -> set[str]:
    """The snapshot logical keys for a set of artifact ids at ``sha`` (for readable assertions)."""
    if not artifact_ids:
        return set()
    rows = conn.execute(
        select(m.snapshot_entry.c.logical_key).where(
            m.snapshot_entry.c.sha == sha,
            m.snapshot_entry.c.artifact_id.in_(list(artifact_ids)),
        )
    ).scalars()
    return set(rows)


# --- MCP read side (DESIGN.md §10) -----------------------------------------


@dataclass(frozen=True)
class SpanHitRow:
    span_id: bytes  # internal — never surfaced in MCP records
    span_kind: str
    fq_symbol_path: str
    file_path: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class GroundedArtifactRow:
    logical_key: str
    kind: str
    payload: dict[str, Any]
    is_deterministic: bool
    confidence: float
    role: str | None  # grounding-edge role for a span (None for unit-level fetches)


@dataclass(frozen=True)
class ProvenanceRow:
    file_path: str
    start_line: int
    end_line: int
    role: str | None


def latest_ingested_sha(conn: Connection) -> str | None:
    """The most-recently-ingested snapshot sha (ingest order, not git topology — DESIGN.md §10)."""
    stmt = (
        select(m.commit_ref.c.sha)
        .join(m.snapshot_entry, m.snapshot_entry.c.sha == m.commit_ref.c.sha)
        .order_by(m.commit_ref.c.ingested_at.desc(), m.commit_ref.c.sha)
        .limit(1)
    )
    return cast("str | None", conn.execute(stmt).scalar())


def latest_sha_for_logical_key(conn: Connection, logical_key: str) -> str | None:
    """The most-recently-ingested sha whose snapshot contains ``logical_key`` (drives freshness)."""
    stmt = (
        select(m.commit_ref.c.sha)
        .select_from(
            m.snapshot_entry.join(m.commit_ref, m.commit_ref.c.sha == m.snapshot_entry.c.sha)
        )
        .where(m.snapshot_entry.c.logical_key == logical_key)
        .order_by(m.commit_ref.c.ingested_at.desc(), m.commit_ref.c.sha)
        .limit(1)
    )
    return cast("str | None", conn.execute(stmt).scalar())


def spans_at_location(conn: Connection, file: str, line: int, sha: str) -> list[SpanHitRow]:
    """Spans whose occurrence at ``sha`` covers ``file:line`` (1-based)."""
    join = m.code_span.join(m.span_occurrence, m.span_occurrence.c.span_id == m.code_span.c.span_id)
    rows = conn.execute(
        select(
            m.code_span.c.span_id,
            m.code_span.c.span_kind,
            m.code_span.c.fq_symbol_path,
            m.span_occurrence.c.file_path,
            m.span_occurrence.c.start_line,
            m.span_occurrence.c.end_line,
        )
        .select_from(join)
        .where(
            m.span_occurrence.c.file_path == file,
            m.span_occurrence.c.sha == sha,
            m.span_occurrence.c.start_line <= line,
            m.span_occurrence.c.end_line >= line,
        )
        .order_by(
            m.span_occurrence.c.start_line,
            m.span_occurrence.c.end_line.desc(),
            m.code_span.c.fq_symbol_path,
        )
    ).all()
    return [SpanHitRow(*row) for row in rows]


def artifacts_grounded_on_span(
    conn: Connection, sha: str, span_id: bytes
) -> list[GroundedArtifactRow]:
    """Artifacts in ``sha``'s snapshot grounded on ``span_id``, each with its grounding role."""
    join = m.snapshot_entry.join(
        m.artifact, m.artifact.c.artifact_id == m.snapshot_entry.c.artifact_id
    ).join(
        m.artifact_derived_from,
        m.artifact_derived_from.c.artifact_id == m.artifact.c.artifact_id,
    )
    rows = conn.execute(
        select(
            m.artifact.c.logical_key,
            m.artifact.c.kind,
            m.artifact.c.payload,
            m.artifact.c.is_deterministic,
            m.artifact.c.confidence,
            m.artifact_derived_from.c.role,
        )
        .select_from(join)
        .where(m.snapshot_entry.c.sha == sha, m.artifact_derived_from.c.span_id == span_id)
        .order_by(m.artifact.c.logical_key)
    ).all()
    return [GroundedArtifactRow(*row) for row in rows]


def provenance_for_artifact(conn: Connection, sha: str, logical_key: str) -> list[ProvenanceRow]:
    """Grounding occurrences (file:line@sha + role) for the ``(sha, logical_key)`` artifact."""
    join = m.snapshot_entry.join(
        m.artifact_derived_from,
        m.artifact_derived_from.c.artifact_id == m.snapshot_entry.c.artifact_id,
    ).join(
        m.span_occurrence,
        and_(
            m.span_occurrence.c.span_id == m.artifact_derived_from.c.span_id,
            m.span_occurrence.c.sha == sha,
        ),
    )
    rows = conn.execute(
        select(
            m.span_occurrence.c.file_path,
            m.span_occurrence.c.start_line,
            m.span_occurrence.c.end_line,
            m.artifact_derived_from.c.role,
        )
        .select_from(join)
        .where(m.snapshot_entry.c.sha == sha, m.snapshot_entry.c.logical_key == logical_key)
        .order_by(
            m.span_occurrence.c.file_path,
            m.span_occurrence.c.start_line,
            m.artifact_derived_from.c.role,
        )
    ).all()
    return [ProvenanceRow(*row) for row in rows]


@dataclass(frozen=True)
class ArtifactSpanRow:
    span_id: bytes
    fq_symbol_path: str
    raw_text: str  # the span's source text at this sha (input + ground-truth for the LLM describer)


def spans_for_artifact(conn: Connection, sha: str, logical_key: str) -> list[ArtifactSpanRow]:
    """The grounding spans of the ``(sha, logical_key)`` artifact, with id + fq path + source text.

    Feeds the LLM-grounded describer: the spans are both the prompt context and the deterministic
    ground truth its claims are validated against (DESIGN.md §9).
    """
    join = (
        m.snapshot_entry.join(
            m.artifact_derived_from,
            m.artifact_derived_from.c.artifact_id == m.snapshot_entry.c.artifact_id,
        )
        .join(m.code_span, m.code_span.c.span_id == m.artifact_derived_from.c.span_id)
        .join(
            m.span_occurrence,
            and_(
                m.span_occurrence.c.span_id == m.artifact_derived_from.c.span_id,
                m.span_occurrence.c.sha == sha,
            ),
        )
    )
    rows = conn.execute(
        select(
            m.code_span.c.span_id,
            m.code_span.c.fq_symbol_path,
            m.span_occurrence.c.raw_text,
        )
        .select_from(join)
        .where(m.snapshot_entry.c.sha == sha, m.snapshot_entry.c.logical_key == logical_key)
        .order_by(m.code_span.c.fq_symbol_path)
    ).all()
    return [ArtifactSpanRow(r.span_id, r.fq_symbol_path, r.raw_text) for r in rows]


def _like_literal(value: str, suffix: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + suffix


def resolve_target_logical_keys(conn: Connection, sha: str, target: str) -> list[str]:
    """Resolve ``target`` to logical keys: exact, then prefix, then file, then module."""

    def _scalars(stmt: Any) -> list[str]:
        return [str(value) for value in conn.execute(stmt).scalars()]

    exact = _scalars(
        select(m.snapshot_entry.c.logical_key).where(
            m.snapshot_entry.c.sha == sha, m.snapshot_entry.c.logical_key == target
        )
    )
    if exact:
        return exact

    prefix = _scalars(
        select(m.snapshot_entry.c.logical_key)
        .where(
            m.snapshot_entry.c.sha == sha,
            m.snapshot_entry.c.logical_key.like(_like_literal(target, "%"), escape="\\"),
        )
        .distinct()
        .order_by(m.snapshot_entry.c.logical_key)
    )
    if prefix:
        return prefix

    by_file = _scalars(
        select(m.snapshot_entry.c.logical_key)
        .select_from(
            m.snapshot_entry.join(
                m.artifact_derived_from,
                m.artifact_derived_from.c.artifact_id == m.snapshot_entry.c.artifact_id,
            ).join(
                m.span_occurrence,
                and_(
                    m.span_occurrence.c.span_id == m.artifact_derived_from.c.span_id,
                    m.span_occurrence.c.sha == sha,
                ),
            )
        )
        .where(m.snapshot_entry.c.sha == sha, m.span_occurrence.c.file_path == target)
        .distinct()
        .order_by(m.snapshot_entry.c.logical_key)
    )
    if by_file:
        return by_file

    by_module = _scalars(
        select(m.snapshot_entry.c.logical_key)
        .select_from(
            m.snapshot_entry.join(
                m.artifact_derived_from,
                m.artifact_derived_from.c.artifact_id == m.snapshot_entry.c.artifact_id,
            ).join(m.code_span, m.code_span.c.span_id == m.artifact_derived_from.c.span_id)
        )
        .where(
            m.snapshot_entry.c.sha == sha,
            or_(
                m.code_span.c.fq_symbol_path == target,
                m.code_span.c.fq_symbol_path.like(_like_literal(target, ".%"), escape="\\"),
            ),
        )
        .distinct()
        .order_by(m.snapshot_entry.c.logical_key)
    )
    return by_module


def units_for_logical_keys(
    conn: Connection, sha: str, logical_keys: Sequence[str]
) -> list[GroundedArtifactRow]:
    """Batch-fetch artifact rows for ``logical_keys`` in the snapshot (unit-level role=None)."""
    if not logical_keys:
        return []
    join = m.snapshot_entry.join(
        m.artifact, m.artifact.c.artifact_id == m.snapshot_entry.c.artifact_id
    )
    rows = conn.execute(
        select(
            m.artifact.c.logical_key,
            m.artifact.c.kind,
            m.artifact.c.payload,
            m.artifact.c.is_deterministic,
            m.artifact.c.confidence,
        )
        .select_from(join)
        .where(
            m.snapshot_entry.c.sha == sha,
            m.snapshot_entry.c.logical_key.in_(list(logical_keys)),
        )
        .order_by(m.artifact.c.logical_key)
    ).all()
    return [
        GroundedArtifactRow(
            r.logical_key, r.kind, r.payload, r.is_deterministic, r.confidence, None
        )
        for r in rows
    ]


def similar_artifacts_by_embedding(
    conn: Connection, sha: str, query_embedding: Sequence[float], k: int
) -> list[GroundedArtifactRow]:
    """Top-k artifacts in ``sha``'s snapshot by cosine distance to ``query_embedding``.

    ``cosine_distance`` is a pgvector comparator not in SQLAlchemy's stubs, so it's called through a
    cast (which also binds the list as a ``vector``, unlike a raw ``op("<=>")``).
    """
    join = m.snapshot_entry.join(
        m.artifact, m.artifact.c.artifact_id == m.snapshot_entry.c.artifact_id
    )
    distance = cast(Any, m.artifact.c.embedding).cosine_distance(list(query_embedding))
    rows = conn.execute(
        select(
            m.artifact.c.logical_key,
            m.artifact.c.kind,
            m.artifact.c.payload,
            m.artifact.c.is_deterministic,
            m.artifact.c.confidence,
        )
        .select_from(join)
        .where(m.snapshot_entry.c.sha == sha, m.artifact.c.embedding.is_not(None))
        .order_by(distance)
        .limit(k)
    ).all()
    return [
        GroundedArtifactRow(
            r.logical_key, r.kind, r.payload, r.is_deterministic, r.confidence, None
        )
        for r in rows
    ]
