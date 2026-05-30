"""Read-side queries: invalidation and (next push) MCP retrieval (DESIGN.md §6, §10)."""

from __future__ import annotations

from collections.abc import Collection

from sqlalchemy import Connection, select

from kb.store import models as m


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
