# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.7.0] - 2026-07-08

### Added

- **Describe slices 5 & 6** (`kb describe`): **event-handler descriptions** (`DESCRIBE_KINDS`
  gained `event_handler`; grounding = the handler span plus its owner/listened-to classes,
  cross-file; a dedicated `_EVENT_FACTS_CAP` because registration payloads overflow the default
  facts budget) and the **whole-repo overview** — one `desc:repo` description per snapshot
  (`target_kind="repo"`), grounded on the **bounded top-level surface** (top-level plain modules
  plus each top-level package's own bounded grounding set — its `__init__` + direct children, the
  `package_targets` precedent; grandchildren are covered by their own nearer package overviews).
  It runs last in the describe pass and synthesizes the just-written package overviews,
  cross-package import edges, external dependencies, and artifact counts as *context*
  (`_repo_facts`, `_REPO_FACTS_CAP`), while claims still validate against the top-level code
  spans. New pure `store.queries.repo_target`. Route/entity/module/package/process prompts stay
  **byte-identical** (no `PROMPT_VERSION` bump). The `semantic_grounding_test` gate gains three
  tests (event-handler cross-file provenance; exactly one repo overview grounded on exactly the
  top surface; a grandchild module provably never grounds the repo overview); headline HARD gates
  stay **fifteen**.
- **ADR-candidate mining from git history — slice 1** (`kb mine`,
  `kb.extract.semantic.mine`): a new key-gated LLM pass (separate from `kb index`, mirror of
  `kb describe`) that walks the local first-parent history from the latest indexed commit
  (`--sha` / `--max-commits` / `--force`) and extracts the *decision* each commit records from
  its message plus its changed code. The D5 grounding bridge: each `decision` artifact
  (`decision:{sha}`, one per mined commit — deterministic, no LLM slugs) is grounded on the
  spans its source commit **changed** (present at the commit, absent at its first parent; role
  `changed`; root commits mine against the empty tree), so prose-born knowledge still satisfies
  the ≥ 1-code-span invariant. The commit message is stored *verbatim* in the payload as a fact
  (immutable, pinned by the sha) — context, never grounding. Claims pass the same deterministic
  `validate_claims` floor as `kb describe` (fabricated claims dropped; no survivors → nothing
  stored), `confidence = kept/(kept+dropped)`, trust stays LOW (DESIGN §4). Merge commits are
  skipped (PR-description mining is a future network slice), unindexed commits are skipped,
  docs-only commits never pay an LLM call, oversized grounding sets are capped with a retro-flag
  (`grounding_capped`), and re-running skips already-mined commits (LLM-cost idempotency; one
  transaction per commit, crash-safe). New `store.queries.changed_span_rows`;
  `summarize`/`embed_text` gained a `decision` branch; `make_git_repo` fixtures accept
  per-commit `messages`.
- **ADR-mining HARD gate** (`kb.eval.adr_mining_test`): stub-LLM (offline, no API key),
  four-commit fixture with decision-bearing messages — grounded claim kept / adversarial
  fabricated claim dropped; provenance limited to the commit's touched files; root commit
  grounded on the whole initial tree; a commit whose changed code backs no claim stores
  nothing; docs-only commit provably skips the LLM; merges skipped; re-mining idempotent
  (same artifact ids, stored decisions never re-billed). Headline HARD gates: fourteen →
  **fifteen**.

## [0.6.0] - 2026-07-07

### Added

- **LLM labeling of process paths** (`kb describe`, 4th slice): `DESCRIBE_KINDS` gained
  `process_path`, so the key-gated describe pass now writes a grounded NL name/summary for each
  materialized business-process path, with a larger facts budget (the path payload —
  steps/edges/sink — is rich context). Claims are span-validated against the path's own spans
  (fabricated claims dropped; a path with no surviving claim stores nothing) and the stored
  description carries `confidence = kept/(kept+dropped)` < 1.0 — the honest pricing of the call
  graph's unknown-unknowns promised in DESIGN §9. Route/entity/module/package prompts stay
  byte-identical (no `PROMPT_VERSION` bump). The `semantic_grounding_test` gate gains a
  process-path fixture (multi-file path via the fixture's own `.kb/sinks.yaml`; real claim kept /
  fabricated claim dropped); headline HARD gates stay **fourteen**. Closes DESIGN §14 item 2.
