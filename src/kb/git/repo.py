"""Read-only git access for ingestion (DESIGN.md §7 step 1).

Resolves commits and enumerates the Python sources of a tree at a given SHA. Reads blobs straight
from the object database (no checkout), so indexing a historical SHA never touches the working
tree. Writing ``commit_ref`` rows is the store's job (``kb.store.writer.upsert_commit``); this
module only reads git.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import cast

import pygit2


def open_repo(path: str) -> pygit2.Repository:
    return pygit2.Repository(path)


def resolve_commit(repo: pygit2.Repository, rev: str) -> pygit2.Commit:
    """Peel any committish (sha, branch, tag, HEAD) to a Commit."""
    return repo.revparse_single(rev).peel(pygit2.Commit)


def commit_sha(repo: pygit2.Repository, rev: str) -> str:
    return str(resolve_commit(repo, rev).id)


def parent_shas(repo: pygit2.Repository, rev: str) -> list[str]:
    return [str(oid) for oid in resolve_commit(repo, rev).parent_ids]


def branch_head_sha(repo: pygit2.Repository, branch: str) -> str | None:
    """Commit sha a LOCAL branch points at (short name or full ref), or None if absent.

    Uses ``references.get`` (correctly typed, works on bare repos) rather than ``branches.get``,
    whose annotation in pygit2 1.19 claims ``Branch`` but actually returns ``None`` on a miss.
    """
    name = branch if branch.startswith("refs/") else f"refs/heads/{branch}"
    ref = repo.references.get(name)
    if ref is None:
        return None
    return str(ref.peel(pygit2.Commit).id)


def first_parent_chain(
    repo: pygit2.Repository, head_sha: str, stop_sha: str, *, limit: int
) -> list[str] | None:
    """The first-parent chain ``(stop_sha, head_sha]``, oldest -> newest, or ``None``.

    ``None`` means ``stop_sha`` was not reached within ``limit`` commits — either the branch
    advanced further than ``limit`` or its history no longer contains ``stop_sha`` (force-push /
    rewind). Both get the same recovery (a single explicit-parent incremental index), so the cases
    are deliberately collapsed rather than paying an unbounded walk to distinguish them.
    """
    chain: list[str] = []
    current = head_sha
    for _ in range(limit):
        chain.append(current)
        parents = parent_shas(repo, current)
        if not parents:  # reached a root commit without meeting stop_sha
            return None
        current = parents[0]
        if current == stop_sha:
            return list(reversed(chain))
    return None


def iter_python_files_at(repo: pygit2.Repository, rev: str) -> Iterator[tuple[str, bytes]]:
    """Yield ``(posix_path, source_bytes)`` for every ``*.py`` blob in the tree at ``rev``."""
    commit = resolve_commit(repo, rev)
    yield from _walk_tree(repo, commit.tree, "")


def iter_files_under_at(
    repo: pygit2.Repository, rev: str, prefix: str
) -> Iterator[tuple[str, bytes]]:
    """Yield ``(posix_path, bytes)`` for every blob under ``prefix`` in the tree at ``rev``.

    Materialization aid for extractor config that travels with the commit (e.g. ``.kb/``); the
    ``*.py``-only ``iter_python_files_at`` stays untouched so root discovery, parsing and diff
    reuse never see non-Python paths.
    """
    commit = resolve_commit(repo, rev)
    key = prefix.rstrip("/")
    try:
        entry = commit.tree[key]
    except KeyError:
        return
    if entry.type_str != "tree":
        return
    subtree = cast("pygit2.Tree", repo[entry.id])
    yield from _walk_any_tree(repo, subtree, f"{key}/")


def _walk_any_tree(
    repo: pygit2.Repository, tree: pygit2.Tree, prefix: str
) -> Iterator[tuple[str, bytes]]:
    for entry in tree:
        name = entry.name or ""
        path = f"{prefix}{name}"
        if entry.type_str == "tree":
            yield from _walk_any_tree(repo, cast("pygit2.Tree", repo[entry.id]), f"{path}/")
        elif entry.type_str == "blob":
            yield path, bytes(cast("pygit2.Blob", repo[entry.id]).data)


def _walk_tree(
    repo: pygit2.Repository, tree: pygit2.Tree, prefix: str
) -> Iterator[tuple[str, bytes]]:
    for entry in tree:
        name = entry.name or ""
        path = f"{prefix}{name}"
        if entry.type_str == "tree":
            yield from _walk_tree(repo, cast("pygit2.Tree", repo[entry.id]), f"{path}/")
        elif entry.type_str == "blob" and name.endswith(".py"):
            blob = cast("pygit2.Blob", repo[entry.id])
            yield path, bytes(blob.data)
