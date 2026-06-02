"""Embedding providers behind a thin adapter (DESIGN.md §1 — models are replaceable adapters).

Default is the LOCAL ``SentenceTransformerProvider`` (no API key, deterministic on CPU, used in CI).
``OpenAIEmbeddingProvider`` is an optional key-gated adapter that down-projects to the same 384-dim
column so both arms of the RAG comparison share one vector size. Heavy imports (torch / openai) are
lazy so importing this module is cheap and the ``kb index`` path never pulls them.
"""

from __future__ import annotations

import os
from typing import Protocol, cast, runtime_checkable

DEFAULT_DIMENSION = 384


@runtime_checkable
class EmbeddingProvider(Protocol):
    model_id: str
    dimension: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class SentenceTransformerProvider:
    """Local default: all-MiniLM-L6-v2, 384-dim, CPU, unit-normalized (deterministic per build)."""

    model_id = "st:all-MiniLM-L6-v2"
    dimension = DEFAULT_DIMENSION

    def __init__(self, model_name: str = "all-MiniLM-L6-v2", device: str = "cpu") -> None:
        from sentence_transformers import SentenceTransformer  # lazy: keeps torch off `kb index`

        self._model = SentenceTransformer(model_name, device=device)

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = self._model.encode(
            texts,
            convert_to_numpy=True,
            normalize_embeddings=True,  # unit vectors -> cosine ranking is stable & comparable
            batch_size=32,
            show_progress_bar=False,
        )
        return cast("list[list[float]]", vectors.astype("float32").tolist())


class OpenAIEmbeddingProvider:
    """Optional API adapter: text-embedding-3-small down-projected to ``dimensions`` (def 384)."""

    dimension = DEFAULT_DIMENSION

    def __init__(
        self, *, dimensions: int = DEFAULT_DIMENSION, model: str = "text-embedding-3-small"
    ) -> None:
        from openai import OpenAI  # lazy

        self._client = OpenAI()  # reads OPENAI_API_KEY
        self._model = model
        self._dimensions = dimensions
        self.model_id = f"openai:{model}@{dimensions}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        resp = self._client.embeddings.create(
            model=self._model, input=texts, dimensions=self._dimensions
        )
        ordered = sorted(resp.data, key=lambda d: d.index)  # defensive: return in input order
        return cast("list[list[float]]", [list(d.embedding) for d in ordered])


def default_provider() -> EmbeddingProvider:
    """Select the provider via ``KB_EMBED_PROVIDER`` in {"st","openai"}; default "st" (local)."""
    if os.environ.get("KB_EMBED_PROVIDER", "st").lower() == "openai":
        return OpenAIEmbeddingProvider()
    return SentenceTransformerProvider()