- **Deterministic business-process path extractor** (`kb.extract.deterministic.paths`): the first
  SECOND-ORDER extractor — `ExtractContext` gained `prior_artifacts` (the pipeline feeds each
  extractor's outputs to later ones; registration order is load-bearing) and the pipeline now
  materializes the analyzed repo's `.kb/` directory alongside the Python tree. `PathEngine` (the
  first shipped increment of the DESIGN §11 seam) BFS-slices the `call_edge` graph from extracted
  entrypoints (`api_route` / `event_handler` handlers) to functions containing SINK calls — matched
  textually against a built-in registry (db/http/email/subprocess/file/queue) merged with an
  optional per-repo `.kb/sinks.yaml` override (strictly validated; the effective registry's digest
  is folded into artifact identity). One `process_path` per (entrypoint, sink, terminal): shortest
  chain, cycle-safe, 0-hop supported, depth/path caps retro-flagged in `limitations` (never
  silent), grounded on EVERY span along the path (roles `entrypoint`/`step`/`terminal` —
  multi-file provenance). Found paths are exact (`confidence` 1.0); incompleteness is an explicit
  payload fact. `pyyaml` moved to runtime deps; `summarize`/`embed_text` gained `process_path`
  branches. The LLM labeler ships in the entry above.
- **Tier-1 process-paths HARD gate** (`kb.eval.tier1_processes_test`): hand-labeled oracle (2-hop
  cross-file chain, 0-hop direct sink, event-handler entrypoint), the flagship artifact grounded
  across **three files**, cycle no-hang, `.kb/sinks.yaml` override proven end-to-end, no-sink route
  yields nothing, depth caps honored, re-index determinism. Headline HARD gates: thirteen →
  **fourteen**.
- **Call-graph edge extractor** (`kb.extract.deterministic.calls`): the sixth deterministic
  extractor — one `call_edge` artifact per RESOLVED caller→callee pair (`call:{caller}->{callee}`,
  call-site lines aggregated in the payload, the import-edge precedent). Three deterministic
  resolution tiers: same-module calls, imported-name calls (**cross-file** — `from x import f
  [as g]` and `import x[.y] [as z]; x.y.f()` module-attribute forms, resolved via per-module import
  tables incl. relative imports), and `self.method()` to a method of the same class. Precision-first:
  only first-party-resolved edges are emitted; `obj.method(...)`, `getattr`/dynamic, `super()`,
  inherited self-calls, star imports, decorator/default-arg expressions and class-body calls are
  documented gaps. Grounded on the caller def span (role `caller`; module span for module-level
  calls) + the callee def span (role `callee`); direct recursion is a single-span artifact; mutual
  recursion yields two distinct artifacts (identity rule v2). `framework_versions` empty. Registered
  in `default_extractors()`; `summarize` / `embed_text` gained a `call_edge` branch. The
  deterministic foundation under the future business-process extractor (DESIGN §14 item 2).
- **Tier-1 call-graph HARD gate** (`kb.eval.tier1_calls_test`): extracted edges match a hand-labeled
  oracle (11 edges across 5 modules); a mutually recursive pair stays two distinct artifacts over an
  identical evidence span set (extractor-level identity-v2 regression); cross-file provenance (one
  artifact spans the caller's and callee's files); six blind spots asserted as *known* gaps; nested
  defs attribute to the innermost function. Headline HARD gates: twelve → **thirteen**.

## [0.5.0] - 2026-07-07

### Fixed

- **Silent artifact_id collision for same-evidence artifacts**: `artifact_id` (identity rule v1)
  hashed the grounding span set + extractor identity but NOT the `logical_key`, so two different
  knowledge units of one extractor sharing their entire evidence set collided into one digest —
  e.g. mutually referencing entities (`Order.items: list[LineItem]` ↔ `LineItem.order: Order`)
  produced ONE artifact and the second logical key silently served the first payload. The same
  latent class affected FastAPI routes stacked on one handler+response-model and would have blocked
  the call-graph extractor (mutual recursion). Adversarial regression added to the Tier-1 entities
  gate (mutual cross-file refs → two distinct, correctly-payloaded, cross-file-grounded artifacts)
  plus identity-rule assertions in the invariants gate.

