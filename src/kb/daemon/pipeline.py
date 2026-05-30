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

from sqlalchemy import Connection, Engine

from kb.extract.base import ExtractContext, Extractor
from kb.git import repo as gitrepo
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


def index_commit(
    engine: Engine,
    repo_path: str,
    rev: str,
    *,
    extractors: Sequence[Extractor] = (),
    first_party_root: str | None = None,
) -> IndexResult:
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

    with engine.begin() as conn:
        upsert_commit(conn, sha, gitrepo.parent_shas(repo, sha))
        for path, source in files.items():
            rel = relativize_to_root(path, root)
            if rel is None:  # outside the first-party root
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
    )


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
