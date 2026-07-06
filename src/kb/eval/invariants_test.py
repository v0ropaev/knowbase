"""HARD GATE: cross-cutting invariants over a real index (DESIGN.md §9).

Zero orphans: every artifact in a snapshot is grounded (>=1 derived_from). Reproducibility:
re-indexing the same SHA yields the identical set of artifact ids (content-addressing is stable).
Distinctness: ``logical_key`` is part of artifact identity (rule v2, ``ARTIFACT_ID_VERSION``), so
two different knowledge units can never collapse into one digest even when they share their entire
evidence span set.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, func, select

from kb.daemon.pipeline import index_commit
from kb.eval._fixtures import make_git_repo
from kb.extract.base import DerivedEdge, ExtractedArtifact
from kb.extract.deterministic.imports import ImportExtractor
from kb.store import models as m

FILES = {
    "src/shop/__init__.py": "from shop.orders import make\n",
    "src/shop/orders.py": "from shop.billing import charge\n\ndef make():\n    return charge()\n",
    "src/shop/billing.py": "def charge():\n    return 1\n",
}


def _index(engine: Engine, tmp_path: Path) -> str:
    sha = make_git_repo(tmp_path, [FILES])[0]
    index_commit(engine, str(tmp_path), sha, extractors=[ImportExtractor()])
    return sha


def _snapshot_artifact_ids(engine: Engine, sha: str) -> set[bytes]:
    with engine.connect() as conn:
        return set(
            conn.execute(
                select(m.snapshot_entry.c.artifact_id).where(m.snapshot_entry.c.sha == sha)
            ).scalars()
        )


def test_no_orphan_artifacts_in_snapshot(engine: Engine, tmp_path: Path) -> None:
    sha = _index(engine, tmp_path)
    join = m.snapshot_entry.outerjoin(
        m.artifact_derived_from,
        m.artifact_derived_from.c.artifact_id == m.snapshot_entry.c.artifact_id,
    )
    with engine.connect() as conn:
        orphans = conn.execute(
            select(m.snapshot_entry.c.artifact_id)
            .select_from(join)
            .where(
                m.snapshot_entry.c.sha == sha,
                m.artifact_derived_from.c.artifact_id.is_(None),
            )
        ).fetchall()
    assert orphans == []


def test_reindex_is_reproducible(engine: Engine, tmp_path: Path) -> None:
    sha = _index(engine, tmp_path)
    first = _snapshot_artifact_ids(engine, sha)
    index_commit(engine, str(tmp_path), sha, extractors=[ImportExtractor()])  # re-index same SHA
    second = _snapshot_artifact_ids(engine, sha)
    assert first == second
    assert len(first) > 0


def test_logical_key_is_part_of_artifact_identity() -> None:
    """Identity rule v2: same evidence + same extractor but different logical keys -> different
    ids (the mutual-reference collision class); identical inputs still reproduce one id."""

    def art(key: str) -> ExtractedArtifact:
        return ExtractedArtifact(
            kind="entity",
            logical_key=key,
            payload={},
            derived_from=[DerivedEdge(b"\x01" * 32), DerivedEdge(b"\x02" * 32)],
            extractor_id="entities",
            extractor_version="2",
        )

    assert art("entity:a.Order").artifact_id() != art("entity:b.LineItem").artifact_id()
    assert art("entity:a.Order").artifact_id() == art("entity:a.Order").artifact_id()


def test_distinct_logical_keys_have_distinct_artifact_ids(engine: Engine, tmp_path: Path) -> None:
    """Snapshot-level guard for identity v2: within a snapshot, one artifact_id per logical_key."""
    sha = _index(engine, tmp_path)
    with engine.connect() as conn:
        keys, ids = conn.execute(
            select(
                func.count(m.snapshot_entry.c.logical_key.distinct()),
                func.count(m.snapshot_entry.c.artifact_id.distinct()),
            ).where(m.snapshot_entry.c.sha == sha)
        ).one()
    assert keys == ids
    assert keys > 0
