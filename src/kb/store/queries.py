"""Read-side queries: invalidation and (next push) MCP retrieval (DESIGN.md §6, §10)."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import Connection, and_, or_, select

from kb.ids import NORMALIZATION_VERSION
from kb.store import models as m
from kb.structural.interface import ParsedSpan


def invalidated_artifact_ids(
    conn: Connection, sha: str, changed_span_ids: Collection[bytes]
) -> set[bytes]:
    """Artifacts in ``sha``'s snapshot grounded in a changed span (one-hop invalidation).

    The MVP dependency DAG is one hop (span -> artifact); the recursive artifact->artifact walk
    arrives with the semantic layer (DESIGN.md §6, deferred).
    """
    if not changed_span_ids:
        return set()
    join = m.snapshot_entry.join(
        m.artifact_derived_from,
        m.artifact_derived_from.c.artifact_id == m.snapshot_entry.c.artifact_id,
    )
    rows = conn.execute(
        select(m.snapshot_entry.c.artifact_id)
        .select_from(join)
        .where(
            m.snapshot_entry.c.sha == sha,
            m.artifact_derived_from.c.span_id.in_(list(changed_span_ids)),
        )
        .distinct()
    ).scalars()
    return set(rows)


def logical_keys_for_artifacts(
    conn: Connection, sha: str, artifact_ids: Collection[bytes]
) -> set[str]:
    """The snapshot logical keys for a set of artifact ids at ``sha`` (for readable assertions)."""
    if not artifact_ids:
        return set()
    rows = conn.execute(
        select(m.snapshot_entry.c.logical_key).where(
            m.snapshot_entry.c.sha == sha,
            m.snapshot_entry.c.artifact_id.in_(list(artifact_ids)),
        )
    ).scalars()
    return set(rows)


# --- MCP read side (DESIGN.md §10) -----------------------------------------


@dataclass(frozen=True)
class SpanHitRow:
    span_id: bytes  # internal — never surfaced in MCP records
    span_kind: str
    fq_symbol_path: str
    file_path: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class GroundedArtifactRow:
    logical_key: str
    kind: str
    payload: dict[str, Any]
    is_deterministic: bool
    confidence: float
    role: str | None  # grounding-edge role for a span (None for unit-level fetches)


@dataclass(frozen=True)
class ProvenanceRow:
    file_path: str
    start_line: int
    end_line: int
    role: str | None


def latest_ingested_sha(conn: Connection) -> str | None:
    """The most-recently-ingested snapshot sha (ingest order, not git topology — DESIGN.md §10)."""
    stmt = (
        select(m.commit_ref.c.sha)
        .join(m.snapshot_entry, m.snapshot_entry.c.sha == m.commit_ref.c.sha)
        .order_by(m.commit_ref.c.ingested_at.desc(), m.commit_ref.c.sha)
        .limit(1)
    )
    return cast("str | None", conn.execute(stmt).scalar())


def latest_sha_for_logical_key(conn: Connection, logical_key: str) -> str | None:
    """The most-recently-ingested sha whose snapshot contains ``logical_key`` (drives freshness)."""
    stmt = (
        select(m.commit_ref.c.sha)
        .select_from(
            m.snapshot_entry.join(m.commit_ref, m.commit_ref.c.sha == m.snapshot_entry.c.sha)
        )
        .where(m.snapshot_entry.c.logical_key == logical_key)
        .order_by(m.commit_ref.c.ingested_at.desc(), m.commit_ref.c.sha)
        .limit(1)
    )
    return cast("str | None", conn.execute(stmt).scalar())


def spans_at_location(conn: Connection, file: str, line: int, sha: str) -> list[SpanHitRow]:
    """Spans whose occurrence at ``sha`` covers ``file:line`` (1-based)."""
    join = m.code_span.join(m.span_occurrence, m.span_occurrence.c.span_id == m.code_span.c.span_id)
    rows = conn.execute(
        select(
            m.code_span.c.span_id,
            m.code_span.c.span_kind,
            m.code_span.c.fq_symbol_path,
            m.span_occurrence.c.file_path,
            m.span_occurrence.c.start_line,
            m.span_occurrence.c.end_line,
        )
        .select_from(join)
        .where(
            m.span_occurrence.c.file_path == file,
            m.span_occurrence.c.sha == sha,
            m.span_occurrence.c.start_line <= line,
            m.span_occurrence.c.end_line >= line,
        )
        .order_by(
            m.span_occurrence.c.start_line,
            m.span_occurrence.c.end_line.desc(),
            m.code_span.c.fq_symbol_path,
        )
    ).all()
    return [SpanHitRow(*row) for row in rows]


def artifacts_grounded_on_span(
    conn: Connection, sha: str, span_id: bytes
) -> list[GroundedArtifactRow]:
    """Artifacts in ``sha``'s snapshot grounded on ``span_id``, each with its grounding role."""
    join = m.snapshot_entry.join(
        m.artifact, m.artifact.c.artifact_id == m.snapshot_entry.c.artifact_id
    ).join(
        m.artifact_derived_from,
        m.artifact_derived_from.c.artifact_id == m.artifact.c.artifact_id,
    )
    rows = conn.execute(
        select(
            m.artifact.c.logical_key,
            m.artifact.c.kind,
            m.artifact.c.payload,
            m.artifact.c.is_deterministic,
            m.artifact.c.confidence,
            m.artifact_derived_from.c.role,
        )
        .select_from(join)
        .where(m.snapshot_entry.c.sha == sha, m.artifact_derived_from.c.span_id == span_id)
        .order_by(m.artifact.c.logical_key)
    ).all()
    return [GroundedArtifactRow(*row) for row in rows]


