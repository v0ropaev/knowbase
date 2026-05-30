"""Grounded, idempotent writes (DESIGN.md §3, §6).

All writes are content-addressed and idempotent (``ON CONFLICT DO NOTHING``). An artifact is
always written together with its derived-from edges in one transaction so the DEFERRED
``artifact_grounded_check`` trigger passes at COMMIT. ``write_grounded_artifact`` additionally
fails fast in-app (``GroundingError``) if asked to store an artifact with no spans — mirroring the
DB invariant so the anti-hallucination rule cannot be bypassed by a code path that forgets it.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import Connection
from sqlalchemy.dialects.postgresql import insert as pg_insert

from kb.extract.base import ExtractedArtifact
from kb.ids import NORMALIZATION_VERSION
from kb.store import models as m
from kb.structural.interface import ParsedSpan


class GroundingError(ValueError):
    """Raised when an artifact would be stored without >= 1 derived-from span (DESIGN.md §3)."""


def upsert_commit(conn: Connection, sha: str, parent_shas: Sequence[str] = ()) -> None:
    stmt = (
        pg_insert(m.commit_ref)
        .values(sha=sha, parent_shas=list(parent_shas))
        .on_conflict_do_nothing(index_elements=["sha"])
    )
    conn.execute(stmt)


def upsert_span(
    conn: Connection, span: ParsedSpan, *, normalization_version: int = NORMALIZATION_VERSION
) -> None:
    """Insert the content-addressed identity row (location-free). Idempotent on ``span_id``."""
    stmt = (
        pg_insert(m.code_span)
        .values(
            span_id=span.span_id,
            lang=span.lang,
            span_kind=span.span_kind,
            fq_symbol_path=span.fq_symbol_path,
            structural_fingerprint=span.structural_fingerprint,
            normalization_version=normalization_version,
            symbol_id=None,
        )
        .on_conflict_do_nothing(index_elements=["span_id"])
    )
    conn.execute(stmt)


def upsert_occurrence(conn: Connection, sha: str, file_path: str, span: ParsedSpan) -> None:
    """Insert the per-SHA location of a span. Idempotent on ``(span_id, sha)``."""
    stmt = (
        pg_insert(m.span_occurrence)
        .values(
            span_id=span.span_id,
            sha=sha,
            file_path=file_path,
            start_byte=span.start_byte,
            end_byte=span.end_byte,
            start_line=span.start_line,
            end_line=span.end_line,
            raw_text=span.raw_text,
        )
        .on_conflict_do_nothing(index_elements=["span_id", "sha"])
    )
    conn.execute(stmt)


def write_grounded_artifact(conn: Connection, art: ExtractedArtifact) -> bytes:
    """Write an artifact and its derived-from edges in one go. Returns the artifact id.

    Raises ``GroundingError`` if there are no derived-from spans. The artifact row is inserted
    first, then its edges; with ``ON CONFLICT DO NOTHING`` a re-write of an existing artifact is a
    no-op (so the DEFERRED grounding trigger does not re-fire for already-grounded artifacts).
    """
    if not art.derived_from:
        raise GroundingError(f"artifact {art.logical_key!r} has no derived_from spans")

    artifact_id = art.artifact_id()
    conn.execute(
        pg_insert(m.artifact)
        .values(
            artifact_id=artifact_id,
            kind=art.kind,
            extractor_id=art.extractor_id,
            extractor_version=art.extractor_version,
            prompt_version=art.prompt_version,
            model_id=art.model_id,
            framework_versions=dict(art.framework_versions),
            is_deterministic=art.is_deterministic,
            confidence=art.confidence,
            logical_key=art.logical_key,
            payload=dict(art.payload),
        )
        .on_conflict_do_nothing(index_elements=["artifact_id"])
    )
    for edge in art.derived_from:
        conn.execute(
            pg_insert(m.artifact_derived_from)
            .values(artifact_id=artifact_id, span_id=edge.span_id, role=edge.role)
            .on_conflict_do_nothing(index_elements=["artifact_id", "span_id"])
        )
    return artifact_id


def write_snapshot_entry(
    conn: Connection, sha: str, logical_key: str, artifact_id: bytes
) -> None:
    """Map ``(sha, logical_key)`` to the current artifact version (git-tree-like manifest)."""
    stmt = pg_insert(m.snapshot_entry).values(
        sha=sha, logical_key=logical_key, artifact_id=artifact_id
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["sha", "logical_key"],
        set_={"artifact_id": stmt.excluded.artifact_id},
    )
    conn.execute(stmt)