### Changed

- **Artifact identity rule v2** (`kb.ids`, per the [LOCKED] block's own evolution protocol):
  `logical_key` joined the `artifact_id` hash, versioned by a new `ARTIFACT_ID_VERSION` constant
  (the artifact-side mirror of `NORMALIZATION_VERSION`) — extractor versions stay semantic.
  Same-input reproducibility and cross-branch dedup are unaffected. **All artifact ids change**:
  existing databases should be re-indexed (`kb index` rewrites snapshots idempotently; superseded
  artifact rows are orphaned — GC remains deferred, harmless).

### Added

- **`kb watch` — incremental re-index daemon** (`kb.daemon.watch`), completing the "incremental
  re-index on git push" roadmap item: polls a **local** branch ref (pygit2; no network, no
  credentials — pair with a bare repo receiving pushes, a cron `git pull`, or a CI step with
  `--once`) and indexes every new first-parent commit incrementally, one by one (`--max-catchup`
  guard, default 50; beyond it — or after a force-push/rewind — a single incremental index of the
  new head against the recorded cursor). The resume point lives in the previously-unused
  `branch_ref` table and advances after **each** indexed commit, so a crash resumes where it left
  off. `--interval` polling loop, Ctrl-C clean stop; failed ticks are logged and retried (`--once`
  exits non-zero for cron). New `git.repo.branch_head_sha` / `first_parent_chain`,
  `store.queries.branch_head`, `store.writer.upsert_branch_ref`; `kb index` and `kb watch` share
  `default_extractors()`. One database tracks one repository. Watch tests are supporting suite —
  headline HARD gates stay **twelve** (the incremental core itself is gate #12).

- **Event-handler extractor** (`kb.extract.deterministic.events`): a fifth deterministic extractor
  emitting one `event_handler` artifact per handler function/method that carries decorator
  registrations — pydantic `@field_validator` / `@model_validator`, FastAPI `@app.on_event`, and
  SQLAlchemy `@event.listens_for`. All of a handler's registrations (stacked decorators included)
  live in `payload.registrations` — one artifact per handler, because `artifact_id` is
  content-addressed over the grounding spans + extractor identity and same-handler registrations
  share their evidence. Grounded on the handler span (role `handler`; the span includes its
  decorators), the enclosing pydantic model (role `owner_class`), and every resolved listened-to
  class (role `target_class`, **cross-file** — the `response_model` precedent). Known gaps flagged
  or skipped, never guessed: call-form `event.listen(...)`, FastAPI lifespan, pydantic-v1
  `@validator`, dynamic event/field names. Registered in `kb index`; `summarize` / `embed_text`
  gained an `event_handler` branch. Fully static — never imports user code.
- **Tier-1 events HARD gate** (`kb.eval.tier1_events_test`): extracted handlers match a hand-labeled
  oracle (stacked `listens_for` decorators included); a SQLAlchemy listener is grounded cross-file on
  the class it listens to; a non-first-party target is grounded-but-flagged; the call-form
  `event.listen(...)` and a dynamic `@app.on_event(EVENT)` name are asserted as *known* gaps.
  Headline HARD gates: eleven → **twelve**.

## [0.4.0] - 2026-06-29

### Changed

- **README branding** — added a hero banner and brand assets under `assets/` (`hero.png`, `logo.png`,
  `logo.svg`, `social-preview.png`, `avatar.png`) and recolored the Mermaid diagrams (README + DESIGN)
  to the brand palette (cream / deep-green / gold). Source renders live in a git-ignored `design/`
  folder; only optimized, renamed copies are published. (Repo social preview + avatar are uploaded
  manually in GitHub Settings.)

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

[Unreleased]: https://github.com/v0ropaev/knowbase/compare/v0.7.0...HEAD
[0.7.0]: https://github.com/v0ropaev/knowbase/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/v0ropaev/knowbase/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/v0ropaev/knowbase/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/v0ropaev/knowbase/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/v0ropaev/knowbase/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/v0ropaev/knowbase/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/v0ropaev/knowbase/releases/tag/v0.1.0