def provenance_for_artifact(conn: Connection, sha: str, logical_key: str) -> list[ProvenanceRow]:
    """Grounding occurrences (file:line@sha + role) for the ``(sha, logical_key)`` artifact."""
    join = m.snapshot_entry.join(
        m.artifact_derived_from,
        m.artifact_derived_from.c.artifact_id == m.snapshot_entry.c.artifact_id,
    ).join(
        m.span_occurrence,
        and_(
            m.span_occurrence.c.span_id == m.artifact_derived_from.c.span_id,
            m.span_occurrence.c.sha == sha,
        ),
    )
    rows = conn.execute(
        select(
            m.span_occurrence.c.file_path,
            m.span_occurrence.c.start_line,
            m.span_occurrence.c.end_line,
            m.artifact_derived_from.c.role,
        )
        .select_from(join)
        .where(m.snapshot_entry.c.sha == sha, m.snapshot_entry.c.logical_key == logical_key)
        .order_by(
            m.span_occurrence.c.file_path,
            m.span_occurrence.c.start_line,
            m.artifact_derived_from.c.role,
        )
    ).all()
    return [ProvenanceRow(*row) for row in rows]


@dataclass(frozen=True)
class ArtifactSpanRow:
    span_id: bytes
    fq_symbol_path: str
    raw_text: str  # the span's source text at this sha (input + ground-truth for the LLM describer)


def spans_for_artifact(conn: Connection, sha: str, logical_key: str) -> list[ArtifactSpanRow]:
    """The grounding spans of the ``(sha, logical_key)`` artifact, with id + fq path + source text.

    Feeds the LLM-grounded describer: the spans are both the prompt context and the deterministic
    ground truth its claims are validated against (DESIGN.md §9).
    """
    join = (
        m.snapshot_entry.join(
            m.artifact_derived_from,
            m.artifact_derived_from.c.artifact_id == m.snapshot_entry.c.artifact_id,
        )
        .join(m.code_span, m.code_span.c.span_id == m.artifact_derived_from.c.span_id)
        .join(
            m.span_occurrence,
            and_(
                m.span_occurrence.c.span_id == m.artifact_derived_from.c.span_id,
                m.span_occurrence.c.sha == sha,
            ),
        )
    )
    rows = conn.execute(
        select(
            m.code_span.c.span_id,
            m.code_span.c.fq_symbol_path,
            m.span_occurrence.c.raw_text,
        )
        .select_from(join)
        .where(m.snapshot_entry.c.sha == sha, m.snapshot_entry.c.logical_key == logical_key)
        .order_by(m.code_span.c.fq_symbol_path)
    ).all()
    return [ArtifactSpanRow(r.span_id, r.fq_symbol_path, r.raw_text) for r in rows]


