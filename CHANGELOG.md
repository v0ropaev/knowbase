# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Per-package architecture overviews** (`kb describe`, third slice): the key-gated describe pass now
  also writes a per-package overview (`target_kind="package"`, logical key `desc:package:<P>`) for each
  first-party package (an `__init__.py`). A package is grounded on its own and its **direct-child**
  modules' spans (bounded — not the whole subtree); the overview synthesizes **rich context** — the
  package's import edges (internal + cross-package), its `public_symbol` surface, and its member
  modules' description summaries — but claims still validate against the package's **code spans** via
  `grounding.validate_claims`, so provenance stays code-grounded. New `store.queries.package_targets`;
  `describe._build_prompt` gained a `facts_cap` (existing route/entity/module prompts unchanged — no
  `PROMPT_VERSION` bump, no re-describe churn). The semantic-grounding HARD gate is extended with the
  package path (adversarial claim dropped); headline HARD gates stay **eleven**.

- **Library public-API-surface extractor** (`kb.extract.deterministic.library_surface`): a fourth
  deterministic extractor emitting one `public_symbol` artifact per name a package exposes from its
  `__init__.py` — covering the libraries/SDK target alongside web/API + entities. The public surface
  is determined statically with tree-sitter (`__all__`-authoritative, else top-level non-underscore
  functions/classes; never imports/executes user code); `__init__` re-exports (`from .sub import X`)
  are resolved **cross-file** to the defining function/class span (role `definition`) and grounded
  additionally on the `__init__` import statement (role `re_export`). Third-party / dynamic-`__all__`
  / star re-exports are grounded-but-flagged in `payload.limitations`, never silently lost. Registered
  in `kb index`; `summarize` / `embed_text` gained a `public_symbol` branch.
- **Tier-1 library-surface HARD gate** (`kb.eval.library_surface_test`, `kb.eval._surface`): the
  extracted surface equals an INDEPENDENT **griffe** static oracle (a different engine; dev-only,
  offline, never on the index path — mirroring how fastapi powers the openapi oracle), with cross-file
  re-export grounding, underscore-private exclusion, and a flagged third-party re-export asserted as a
  known gap. New dev dependency `griffe>=1.5,<3`. Headline HARD gates: ten → **eleven**.

- **Incremental (diff-based) re-index** (`kb index --incremental` / `--parent <sha>`): `index_commit`
  can index a commit against an already-indexed parent — reusing the spans of files unchanged since
  the parent snapshot (rebuilt from the DB, no tree-sitter re-parse) and parsing only changed/new
  files. Extractors still run **fully** over the materialized tree (correct for cross-file
  grimp/fastapi/entities; idempotent writes make unchanged artifacts no-ops), so the snapshot is
  identical to a full re-index. The parent is auto-detected from `commit_ref.parent_shas` (first
  indexed one); an explicit unindexed `--parent` raises, and a missing parent or a first-party-root
  change falls back to a full index. New `store.queries.is_sha_indexed` / `reusable_spans`;
  `IndexResult` gains `mode` / `parsed_files` / `reused_files`. Hook-friendly:
  `kb index <repo> --sha <new> --parent <old>`.
- **Incremental re-index equivalence HARD gate** (`kb.eval.incremental_reindex_test`): proves an
  incremental re-index yields the same `{logical_key: artifact_id}` snapshot as a full re-index of
  the same tree (compared across two distinct SHAs in one database) and that the parse is skipped for
  unchanged files; plus full-fallback and unindexed-parent-raises cases. Headline HARD gates:
  nine → **ten**.

- **LLM-grounded semantic layer — first slice** (`kb.extract.semantic`, `kb describe`): an optional,
  key-gated pass (separate from `kb index`) has an LLM write a short NL summary + structured claims
  for each `api_route` / `entity` artifact in a snapshot. Every claim is validated against the
  artifact's own grounding spans by a **deterministic sub-property gate**
  (`grounding.validate_claims`) — claims citing a symbol not in the code are dropped, and a
  `description` artifact is stored only if something survives, grounded on the same spans
  (`extraction_method = "llm_grounded"`, `model_id` + `prompt_version` in the artifact key). Surfaced
  via MCP `get_knowledge` / `search_knowledge`. Uses `kb.llm` (Anthropic default, OpenAI optional).
