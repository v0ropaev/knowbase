# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- (nothing yet)

## [0.2.0] - 2026-06-02

Spine plus the first **knowledge extractors**, **MCP serving**, and the **knowledge-vs-RAG** gate.
Everything still grounds what it claims (≥ 1 `file:line@sha` span); LLMs and embeddings remain
replaceable adapters.

### Added

- **FastAPI API-contract extractor** (`kb.extract.deterministic.fastapi_contract`): a fully static
  (tree-sitter) extractor that produces `api_route` artifacts grounded **across files** — the handler
  span in `routes.py` plus the `response_model` class span in `schemas.py`. Never imports user code.
- **Sandboxed introspect oracle** (`kb.introspect`, `kb introspect`): runs a FastAPI app in a
  network-blocked subprocess and emits its `app.openapi()` as JSON. Eval-only ground truth for the API
  gate; never on the index path.
- **Read-only MCP server** (`kb.mcp`, `kb serve`): `find_provenance`, `get_knowledge`, and
  `search_knowledge` over stdio; every unit carries provenance + extraction method + confidence +
  freshness.
- **pgvector embeddings + semantic search** (`kb.embed`, `kb embed`): a replaceable `EmbeddingProvider`
  (sentence-transformers `all-MiniLM-L6-v2` default, OpenAI optional via `KB_EMBED_PROVIDER`) populated
  in a separate, idempotent pass into `artifact.embedding vector(384)`. Torch is isolated behind the
  `embed` extra and a lazy import — off the index path.
- **Frozen RAG-over-source baseline** (`kb.rag.baseline`) and the **Tier-3 knowledge-vs-RAG recall
  gate**: knowbase cross-file recall@k == 1.0 (a structural floor); the RAG arm is tracked, never
  asserted.
- **Migration `0002_embeddings_rag`**: `artifact.embedding` / `embedding_model_id` columns, HNSW
  indexes, and the `rag_chunk` table.

### Changed

- CI eval gates: five → **seven** (added the Tier-1 API oracle and the Tier-3 knowledge-vs-RAG recall
  gate).
- CI Postgres service image: `postgres:17` → `pgvector/pgvector:pg17`; install uses
  `uv sync --extra dev --extra embed` with a cached embedding model.

## [0.1.0] - 2026-05-30

First MVP: the **provenance spine**. Nothing is stored unless it is bound to at least one exact
code span (`file:line@sha`). The same mechanism delivers anti-hallucination, incremental update via
git diff, and consumer trust (every served unit carries provenance, method, confidence, and
freshness).

### Added

- **Structural identity** (`kb.ids`, `kb.structural`): content-addressed identity hashing with a
  locked scheme — `span_id` excludes the file path and byte offsets. Spans are extracted with
  tree-sitter and fingerprinted as a normalized S-expression (named nodes only; comments and
  docstrings dropped; identifiers and literals kept). Location is tracked per SHA. Normalization
  rules are versioned via `NORMALIZATION_VERSION`.
- **Single-PostgreSQL store** (`kb.store`): one PostgreSQL database managed with Alembic;
  content-addressed, idempotent writes; the `>= 1 derived_from` anti-hallucination invariant
  enforced both in-app and by a deferred constraint trigger.
- **Git ingest** (`kb.git`): pygit2-based reading of blobs at a given SHA with no working-tree
  checkout, plus a diff-based invalidation seed for incremental updates.
- **Deterministic import extractor** (`kb.extract.deterministic.imports`): import/dependency edges
  from tree-sitter spans grounded by line, with edge resolution via grimp.
- **CLI** (`kb.daemon.cli`): the `kb` command, with `kb index` for ingesting a repo at a SHA into a
  Postgres store (`serve` and `introspect` are stubs for the next milestone).
- **Five CI eval gates** (`kb.eval`): identity reproducibility, adversarial grounding, the Tier-1
  import oracle, Tier-4 one-hop invalidation, and the store/provenance invariants — run in CI
  against a PostgreSQL 17 service.

[Unreleased]: https://github.com/v0ropaev/knowbase/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/v0ropaev/knowbase/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/v0ropaev/knowbase/releases/tag/v0.1.0