@dataclass(frozen=True)
class ModuleTarget:
    module: str  # the file's module span fq path (e.g. "app.schemas")
    file_path: str
    spans: list[ArtifactSpanRow]  # ALL of the file's spans (module + classes/functions/imports)


def module_targets(conn: Connection, sha: str) -> list[ModuleTarget]:
    """First-party modules in ``sha``'s snapshot, each with all of its spans.

    A module is not an artifact, so it is enumerated from its span occurrences at ``sha`` (which are
    first-party-only — the pipeline parses solely files under the first-party root). Feeds the
    LLM-grounded module describer: the file's spans are both the prompt context and the
    deterministic ground truth its claims are validated against (DESIGN.md §9).
    """
    join = m.code_span.join(m.span_occurrence, m.span_occurrence.c.span_id == m.code_span.c.span_id)
    rows = conn.execute(
        select(
            m.code_span.c.span_id,
            m.code_span.c.span_kind,
            m.code_span.c.fq_symbol_path,
            m.span_occurrence.c.raw_text,
            m.span_occurrence.c.file_path,
        )
        .select_from(join)
        .where(m.span_occurrence.c.sha == sha)
        .order_by(m.span_occurrence.c.file_path, m.code_span.c.fq_symbol_path)
    ).all()
    by_file: dict[str, list[Any]] = {}
    for row in rows:
        by_file.setdefault(row.file_path, []).append(row)
    targets: list[ModuleTarget] = []
    for file_path, file_rows in by_file.items():
        module = next(
            (r.fq_symbol_path for r in file_rows if r.span_kind == "module"),
            file_rows[0].fq_symbol_path,
        )
        spans = [ArtifactSpanRow(r.span_id, r.fq_symbol_path, r.raw_text) for r in file_rows]
        targets.append(ModuleTarget(module=module, file_path=file_path, spans=spans))
    return targets


@dataclass(frozen=True)
class PackageTarget:
    package: str  # the package's dotted name (e.g. "app", "kb.store")
    init_file_path: str  # the package's __init__.py path
    member_modules: list[str]  # the package module itself + its DIRECT-child modules
    spans: list[ArtifactSpanRow]  # grounding spans of those member modules' files


def _parent_module(module: str) -> str:
    return module.rsplit(".", 1)[0] if "." in module else ""


def package_targets(conn: Connection, sha: str) -> list[PackageTarget]:
    """First-party packages (``__init__.py`` files) in ``sha``'s snapshot, each grounded on its own
    and its DIRECT-child modules' spans.

    A package is identified by an ``__init__.py`` file; its name is that file's module-span fq path
    (``app/__init__.py`` -> ``app``). The grounding set is the package module plus modules ``M``
    where ``parent(M) == P`` — direct children only, NOT the whole subtree, so a root package's
    stays bounded (grandchildren are covered by their own nearer package overview). Feeds the
    per-package architecture-overview describer (DESIGN.md §9, §11).
    """
    modules = module_targets(conn, sha)
    by_module = {mt.module: mt for mt in modules}
    all_modules = set(by_module)
    targets: list[PackageTarget] = []
    for mt in modules:
        if mt.file_path.rsplit("/", 1)[-1] != "__init__.py":
            continue
        package = mt.module
        children = sorted(
            mod for mod in all_modules if mod != package and _parent_module(mod) == package
        )
        members = [package, *children]
        spans: list[ArtifactSpanRow] = []
        for mod in members:
            member = by_module.get(mod)
            if member is not None:
                spans.extend(member.spans)
        spans.sort(key=lambda s: s.fq_symbol_path)
        targets.append(
            PackageTarget(
                package=package, init_file_path=mt.file_path, member_modules=members, spans=spans
            )
        )
    targets.sort(key=lambda t: t.package)
    return targets