- **Per-module descriptions** (`kb describe`, second slice): the same pass now also describes each
  first-party module (file). A module is not an artifact, so it is enumerated from its span
  occurrences (`store.queries.module_targets`) and grounded on **all** of the file's spans
  (module + classes/functions/imports); `target_kind="module"`, logical key `desc:module:<fqname>`.
  The same span-validation gate applies, so a module gets a description only if a cited symbol
  actually occurs in the file — no new invariants. The `semantic_grounding` HARD gate is extended
  with the module path (adversarial claim dropped; a module with no matching symbol gets nothing).
- **Semantic grounding HARD gate** (`kb.eval.semantic_grounding_test`): runs the describer on a
  **stub** LLM (no API key) and asserts an adversarial fabricated claim is dropped while the grounded
  claim is stored — on both the artifact and the module path — the DESIGN §9 semantic floor,
  enforced deterministically in CI. Headline HARD gates: eight → **nine**.

## [0.3.0] - 2026-06-21

### Added

- **Docker** (`Dockerfile`, `docker-compose.yml`, `.github/workflows/docker.yml`): a multi-stage,
  uv-based image published to GHCR (`ghcr.io/v0ropaev/knowbase`) — `:edge` from `master`, semver +
  `:latest` on `v*` tags, multi-arch amd64+arm64. The **slim** default image carries `index` /
  `migrate` / `serve` / `introspect`; the **`-embed`** tag adds CPU-torch for `kb embed` + search. A
  `docker compose` brings up a `pgvector` Postgres + the CLI for local dev/eval. CI build-validates on
  PRs (hadolint + build, no push) and publishes on master/tags.
- **`kb migrate`** CLI command: applies the Alembic schema to `head` (`--db-url` or `KB_DB_URL`).
- **Deterministic entities extractor** (`kb.extract.deterministic.entities`): a fully static
  (tree-sitter) extractor that emits one `entity` artifact per domain class — pydantic `BaseModel`,
  `@dataclass`, and SQLAlchemy declarative model — with its fields, grounded on the class-definition
  span **and, across files, on the first-party entities it references** (resolved from field-type
  annotations and SQLAlchemy `relationship()` targets; role `related_entity`). One `entity:Order`
  artifact then spans every file it depends on — the cross-file shape RAG-over-chunks misses.
  Detection signals and limits are recorded in the payload (transitive bases, imperative SQLAlchemy
  mapping, and `ForeignKey("table.col")` resolution are documented gaps, not silent losses);
  `framework_versions` (pydantic / sqlalchemy) is folded into the artifact key. Surfaced via MCP
  `get_knowledge`/`search_knowledge` (entity embed text enriched with field + related-entity names).
- **Tier-1 entities gate** (`kb.eval.tier1_entities_test`): a hand-labeled HARD gate — extracted
  entities + fields match the oracle, a bare declarative `Base` is not an entity, a `create_model(...)`
  model is asserted as a known gap, every entity is grounded on a `class` span, and a cross-file
  reference (`Cart` → `Order`) is grounded on both files. Brings the headline HARD gates to **eight**.
- **Tier-3 entity questions** (`kb.eval.questions`): the knowledge-vs-RAG A/B now also covers domain
  entities (a two-file `Order`/`LineItem` fixture), asserting knowbase cross-file recall@k == 1.0 for
  entity questions as well as API-contract questions.
- **Nightly LLM-judged A/B** (`kb.llm`, `kb.eval.tier3_llm_judge_test`, `.github/workflows/nightly-llm-ab.yml`):
  an optional, key-gated, **non-gating** answer-quality comparison. An answerer LLM answers each question
  from knowbase-grounded context vs RAG-over-source context; a judge LLM scores accuracy against
  hand-written `GOLD` references and flags hallucination (claims unsupported by that arm's context).
  `kb.llm.providers` mirrors the embed-provider pattern (Anthropic default, OpenAI optional, lazy imports
  via the new `llm` extra); the test self-skips without an API key and asserts only that the A/B ran
  (never the win); the nightly workflow uploads a metrics artifact. `RagHit` gained a `raw_text` field so
  the RAG arm can feed chunk text to the answerer.

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

[Unreleased]: https://github.com/v0ropaev/knowbase/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/v0ropaev/knowbase/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/v0ropaev/knowbase/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/v0ropaev/knowbase/releases/tag/v0.1.0
