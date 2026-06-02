"""Populate artifact embeddings for a snapshot — a separate pass from ``kb index`` (DESIGN.md §10).

Idempotent: only (re-)embeds artifacts that have no embedding or were embedded with a different
model. Embeddings are shared across snapshots (artifacts are content-addressed), so embedding once
benefits every snapshot the artifact appears in.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Engine, or_, select, update

from kb.embed.providers import EmbeddingProvider
from kb.embed.text import embed_text
from kb.store import models as m


@dataclass(frozen=True)
class EmbedResult:
    sha: str
    embedded: int


def embed_snapshot(engine: Engine, sha: str, provider: EmbeddingProvider) -> EmbedResult:
    """Embed snapshot artifacts lacking an up-to-date embedding for ``provider.model_id``."""
    join = m.snapshot_entry.join(
        m.artifact, m.artifact.c.artifact_id == m.snapshot_entry.c.artifact_id
    )
    with engine.begin() as conn:
        rows = conn.execute(
            select(m.artifact.c.artifact_id, m.artifact.c.kind, m.artifact.c.payload)
            .select_from(join)
            .where(
                m.snapshot_entry.c.sha == sha,
                or_(
                    m.artifact.c.embedding.is_(None),
                    m.artifact.c.embedding_model_id.is_distinct_from(provider.model_id),
                ),
            )
        ).all()
        if not rows:
            return EmbedResult(sha=sha, embedded=0)
        vectors = provider.embed([embed_text(r.kind, r.payload) for r in rows])
        for row, vector in zip(rows, vectors, strict=True):
            conn.execute(
                update(m.artifact)
                .where(m.artifact.c.artifact_id == row.artifact_id)
                .values(embedding=vector, embedding_model_id=provider.model_id)
            )
    return EmbedResult(sha=sha, embedded=len(rows))