def is_sha_indexed(conn: Connection, sha: str) -> bool:
    """True if ``sha`` has a snapshot manifest (>= 1 ``snapshot_entry``) — the witness of a real
    index. ``commit_ref`` / ``span_occurrence`` rows are written even by partial runs, so they are
    not reliable witnesses; the manifest is (DESIGN.md §7)."""
    stmt = select(m.snapshot_entry.c.logical_key).where(m.snapshot_entry.c.sha == sha).limit(1)
    return conn.execute(stmt).first() is not None


def reusable_spans(
    conn: Connection, sha: str, file_paths: Collection[str]
) -> list[tuple[str, ParsedSpan]]:
    """Reconstruct ``(file_path, ParsedSpan)`` for ``file_paths`` from the snapshot at ``sha``.

    Feeds the incremental indexer: a file unchanged since the parent commit needs no re-parse — its
    spans are rebuilt from ``code_span ⋈ span_occurrence`` (identity + per-SHA location) and an
    occurrence is written at the child SHA. Only spans at the current ``NORMALIZATION_VERSION`` are
    returned, so a normalization bump forces a fresh parse rather than reusing stale identities.
    """
    if not file_paths:
        return []
    join = m.code_span.join(m.span_occurrence, m.span_occurrence.c.span_id == m.code_span.c.span_id)
    rows = conn.execute(
        select(
            m.span_occurrence.c.file_path,
            m.code_span.c.span_kind,
            m.code_span.c.fq_symbol_path,
            m.code_span.c.structural_fingerprint,
            m.code_span.c.span_id,
            m.code_span.c.lang,
            m.span_occurrence.c.start_byte,
            m.span_occurrence.c.end_byte,
            m.span_occurrence.c.start_line,
            m.span_occurrence.c.end_line,
            m.span_occurrence.c.raw_text,
        )
        .select_from(join)
        .where(
            m.span_occurrence.c.sha == sha,
            m.span_occurrence.c.file_path.in_(list(file_paths)),
            m.code_span.c.normalization_version == NORMALIZATION_VERSION,
        )
    ).all()
    return [
        (
            r.file_path,
            ParsedSpan(
                span_kind=r.span_kind,
                fq_symbol_path=r.fq_symbol_path,
                structural_fingerprint=bytes(r.structural_fingerprint),
                span_id=bytes(r.span_id),
                lang=r.lang,
                start_byte=r.start_byte,
                end_byte=r.end_byte,
                start_line=r.start_line,
                end_line=r.end_line,
                raw_text=r.raw_text,
            ),
        )
        for r in rows
    ]


