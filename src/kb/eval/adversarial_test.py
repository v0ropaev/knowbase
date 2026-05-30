"""HARD GATE: the anti-hallucination invariant is enforced end-to-end (DESIGN.md §3, step 4b).

Two layers must reject an ungrounded artifact: the application (``GroundingError`` before any
write) and the database (the DEFERRED ``artifact_grounded_check`` trigger at COMMIT). A genuinely
grounded artifact must commit cleanly.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Connection, Engine, delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError

from kb.extract.base import DerivedEdge, ExtractedArtifact
from kb.store import models as m
from kb.store.writer import GroundingError, upsert_span, write_grounded_artifact
from kb.structural.treesitter_index import TreeSitterIndex

INDEX = TreeSitterIndex()


def _function_span():
    spans = INDEX.parse_file("pkg.mod", b"def f(x):\n    return x\n").spans
    return next(s for s in spans if s.span_kind == "function")


def test_app_layer_rejects_ungrounded(conn: Connection) -> None:
    art = ExtractedArtifact(
        kind="x",
        logical_key="x",
        payload={},
        derived_from=[],  # no grounding
        extractor_id="t",
        extractor_version="1",
    )
    with pytest.raises(GroundingError):
        write_grounded_artifact(conn, art)


def test_db_trigger_rejects_ungrounded_at_commit(engine: Engine) -> None:
    artifact_id = bytes([0xAB]) * 32
    with pytest.raises(DBAPIError):
        with engine.begin() as conn:  # commits on exit -> deferred trigger fires
            conn.execute(
                pg_insert(m.artifact).values(
                    artifact_id=artifact_id,
                    kind="x",
                    extractor_id="t",
                    extractor_version="1",
                    prompt_version="",
                    model_id="",
                    framework_versions={},
                    is_deterministic=True,
                    confidence=1.0,
                    logical_key="adv:ungrounded",
                    payload={},
                )
            )
    # the failed COMMIT persisted nothing
    with engine.connect() as conn:
        present = conn.execute(
            select(m.artifact.c.artifact_id).where(m.artifact.c.artifact_id == artifact_id)
        ).first()
    assert present is None


def test_db_trigger_accepts_grounded_at_commit(engine: Engine) -> None:
    span = _function_span()
    art = ExtractedArtifact(
        kind="import_edge",
        logical_key="adv:grounded",
        payload={},
        derived_from=[DerivedEdge(span.span_id)],
        extractor_id="adv",
        extractor_version="1",
    )
    artifact_id = art.artifact_id()
    try:
        with engine.begin() as conn:
            upsert_span(conn, span)
            write_grounded_artifact(conn, art)  # commits cleanly => trigger passed
        with engine.connect() as conn:
            got = conn.execute(
                select(m.artifact.c.artifact_id).where(m.artifact.c.artifact_id == artifact_id)
            ).scalar()
        assert got == artifact_id
    finally:
        with engine.begin() as conn:
            conn.execute(
                delete(m.artifact_derived_from).where(
                    m.artifact_derived_from.c.artifact_id == artifact_id
                )
            )
            conn.execute(delete(m.artifact).where(m.artifact.c.artifact_id == artifact_id))
            conn.execute(delete(m.code_span).where(m.code_span.c.span_id == span.span_id))
