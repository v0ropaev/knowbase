"""HARD GATE — incremental re-index equivalence (DESIGN.md §6, §7).

``index_commit(incremental=True)`` reuses the spans of files unchanged since the parent commit
(re-parsing only changed files) while the extractors still run fully over the materialized tree.
This gate proves the result is identical to a FULL re-index of the same tree — the same
``{logical_key: artifact_id}`` snapshot — AND that the parse was actually skipped (the
``reused_files`` / ``parsed_files`` counters), so the optimization can never silently drift from the
deterministic spine. ``artifact_id`` is content-addressed (SHA-independent), so the comparison runs
in one database across two distinct commit SHAs; no second Postgres is needed.

The fixture is a self-contained ``incpkg`` package with its own routes/entities (distinct paths and
module names from the shared ``app.*`` fixture), so indexing several of its snapshots into the
shared session database never collides with another test's logical keys / freshness.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Engine, select

from kb.daemon.pipeline import IndexResult, index_commit
from kb.eval._fixtures import make_git_repo
from kb.extract.deterministic.entities import EntityExtractor
from kb.extract.deterministic.fastapi_contract import FastAPIExtractor
from kb.extract.deterministic.imports import ImportExtractor
from kb.store import models as m

EXTRACTORS = [ImportExtractor(), FastAPIExtractor(), EntityExtractor()]

# A small cross-file FastAPI package: routes (incpkg.routes) reference response models that live in
# incpkg.schemas, so an edit to schemas re-grounds the routes across files.
INC = {
    "src/incpkg/__init__.py": "",
    "src/incpkg/schemas.py": (
        "from pydantic import BaseModel\n\n"
        "class ItemOut(BaseModel):\n    id: int\n    name: str\n\n"
        "class ItemIn(BaseModel):\n    sku: str\n"
    ),
    "src/incpkg/routes.py": (
        "from typing import List\n"
        "from fastapi import APIRouter\n"
        "from incpkg.schemas import ItemOut, ItemIn\n\n"
        "router = APIRouter()\n\n"
        "@router.get('/items', response_model=List[ItemOut])\n"
        "def list_items(limit: int = 10):\n    return []\n\n"
        "@router.post('/items', response_model=ItemOut, status_code=201)\n"
        "def create_item(body: ItemIn):\n    return body\n"
    ),
    "src/incpkg/main.py": (
        "from fastapi import FastAPI\n"
        "from incpkg.routes import router\n\n"
        "app = FastAPI()\n"
        "app.include_router(router, prefix='/inc')\n"
    ),
}

# INC_B == INC with one file edited: ItemOut gains a field, so its class span changes (new span_id)
# and the routes referencing it cross-file are re-grounded. The other three files stay identical and
# take the reuse path.
INC_B = {
    **INC,
    "src/incpkg/schemas.py": (
        "from pydantic import BaseModel\n\n"
        "class ItemOut(BaseModel):\n    id: int\n    name: str\n    qty: int\n\n"
        "class ItemIn(BaseModel):\n    sku: str\n"
    ),
}


def _index(
    engine: Engine,
    repo_dir: Path,
    rev: str,
    *,
    incremental: bool = False,
    parent: str | None = None,
) -> IndexResult:
    return index_commit(
        engine,
        str(repo_dir),
        rev,
        extractors=EXTRACTORS,
        first_party_root="src",
        incremental=incremental,
        parent=parent,
    )


def _snapshot_map(engine: Engine, sha: str) -> dict[str, bytes]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(m.snapshot_entry.c.logical_key, m.snapshot_entry.c.artifact_id).where(
                m.snapshot_entry.c.sha == sha
            )
        ).all()
    return {r.logical_key: bytes(r.artifact_id) for r in rows}


def _solo(tag: str) -> dict[str, str]:
    """A minimal first-party tree with content unique to ``tag`` (so its SHAs never collide with
    another test's in the shared session database)."""
    return {f"src/{tag}/__init__.py": "", f"src/{tag}/m.py": f"VALUE = {tag!r}\n"}


def test_incremental_equals_full_reindex(engine: Engine, tmp_path: Path) -> None:
    r1 = tmp_path / "r1"
    sha_a, sha_b = make_git_repo(r1, [INC, INC_B])
    _index(engine, r1, sha_a)  # full index of the parent
    inc = _index(engine, r1, sha_b, incremental=True)  # parent auto-detected from parent_shas

    r2 = tmp_path / "r2"
    (sha_b_full,) = make_git_repo(r2, [INC_B])
    _index(engine, r2, sha_b_full)  # full re-index of the same tree, as a distinct commit

    assert sha_b != sha_b_full  # distinct SHAs -> no snapshot_entry collision in one DB
    inc_map = _snapshot_map(engine, sha_b)
    full_map = _snapshot_map(engine, sha_b_full)
    assert inc_map  # non-empty: routes + entities + import edges were produced
    assert inc_map == full_map  # equivalence: incremental == full, artifact-for-artifact

    # incrementality: only the edited file was parsed; the other three were reused (parse skipped)
    assert inc.mode == "incremental"
    assert inc.parsed_files == 1
    assert inc.reused_files == 3


def test_full_fallback_when_parent_not_indexed(engine: Engine, tmp_path: Path) -> None:
    base = _solo("fallback")
    edited = {**base, "src/fallback/m.py": "VALUE = 'changed'\n"}
    r = tmp_path / "fb"
    _sha_a, sha_b = make_git_repo(r, [base, edited])
    # index only the child, incrementally: its parent was never indexed -> safe full fallback
    result = _index(engine, r, sha_b, incremental=True)
    assert result.mode == "full"
    assert result.reused_files == 0
    assert result.parsed_files >= 1


def test_explicit_unindexed_parent_raises(engine: Engine, tmp_path: Path) -> None:
    base = _solo("raise")
    edited = {**base, "src/raise/m.py": "VALUE = 'changed'\n"}
    r = tmp_path / "rz"
    sha_a, sha_b = make_git_repo(r, [base, edited])
    with pytest.raises(ValueError):
        _index(engine, r, sha_b, parent=sha_a)  # sha_a is not indexed -> loud failure
