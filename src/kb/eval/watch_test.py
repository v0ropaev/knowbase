"""``kb watch`` tick semantics — orchestration over the already-gated incremental core.

Supporting suite (like the MCP tests): the incremental correctness itself is HARD gate #12
(``incremental_reindex_test``); here we assert the watch state machine — fresh tick, per-commit
catch-up, idle ticks, the ``max_catchup`` jump, rewinds/force-pushes, and crash resume via the
``branch_ref`` cursor. The headline HARD-gate count stays at twelve.

Shared-DB discipline: the ``engine`` fixture is session-scoped, so every test uses a unique package
tag (unique modules -> unique logical keys / shas) AND watches its own uniquely-named branch
(``branch_ref.name`` is a PK in the shared database). The full commit chain is built up front; a
"push" between ticks is simulated by moving the branch ref with ``set_target`` (indexing reads
blobs from the odb, never the working tree).
"""

from __future__ import annotations

from pathlib import Path

import pygit2
import pytest
from sqlalchemy import Engine

from kb.daemon import watch
from kb.daemon.watch import watch_tick
from kb.eval._fixtures import make_git_repo
from kb.extract.deterministic.imports import ImportExtractor
from kb.store.queries import branch_head, is_sha_indexed


def _files(tag: str, version: str) -> dict[str, str]:
    # a.py imports b.py so ImportExtractor always yields >= 1 artifact: is_sha_indexed witnesses
    # snapshot_entry rows, so a zero-artifact snapshot would never register as indexed.
    return {
        f"src/{tag}/__init__.py": "",
        f"src/{tag}/b.py": f"B = '{tag}-const'\n",
        f"src/{tag}/a.py": f"from {tag}.b import B\nVALUE = '{version}'\n",
    }


def _setup(tmp_path: Path, tag: str, n: int) -> tuple[pygit2.Repository, list[str]]:
    """A repo with an n-commit chain and a branch named ``tag`` pointing at the FIRST commit."""
    shas = make_git_repo(tmp_path, [_files(tag, f"v{i}") for i in range(n)])
    repo = pygit2.Repository(str(tmp_path))
    first = repo.revparse_single(shas[0]).peel(pygit2.Commit)
    repo.branches.create(tag, first)
    return repo, shas


def _move(repo: pygit2.Repository, tag: str, sha: str) -> None:
    repo.references[f"refs/heads/{tag}"].set_target(sha)


def _tick(engine: Engine, tmp_path: Path, tag: str, **kw):  # type: ignore[no-untyped-def]
    return watch_tick(
        engine, str(tmp_path), tag, extractors=[ImportExtractor()], first_party_root="src", **kw
    )


def _cursor(engine: Engine, tag: str) -> str | None:
    with engine.connect() as conn:
        return branch_head(conn, tag)


def test_fresh_tick_indexes_head_full_and_sets_cursor(engine: Engine, tmp_path: Path) -> None:
    tag = "wfresh"
    _repo, shas = _setup(tmp_path, tag, 1)
    results = _tick(engine, tmp_path, tag)
    assert [r.sha for r in results] == [shas[0]]
    assert results[0].mode == "full"  # fresh database: auto-parent falls back to full
    assert _cursor(engine, tag) == shas[0]


def test_advance_two_commits_indexes_each_incrementally_in_order(
    engine: Engine, tmp_path: Path
) -> None:
    tag = "wadvance"
    repo, shas = _setup(tmp_path, tag, 3)
    _tick(engine, tmp_path, tag)  # index shas[0], set cursor
    _move(repo, tag, shas[2])  # "push" two commits

    results = _tick(engine, tmp_path, tag)
    assert [r.sha for r in results] == [shas[1], shas[2]]  # oldest -> newest
    for r in results:
        assert r.mode == "incremental"
        assert r.parsed_files == 1  # only a.py changes per commit ...
        assert r.reused_files == 2  # ... __init__.py + b.py are reused from the parent
    assert _cursor(engine, tag) == shas[2]


def test_noop_when_ref_unmoved(engine: Engine, tmp_path: Path) -> None:
    tag = "wnoop"
    _repo, shas = _setup(tmp_path, tag, 1)
    _tick(engine, tmp_path, tag)
    assert _tick(engine, tmp_path, tag) == []  # idle tick
    assert _cursor(engine, tag) == shas[0]


def test_max_catchup_exceeded_skips_to_head_with_explicit_parent(
    engine: Engine, tmp_path: Path
) -> None:
    tag = "wjump"
    repo, shas = _setup(tmp_path, tag, 3)
    _tick(engine, tmp_path, tag)
    _move(repo, tag, shas[2])

    notes: list[str] = []
    results = _tick(engine, tmp_path, tag, max_catchup=1, echo=notes.append)
    assert [r.sha for r in results] == [shas[2]]  # a single jump to head ...
    assert results[0].mode == "incremental"  # ... diffed against the explicit cursor parent
    with engine.connect() as conn:
        assert is_sha_indexed(conn, shas[1]) is False  # the intermediate commit is skipped
    assert _cursor(engine, tag) == shas[2]
    assert notes  # the jump is reported, not silent


def test_rewind_to_indexed_ancestor_updates_cursor_only(engine: Engine, tmp_path: Path) -> None:
    tag = "wrewind"
    repo, shas = _setup(tmp_path, tag, 2)
    _tick(engine, tmp_path, tag)  # index shas[0]
    _move(repo, tag, shas[1])
    _tick(engine, tmp_path, tag)  # index shas[1]

    _move(repo, tag, shas[0])  # rewind to an already-indexed ancestor
    notes: list[str] = []
    assert _tick(engine, tmp_path, tag, echo=notes.append) == []  # no re-index, no crash
    assert _cursor(engine, tag) == shas[0]
    assert notes


def test_rewind_to_unindexed_commit_uses_descendant_cursor_as_parent(
    engine: Engine, tmp_path: Path
) -> None:
    tag = "wdiverge"
    repo, shas = _setup(tmp_path, tag, 3)
    _tick(engine, tmp_path, tag)
    _move(repo, tag, shas[2])
    _tick(engine, tmp_path, tag, max_catchup=1)  # jump: shas[1] stays unindexed, cursor = shas[2]

    _move(repo, tag, shas[1])  # rewind to the unindexed intermediate commit
    notes: list[str] = []
    results = _tick(engine, tmp_path, tag, echo=notes.append)
    assert [r.sha for r in results] == [shas[1]]
    assert results[0].mode == "incremental"  # diffed against the DESCENDANT cursor (tree pair)
    assert _cursor(engine, tag) == shas[1]
    assert notes  # divergence is reported


def test_mid_chain_failure_advances_cursor_then_resumes(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tag = "wcrash"
    repo, shas = _setup(tmp_path, tag, 3)
    _tick(engine, tmp_path, tag)
    _move(repo, tag, shas[2])

    real_index_commit = watch.index_commit

    def flaky(engine_, repo_path, rev, **kw):  # type: ignore[no-untyped-def]
        if rev == shas[2]:
            raise RuntimeError("boom")
        return real_index_commit(engine_, repo_path, rev, **kw)

    monkeypatch.setattr(watch, "index_commit", flaky)
    with pytest.raises(RuntimeError):
        _tick(engine, tmp_path, tag)
    assert _cursor(engine, tag) == shas[1]  # the completed commit was cursored before the crash

    monkeypatch.setattr(watch, "index_commit", real_index_commit)
    results = _tick(engine, tmp_path, tag)  # resume: only the failed commit remains
    assert [r.sha for r in results] == [shas[2]]
    assert _cursor(engine, tag) == shas[2]
