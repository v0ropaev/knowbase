"""Indexing pipeline: INGEST -> STRUCTURE -> EXTRACT -> SNAPSHOT (DESIGN.md §7).

For one commit: record ``commit_ref``, parse every first-party Python file into content-addressed
spans (upserting identities + per-SHA occurrences), then run the configured deterministic
extractors and write their grounded artifacts + the snapshot manifest. Extractors run against an
on-disk materialization of the tree at the SHA (so resolver-based ones like grimp see the exact
snapshot, not the working tree).
"""

from __future__ import annotations

import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pygit2
from sqlalchemy import Connection, Engine

from kb.extract.base import ExtractContext, Extractor
from kb.git import repo as gitrepo
from kb.git.diff import changed_paths
from kb.store.queries import is_sha_indexed, reusable_spans
from kb.store.writer import (
    upsert_commit,
    upsert_occurrence,
    upsert_span,
    write_grounded_artifact,
    write_snapshot_entry,
)
from kb.structural.interface import ParsedSpan
from kb.structural.layout import discover_first_party_root, relativize_to_root
from kb.structural.symbol_path import module_fqname
from kb.structural.treesitter_index import TreeSitterIndex


@dataclass(frozen=True)
class IndexResult:
    sha: str
    files_indexed: int
    spans: int
    artifacts: int
    gaps: list[str]  # repo-relative paths whose parse hit a syntax error (recorded, not dropped)
    mode: str = "full"  # "full" | "incremental"
    parsed_files: int = 0  # first-party files (re)parsed this run
    reused_files: int = 0  # first-party files whose spans were reused from the parent snapshot


def index_commit(
    engine: Engine,
    repo_path: str,
    rev: str,
    *,
    extractors: Sequence[Extractor] = (),
    first_party_root: str | None = None,
    incremental: bool = False,
    parent: str | None = None,
) -> IndexResult:
    """Index one commit (DESIGN.md §7).

    In ``incremental`` mode (or when ``parent`` is given), reuse the spans of files unchanged since
    an already-indexed parent commit instead of re-parsing them; only changed/new files are parsed.
    Extractors still run fully over the materialized tree (correct for whole-snapshot/cross-file
    extractors), and idempotent writes make unchanged artifacts no-ops — so the result is identical
    to a full re-index. Falls back to a full index when no indexed parent applies.
    """
    repo = gitrepo.open_repo(repo_path)
    sha = gitrepo.commit_sha(repo, rev)
    files = dict(gitrepo.iter_python_files_at(repo, sha))
    root = (
        first_party_root
        if first_party_root is not None
        else discover_first_party_root(list(files))
    )

    index = TreeSitterIndex()
    spans_by_module: dict[str, list[ParsedSpan]] = {}
    path_by_module: dict[str, str] = {}
    gaps: list[str] = []
    total_spans = 0
    parsed_files = 0
    reused_files = 0

    with engine.begin() as conn:
        upsert_commit(conn, sha, gitrepo.parent_shas(repo, sha))
        parent_sha = _resolve_parent(
            conn,
            repo,
            sha,
            incremental=incremental,
            parent=parent,
            first_party_root=first_party_root,
            root=root,
        )
        mode = "incremental" if parent_sha is not None else "full"

        reuse_paths: set[str] = set()
        if parent_sha is not None:
            changed = changed_paths(repo, parent_sha, sha)
            reuse_paths = {
                path
                for path in files
                if relativize_to_root(path, root) is not None and path not in changed
            }
            reuse_paths = _reuse_unchanged_spans(
                conn, sha, parent_sha, root, reuse_paths, spans_by_module, path_by_module
            )
            for module_path in reuse_paths:
                module = module_fqname(_require_rel(module_path, root))
                for span in spans_by_module[module]:
                    upsert_span(conn, span)
                    upsert_occurrence(conn, sha, module_path, span)
                    total_spans += 1
            reused_files = len(reuse_paths)

        for path, source in files.items():
            rel = relativize_to_root(path, root)
            if rel is None:  # outside the first-party root
                continue
            if path in reuse_paths:  # spans reused from the parent snapshot, no re-parse
                continue
            module = module_fqname(rel)
            result = index.parse_file(module, source)
            if result.has_error:
                gaps.append(path)
            spans_by_module[module] = list(result.spans)
            path_by_module[module] = path
            for span in result.spans:
                upsert_span(conn, span)
                upsert_occurrence(conn, sha, path, span)
                total_spans += 1
            parsed_files += 1

        artifacts = 0
        if extractors:
            artifacts = _run_extractors(
                conn, extractors, files, sha, root, spans_by_module, path_by_module
            )

    return IndexResult(
        sha=sha,
        files_indexed=len(path_by_module),
        spans=total_spans,
        artifacts=artifacts,
        gaps=gaps,
        mode=mode,
        parsed_files=parsed_files,
        reused_files=reused_files,
    )