def _like_literal(value: str, suffix: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + suffix


def resolve_target_logical_keys(conn: Connection, sha: str, target: str) -> list[str]:
    """Resolve ``target`` to logical keys: exact, then prefix, then file, then module."""

    def _scalars(stmt: Any) -> list[str]:
        return [str(value) for value in conn.execute(stmt).scalars()]

    exact = _scalars(
        select(m.snapshot_entry.c.logical_key).where(
            m.snapshot_entry.c.sha == sha, m.snapshot_entry.c.logical_key == target
        )
    )
    if exact:
        return exact

    prefix = _scalars(
        select(m.snapshot_entry.c.logical_key)
        .where(
            m.snapshot_entry.c.sha == sha,
            m.snapshot_entry.c.logical_key.like(_like_literal(target, "%"), escape="\\"),
        )
        .distinct()
        .order_by(m.snapshot_entry.c.logical_key)
    )
    if prefix:
        return prefix

    by_file = _scalars(
        select(m.snapshot_entry.c.logical_key)
        .select_from(
            m.snapshot_entry.join(
                m.artifact_derived_from,
                m.artifact_derived_from.c.artifact_id == m.snapshot_entry.c.artifact_id,
            ).join(
                m.span_occurrence,
                and_(
                    m.span_occurrence.c.span_id == m.artifact_derived_from.c.span_id,
                    m.span_occurrence.c.sha == sha,
                ),
            )
        )
        .where(m.snapshot_entry.c.sha == sha, m.span_occurrence.c.file_path == target)
        .distinct()
        .order_by(m.snapshot_entry.c.logical_key)
    )
    if by_file:
        return by_file

    by_module = _scalars(
        select(m.snapshot_entry.c.logical_key)
        .select_from(
            m.snapshot_entry.join(
                m.artifact_derived_from,
                m.artifact_derived_from.c.artifact_id == m.snapshot_entry.c.artifact_id,
            ).join(m.code_span, m.code_span.c.span_id == m.artifact_derived_from.c.span_id)
        )
        .where(
            m.snapshot_entry.c.sha == sha,
            or_(
                m.code_span.c.fq_symbol_path == target,
                m.code_span.c.fq_symbol_path.like(_like_literal(target, ".%"), escape="\\"),
            ),
        )
        .distinct()
        .order_by(m.snapshot_entry.c.logical_key)
    )
    return by_module


def units_for_logical_keys(
    conn: Connection, sha: str, logical_keys: Sequence[str]
) -> list[GroundedArtifactRow]:
    """Batch-fetch artifact rows for ``logical_keys`` in the snapshot (unit-level role=None)."""
    if not logical_keys:
        return []
    join = m.snapshot_entry.join(
        m.artifact, m.artifact.c.artifact_id == m.snapshot_entry.c.artifact_id
    )
    rows = conn.execute(
        select(
            m.artifact.c.logical_key,
            m.artifact.c.kind,
            m.artifact.c.payload,
            m.artifact.c.is_deterministic,
            m.artifact.c.confidence,
        )
        .select_from(join)
        .where(
            m.snapshot_entry.c.sha == sha,
            m.snapshot_entry.c.logical_key.in_(list(logical_keys)),
        )
        .order_by(m.artifact.c.logical_key)
    ).all()
    return [
        GroundedArtifactRow(
            r.logical_key, r.kind, r.payload, r.is_deterministic, r.confidence, None
        )
        for r in rows
    ]


def similar_artifacts_by_embedding(
    conn: Connection, sha: str, query_embedding: Sequence[float], k: int
) -> list[GroundedArtifactRow]:
    """Top-k artifacts in ``sha``'s snapshot by cosine distance to ``query_embedding``.

    ``cosine_distance`` is a pgvector comparator not in SQLAlchemy's stubs, so it's called through a
    cast (which also binds the list as a ``vector``, unlike a raw ``op("<=>")``).
    """
    join = m.snapshot_entry.join(
        m.artifact, m.artifact.c.artifact_id == m.snapshot_entry.c.artifact_id
    )
    distance = cast(Any, m.artifact.c.embedding).cosine_distance(list(query_embedding))
    rows = conn.execute(
        select(
            m.artifact.c.logical_key,
            m.artifact.c.kind,
            m.artifact.c.payload,
            m.artifact.c.is_deterministic,
            m.artifact.c.confidence,
        )
        .select_from(join)
        .where(m.snapshot_entry.c.sha == sha, m.artifact.c.embedding.is_not(None))
        .order_by(distance)
        .limit(k)
    ).all()
    return [
        GroundedArtifactRow(
            r.logical_key, r.kind, r.payload, r.is_deterministic, r.confidence, None
        )
        for r in rows
    ]
