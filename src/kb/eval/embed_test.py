"""Embedding provider + population: shape, determinism, idempotency (DESIGN.md §10)."""

from __future__ import annotations

import math
from pathlib import Path

from sqlalchemy import Engine, select

from kb.daemon.pipeline import index_commit
from kb.embed.population import embed_snapshot
from kb.eval._fixtures import make_git_repo
from kb.eval.tier1_api_test import FILES
from kb.extract.deterministic.fastapi_contract import FastAPIExtractor
from kb.store import models as m


def test_embed_returns_unit_vectors(st_provider) -> None:
    out = st_provider.embed(["hello world", "a second string"])
    assert len(out) == 2
    assert all(len(v) == st_provider.dimension == 384 for v in out)
    assert abs(math.sqrt(sum(x * x for x in out[0])) - 1.0) < 1e-3  # normalized


def test_embed_is_deterministic(st_provider) -> None:
    a = st_provider.embed(["stable text"])[0]
    b = st_provider.embed(["stable text"])[0]
    assert max(abs(x - y) for x, y in zip(a, b, strict=True)) < 1e-5


def test_embed_snapshot_idempotent(engine: Engine, tmp_path: Path, st_provider) -> None:
    sha = make_git_repo(tmp_path, [FILES])[0]
    index_commit(
        engine, str(tmp_path), sha, extractors=[FastAPIExtractor()], first_party_root="src"
    )

    first = embed_snapshot(engine, sha, st_provider)
    assert first.embedded > 0
    second = embed_snapshot(engine, sha, st_provider)
    assert second.embedded == 0  # nothing re-embedded for the same model

    join = m.snapshot_entry.join(
        m.artifact, m.artifact.c.artifact_id == m.snapshot_entry.c.artifact_id
    )
    with engine.connect() as conn:
        unembedded = conn.execute(
            select(m.artifact.c.artifact_id)
            .select_from(join)
            .where(m.snapshot_entry.c.sha == sha, m.artifact.c.embedding_model_id.is_(None))
        ).first()
    assert unembedded is None
