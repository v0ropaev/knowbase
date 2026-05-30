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


def iter_python_files_at(repo: pygit2.Repository, rev: str) -> Iterator[tuple[str, bytes]]:
    """Yield ``(posix_path, source_bytes)`` for every ``*.py`` blob in the tree at ``rev``."""
    commit = resolve_commit(repo, rev)
    yield from _walk_tree(repo, commit.tree, "")


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
