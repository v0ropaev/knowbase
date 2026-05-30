"""Store writer: round-trip + content-addressed idempotency (DESIGN.md §6, step 4)."""

from __future__ import annotations

from sqlalchemy import Connection, func, select

from kb.extract.base import DerivedEdge, ExtractedArtifact
from kb.store import models as m
from kb.store.writer import (
    upsert_commit,
    upsert_occurrence,
    upsert_span,
    write_grounded_artifact,
    write_snapshot_entry,
)
from kb.structural.treesitter_index import TreeSitterIndex

INDEX = TreeSitterIndex()


def _function_span():
    spans = INDEX.parse_file("pkg.mod", b"def f(x):\n    return x + 1\n").spans
    return next(s for s in spans if s.span_kind == "function")


def _import_artifact(span_id: bytes) -> ExtractedArtifact:
    return ExtractedArtifact(
        kind="import_edge",
        logical_key="import:a->b",
        payload={"importer": "a", "imported": "b"},
        derived_from=[DerivedEdge(span_id, "import_statement")],
        extractor_id="imports",
        extractor_version="1",
    )


def test_span_and_occurrence_roundtrip(conn: Connection) -> None:
    span = _function_span()
    upsert_commit(conn, "sha1")
    upsert_span(conn, span)
    upsert_occurrence(conn, "sha1", "pkg/mod.py", span)

    row = conn.execute(
        select(m.code_span.c.fq_symbol_path, m.code_span.c.span_kind).where(
            m.code_span.c.span_id == span.span_id
        )
    ).one()
    assert row.fq_symbol_path == "pkg.mod.f"
    assert row.span_kind == "function"

    occ = conn.execute(
        select(m.span_occurrence.c.file_path, m.span_occurrence.c.start_line).where(
            m.span_occurrence.c.span_id == span.span_id
        )
    ).one()
    assert occ.file_path == "pkg/mod.py"
    assert occ.start_line == 1  # 1-based


def test_span_upsert_is_idempotent(conn: Connection) -> None:
    span = _function_span()
    upsert_span(conn, span)
    upsert_span(conn, span)
    count = conn.execute(
        select(func.count()).select_from(m.code_span).where(m.code_span.c.span_id == span.span_id)
    ).scalar()
    assert count == 1


def test_grounded_artifact_and_snapshot_roundtrip(conn: Connection) -> None:
    span = _function_span()
    upsert_commit(conn, "sha1")
    upsert_span(conn, span)
    art = _import_artifact(span.span_id)

    artifact_id = write_grounded_artifact(conn, art)
    write_snapshot_entry(conn, "sha1", art.logical_key, artifact_id)

    got = conn.execute(
        select(m.artifact.c.logical_key, m.artifact.c.payload, m.artifact.c.is_deterministic).where(
            m.artifact.c.artifact_id == artifact_id
        )
    ).one()
    assert got.logical_key == "import:a->b"
    assert got.payload["imported"] == "b"
    assert got.is_deterministic is True

    edges = conn.execute(
        select(m.artifact_derived_from.c.span_id, m.artifact_derived_from.c.role).where(
            m.artifact_derived_from.c.artifact_id == artifact_id
        )
    ).all()
    assert (span.span_id, "import_statement") in [(e.span_id, e.role) for e in edges]

    snap = conn.execute(
        select(m.snapshot_entry.c.artifact_id).where(
            m.snapshot_entry.c.sha == "sha1", m.snapshot_entry.c.logical_key == "import:a->b"
        )
    ).scalar()
    assert snap == artifact_id


def test_artifact_write_is_idempotent(conn: Connection) -> None:
    span = _function_span()
    upsert_span(conn, span)
    art = _import_artifact(span.span_id)
    first = write_grounded_artifact(conn, art)
    second = write_grounded_artifact(conn, art)
    assert first == second
    count = conn.execute(
        select(func.count()).select_from(m.artifact).where(m.artifact.c.artifact_id == first)
    ).scalar()
    assert count == 1
