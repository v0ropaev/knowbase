"""Incremental seed: which spans changed between two commits (DESIGN.md §6, §7 step 3).

A span's identity is content-addressed, so a span whose code changed simply *disappears* at the
new SHA (a new identity takes its place). The invalidation seed is therefore, per changed file,
the span ids present at ``sha_a`` but absent at ``sha_b`` — those are the groundings that no longer
hold, so the artifacts derived from them must be re-extracted.
"""

from __future__ import annotations

import pygit2

from kb.git.repo import iter_python_files_at, resolve_commit
from kb.structural.layout import discover_first_party_root, relativize_to_root
from kb.structural.symbol_path import module_fqname
from kb.structural.treesitter_index import TreeSitterIndex


def changed_paths(repo: pygit2.Repository, sha_a: str, sha_b: str) -> set[str]:
    """Python file paths touched between ``sha_a`` and ``sha_b`` (added/modified/deleted)."""
    tree_a = resolve_commit(repo, sha_a).tree
    tree_b = resolve_commit(repo, sha_b).tree
    paths: set[str] = set()
    for delta in tree_a.diff_to_tree(tree_b).deltas:
        for candidate in (delta.old_file.path, delta.new_file.path):
            if candidate and candidate.endswith(".py"):
                paths.add(candidate)
    return paths


def changed_span_ids(
    repo: pygit2.Repository, sha_a: str, sha_b: str, *, first_party_root: str | None = None
) -> set[bytes]:
    """Span ids present at ``sha_a`` but gone at ``sha_b`` (the invalidation seed)."""
    files_a = dict(iter_python_files_at(repo, sha_a))
    files_b = dict(iter_python_files_at(repo, sha_b))
    root_a = first_party_root or discover_first_party_root(list(files_a))
    root_b = first_party_root or discover_first_party_root(list(files_b))
    index = TreeSitterIndex()

    seed: set[bytes] = set()
    for path in changed_paths(repo, sha_a, sha_b):
        ids_a = _file_span_ids(index, path, files_a.get(path), root_a)
        ids_b = _file_span_ids(index, path, files_b.get(path), root_b)
        seed |= ids_a - ids_b
    return seed


def _file_span_ids(
    index: TreeSitterIndex, path: str, source: bytes | None, root: str
) -> set[bytes]:
    if source is None:
        return set()
    rel = relativize_to_root(path, root)
    if rel is None:
        return set()
    module = module_fqname(rel)
    return {span.span_id for span in index.parse_file(module, source).spans}
