"""Frozen RAG-over-source baseline: chunk → embed → cosine top-k (DESIGN.md §10).

A deterministic line-window chunker (no langchain/tiktoken — keeps the baseline dependency-light and
exactly reproducible). The thesis is structural: RAG must retrieve TWO separate source chunks across
files to answer a cross-file contract question, where one grounded artifact already spans both.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import Connection, Engine, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from kb.embed.providers import EmbeddingProvider
from kb.git import repo as gitrepo
from kb.store import models as m

# FROZEN baseline params — changing any of these is a baseline change; bump the version.
RAG_BASELINE_VERSION = "1"
CHUNK_LINES = 40
CHUNK_OVERLAP = 10


@dataclass(frozen=True)
class Chunk:
    file_path: str
    chunk_idx: int
    start_line: int  # 1-based, inclusive
    end_line: int
    raw_text: str


@dataclass(frozen=True)
class RagHit:
    file_path: str
    chunk_idx: int
    start_line: int
    end_line: int
    distance: float
    raw_text: str = ""  # the chunk's source text (for the LLM A/B answerer); empty unless selected


def chunk_source(file_path: str, source: str) -> list[Chunk]:
    """Line windows: ``CHUNK_LINES`` lines, ``CHUNK_OVERLAP`` overlap, 1-based (deterministic)."""
    lines = source.splitlines()
    step = CHUNK_LINES - CHUNK_OVERLAP
    chunks: list[Chunk] = []
    idx = start = 0
    while start < len(lines):
        window = lines[start : start + CHUNK_LINES]
        if not window:
            break
        chunks.append(Chunk(file_path, idx, start + 1, start + len(window), "\n".join(window)))
        idx += 1
        start += step
    return chunks


def index_rag_baseline(
    engine: Engine, repo_path: str, sha: str, provider: EmbeddingProvider
) -> int:
    """Chunk every first-party ``*.py`` file at ``sha``, embed, and upsert into ``rag_chunk``."""
    repo = gitrepo.open_repo(repo_path)
    chunks = [
        chunk
        for path, source in gitrepo.iter_python_files_at(repo, sha)
        for chunk in chunk_source(path, source.decode("utf-8", errors="replace"))
    ]
    if not chunks:
        return 0
    vectors = provider.embed([chunk.raw_text for chunk in chunks])
    with engine.begin() as conn:
        for chunk, vector in zip(chunks, vectors, strict=True):
            stmt = pg_insert(m.rag_chunk).values(
                sha=sha,
                file_path=chunk.file_path,
                chunk_idx=chunk.chunk_idx,
                start_line=chunk.start_line,
                end_line=chunk.end_line,
                raw_text=chunk.raw_text,
                embedding=vector,
                model_id=provider.model_id,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["sha", "file_path", "chunk_idx"],
                set_={"embedding": stmt.excluded.embedding, "model_id": stmt.excluded.model_id},
            )
            conn.execute(stmt)
    return len(chunks)


def rag_retrieve(
    conn: Connection, query: str, provider: EmbeddingProvider, sha: str, k: int
) -> list[RagHit]:
    """Top-k source chunks in ``sha`` by cosine distance to the embedded ``query``."""
    [vector] = provider.embed([query])
    distance = cast(Any, m.rag_chunk.c.embedding).cosine_distance(vector)
    rows = conn.execute(
        select(
            m.rag_chunk.c.file_path,
            m.rag_chunk.c.chunk_idx,
            m.rag_chunk.c.start_line,
            m.rag_chunk.c.end_line,
            distance.label("distance"),
            m.rag_chunk.c.raw_text,
        )
        .where(m.rag_chunk.c.sha == sha, m.rag_chunk.c.embedding.is_not(None))
        .order_by(distance)
        .limit(k)
    ).all()
    return [
        RagHit(r.file_path, r.chunk_idx, r.start_line, r.end_line, float(r.distance), r.raw_text)
        for r in rows
    ]