def _require_rel(path: str, root: str) -> str:
    rel = relativize_to_root(path, root)
    assert rel is not None  # callers pass only first-party paths
    return rel


def _resolve_parent(
    conn: Connection,
    repo: pygit2.Repository,
    sha: str,
    *,
    incremental: bool,
    parent: str | None,
    first_party_root: str | None,
    root: str,
) -> str | None:
    """The already-indexed parent snapshot to diff against, or ``None`` to do a full index.

    An explicit ``parent`` implies incremental and must already be indexed (else we raise loudly).
    Auto mode picks the first parent commit that is indexed. Span reuse is only valid when the
    first-party root is unchanged (it drives module names -> span identity), so a root change falls
    back to a full re-index.
    """
    if parent is None and not incremental:
        return None
    if parent is not None:
        candidate = gitrepo.commit_sha(repo, parent)
        if not is_sha_indexed(conn, candidate):
            raise ValueError(f"explicit parent {candidate[:12]} is not indexed")
    else:
        candidate = next(
            (p for p in gitrepo.parent_shas(repo, sha) if is_sha_indexed(conn, p)), ""
        )
        if not candidate:
            return None
    parent_files = dict(gitrepo.iter_python_files_at(repo, candidate))
    parent_root = (
        first_party_root
        if first_party_root is not None
        else discover_first_party_root(list(parent_files))
    )
    return candidate if parent_root == root else None


def _reuse_unchanged_spans(
    conn: Connection,
    sha: str,
    parent_sha: str,
    root: str,
    reuse_paths: set[str],
    spans_by_module: dict[str, list[ParsedSpan]],
    path_by_module: dict[str, str],
) -> set[str]:
    """Populate ``spans_by_module`` / ``path_by_module`` for unchanged files from the parent
    snapshot. Returns the paths actually reused — a file whose spans are missing in the parent (DB
    drift) is dropped so the caller re-parses it instead."""
    spans_by_path: dict[str, list[ParsedSpan]] = {}
    for file_path, span in reusable_spans(conn, parent_sha, reuse_paths):
        spans_by_path.setdefault(file_path, []).append(span)
    for file_path, spans in spans_by_path.items():
        module = module_fqname(_require_rel(file_path, root))
        spans_by_module[module] = spans
        path_by_module[module] = file_path
    return set(spans_by_path)


def _run_extractors(
    conn: Connection,
    extractors: Sequence[Extractor],
    files: dict[str, bytes],
    sha: str,
    first_party_root: str,
    spans_by_module: dict[str, list[ParsedSpan]],
    path_by_module: dict[str, str],
) -> int:
    written = 0
    with tempfile.TemporaryDirectory(prefix="kb-snapshot-") as tmp:
        for path, content in files.items():
            target = Path(tmp) / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        ctx = ExtractContext(
            sha=sha,
            materialized_root=tmp,
            first_party_root=first_party_root,
            spans_by_module=spans_by_module,
            path_by_module=path_by_module,
        )
        for extractor in extractors:
            for artifact in extractor.extract(ctx):
                artifact_id = write_grounded_artifact(conn, artifact)
                write_snapshot_entry(conn, sha, artifact.logical_key, artifact_id)
                written += 1
    return written
