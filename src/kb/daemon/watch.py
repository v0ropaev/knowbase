"""``kb watch`` — the live trigger over the incremental re-index core (DESIGN.md §7).

Polls a LOCAL branch ref (pygit2; no network, no credentials — pair it with a bare repo receiving
pushes, a cron ``git pull``, or a CI step) and indexes every new first-parent commit incrementally,
recording the resume point in ``branch_ref``. The cursor advances after EACH successfully indexed
commit, so a crash resumes where it left off; all store writes are idempotent, so re-running a
half-finished tick is safe.

Catch-up semantics: up to ``max_catchup`` commits are indexed one by one (each step's auto-detected
parent is the previous step). Beyond that — or when the cursor is no longer on the branch's
first-parent history (force-push / rewind) — a single incremental index of the new head is run
against the cursor as the explicit parent (a tree-pair diff needs no ancestry).

Caveat (pre-existing semantics): a snapshot with zero artifacts never registers as indexed
(``is_sha_indexed`` witnesses ``snapshot_entry``), so watching a repo that produces no artifacts
re-indexes its head every tick. One database tracks ONE repository (the schema has no repo column).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence

import pygit2
from sqlalchemy import Engine

from kb.daemon.pipeline import IndexResult, index_commit
from kb.extract.base import Extractor
from kb.extract.deterministic.entities import EntityExtractor
from kb.extract.deterministic.events import EventExtractor
from kb.extract.deterministic.fastapi_contract import FastAPIExtractor
from kb.extract.deterministic.imports import ImportExtractor
from kb.extract.deterministic.library_surface import LibrarySurfaceExtractor
from kb.git import repo as gitrepo
from kb.store.queries import branch_head, is_sha_indexed
from kb.store.writer import upsert_branch_ref


def default_extractors() -> list[Extractor]:
    """The standard extractor set shared by ``kb index`` and ``kb watch``."""
    return [
        ImportExtractor(),
        FastAPIExtractor(),
        EntityExtractor(),
        EventExtractor(),
        LibrarySurfaceExtractor(),
    ]


def _noop_echo(_line: str) -> None:
    return None


def _set_cursor(engine: Engine, branch: str, sha: str) -> None:
    with engine.begin() as conn:
        upsert_branch_ref(conn, branch, sha)


def watch_tick(
    engine: Engine,
    repo_path: str,
    branch: str,
    *,
    extractors: Sequence[Extractor] = (),
    max_catchup: int = 50,
    first_party_root: str | None = None,
    echo: Callable[[str], None] = _noop_echo,
) -> list[IndexResult]:
    """One poll of the local ref: index whatever the branch gained since the cursor.

    Returns one ``IndexResult`` per indexed commit (empty on an idle tick). The check order is
    load-bearing — see the module docstring for the catch-up / force-push semantics.
    """
    repo = gitrepo.open_repo(repo_path)
    head = gitrepo.branch_head_sha(repo, branch)
    if head is None:
        raise ValueError(f"branch {branch!r} not found in {repo_path}")

    with engine.connect() as conn:
        raw_cursor = branch_head(conn, branch)
        cursor = raw_cursor if raw_cursor is not None and is_sha_indexed(conn, raw_cursor) else None
        head_indexed = is_sha_indexed(conn, head)

    if cursor == head:
        return []  # idle tick
    if head_indexed:  # rewind to an indexed ancestor / a second branch on known commits
        _set_cursor(engine, branch, head)
        echo(f"cursor moved to already-indexed {head[:12]}")
        return []
    if cursor is None:  # fresh branch/database: index the head only, never the whole history
        result = index_commit(
            engine, repo_path, head,
            extractors=extractors, first_party_root=first_party_root, incremental=True,
        )
        _set_cursor(engine, branch, head)
        return [result]

    chain = gitrepo.first_parent_chain(repo, head, cursor, limit=max_catchup)
    if chain is not None:
        results: list[IndexResult] = []
        for sha in chain:  # oldest -> newest; auto-parent = the previous (indexed) step
            results.append(
                index_commit(
                    engine, repo_path, sha,
                    extractors=extractors, first_party_root=first_party_root, incremental=True,
                )
            )
            _set_cursor(engine, branch, sha)  # per-commit: a crash resumes from here
        return results

    # Diverged (force-push / rewind past an unindexed commit) or further than max_catchup:
    # one incremental index of the head against the cursor (indexed by construction).
    parent: str | None = cursor
    try:
        gitrepo.resolve_commit(repo, cursor)
    except (KeyError, pygit2.GitError):  # cursor commit gc'd from the repo -> auto-detect
        parent = None
    echo(f"branch jumped past the cursor; indexing head {head[:12]} against {str(parent)[:12]}")
    result = index_commit(
        engine, repo_path, head,
        extractors=extractors, first_party_root=first_party_root,
        incremental=True, parent=parent,
    )
    _set_cursor(engine, branch, head)
    return [result]


def run_watch(
    engine: Engine,
    repo_path: str,
    branch: str,
    *,
    extractors: Sequence[Extractor],
    interval_s: float,
    max_catchup: int,
    once: bool,
    first_party_root: str | None = None,
    echo: Callable[[str], None] = print,
) -> None:
    """The polling loop: tick, report, sleep. ``once`` runs a single tick (cron/CI-friendly).

    In loop mode a failing tick is logged and retried on the next tick (transient errors self-heal;
    a deterministic one repeats visibly instead of killing the daemon — completed commits are never
    redone because the cursor advanced). In ``once`` mode exceptions propagate (non-zero exit).
    ``KeyboardInterrupt`` is not an ``Exception`` and propagates to the CLI for a clean stop.
    """
    while True:
        try:
            results = watch_tick(
                engine, repo_path, branch,
                extractors=extractors, max_catchup=max_catchup,
                first_party_root=first_party_root, echo=echo,
            )
            for r in results:
                echo(
                    f"indexed {r.sha[:12]} ({r.mode}): {r.files_indexed} files "
                    f"({r.parsed_files} parsed, {r.reused_files} reused), {r.spans} spans, "
                    f"{r.artifacts} artifacts, {len(r.gaps)} gaps"
                )
        except Exception as exc:
            if once:
                raise
            echo(f"watch tick failed (will retry next tick): {exc!r}")
        if once:
            return
        time.sleep(interval_s)
