"""HARD GATE: cross-cutting invariants over a real index (DESIGN.md §9).

Zero orphans: every artifact in a snapshot is grounded (>=1 derived_from). Reproducibility:
re-indexing the same SHA yields the identical set of artifact ids (content-addressing is stable).
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, select

from kb.daemon.pipeline import index_commit
from kb.eval._fixtures import make_git_repo
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
