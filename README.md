<div align="center">

<img src="assets/hero.png" alt="knowbase — a versioned, provenance-grounded knowledge layer over a codebase, served to humans and AI agents" width="820">

[![CI](https://github.com/v0ropaev/knowbase/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/v0ropaev/knowbase/actions/workflows/ci.yml)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](./LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Checked with mypy](https://img.shields.io/badge/mypy-strict-blue)](https://mypy-lang.org/)
[![GHCR image](https://img.shields.io/badge/ghcr.io-knowbase-2496ED?logo=docker&logoColor=white)](https://github.com/v0ropaev/knowbase/pkgs/container/knowbase)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://github.com/v0ropaev/knowbase/pulls)

</div>

> A versioned, provenance-grounded **knowledge layer** over a codebase — served to humans and AI agents. Not RAG-over-code.

knowbase turns a git repository into a **Knowledge Layer**: a queryable, git-versioned model of what a codebase *means* — its architecture, domain entities, API contracts, dependencies, events, and business processes — where **every fact is bound to the exact lines of code it came from** (`file:line@sha`).

The one thing that makes it different: it does not embed your code and hope. It **extracts** durable knowledge and grounds each unit in a real code span. LLMs and embeddings are *replaceable adapters* around that spine — swap the model, the knowledge and its provenance stay.

```mermaid
%%{init: {'theme':'base','themeVariables':{'background':'#FAF3E7','primaryColor':'#FAF3E7','primaryTextColor':'#071812','primaryBorderColor':'#1C4434','lineColor':'#3C5B4E','secondaryColor':'#EDE3D2','tertiaryColor':'#F3EAD9','textColor':'#1C4434','fontFamily':'ui-sans-serif, system-ui, sans-serif'}}}%%
flowchart LR
    subgraph usual["The usual pipeline — lossy, opaque"]
        direction LR
        R1[Repository] --> E1["Embeddings<br/>chunks, no provenance,<br/>drifts from HEAD"] --> A1[AI Agent]
    end
    subgraph kbflow["knowbase — grounded, versioned"]
        direction LR
        R2[Repository] --> X2["Knowledge Extraction<br/>deterministic + LLM adapters"] --> K2["Knowledge Layer<br/>provenance · method ·<br/>confidence · freshness"] --> A2[AI Agent]
    end
```

## Why

- **Code is implementation, not knowledge.** "What are the API contracts?", "which module owns billing?", "what invalidates this cache?" are not answered by reading one file — the answer is spread across the repo and lives in nobody's head.
- **Docs rot.** Hand-written architecture docs and diagrams drift from `HEAD` the moment they are merged. There is no mechanical link back to the code, so nothing tells you when they go stale.
- **Agents get fragments.** RAG-over-code retrieves nearest-neighbor chunks with no provenance and no notion of whether they still reflect the current commit. The model fills the gaps by guessing.

knowbase answers with **units of knowledge that are versioned against git and traceable to source** — or it does not answer at all.

## How it works — the provenance spine

The core invariant: **nothing is stored unless it is bound to ≥ 1 exact code span** (`file:line@sha`). That single rule buys three properties at once:

- **Anti-hallucination.** An ungrounded artifact is *not stored* — enforced both in-app (a `GroundingError` before any write) and in the database (a deferred `artifact_grounded_check` constraint trigger that fails the transaction at `COMMIT`). An extractor that cannot point at code cannot persist a claim.
- **Incremental update.** A `git diff` maps changed code to changed spans, which invalidates *exactly* the derived artifacts whose grounding moved — no over-invalidation, no stale survivors.
- **Consumer trust.** Every served unit carries its provenance, the method that produced it (deterministic vs. model), confidence, and freshness relative to the commit.

Identity is **content-addressed and location-free by construction**. A span's `span_id` is a `sha256` over `(normalization_version, lang, span_kind, fq_symbol_path, structural_fingerprint)` — no file path, no byte offsets. The structural fingerprint is a normalized S-expression of the tree-sitter parse (named nodes only; comments *and* docstrings dropped; identifiers and literals kept). So reformatting, moving a file, or editing a comment does **not** change identity; a real rename or a structural edit does. Location is recorded per-SHA in `span_occurrence`, separate from identity.

Artifacts are content-addressed the same way — over their byte-sorted, de-duplicated grounding spans plus the `logical_key` and `extractor_id`/`extractor_version` (and `prompt_version`/`model_id` for model-backed extractors). The `logical_key` is part of the identity so that two different knowledge units sharing their entire evidence set (mutually referencing entities, mutually recursive callers) can never collapse into one digest. Re-indexing the same commit reproduces the identical set of artifact ids.

The spine is a handful of content-addressed tables: each **artifact** carries ≥ 1 `derived_from` edge to a **code_span**, spans are located **per-SHA** in `span_occurrence`, and a per-SHA **snapshot** ties the grounded artifacts to a commit.

```mermaid
%%{init: {'theme':'base','themeVariables':{'background':'#FAF3E7','primaryColor':'#FAF3E7','primaryTextColor':'#071812','primaryBorderColor':'#1C4434','lineColor':'#3C5B4E','secondaryColor':'#EDE3D2','tertiaryColor':'#F3EAD9','textColor':'#1C4434','fontFamily':'ui-sans-serif, system-ui, sans-serif'}}}%%
flowchart TD
    AR["artifact<br/>knowledge unit (+ embedding)"]
    CS["code_span<br/>content-addressed id · location-free"]
    SO["span_occurrence<br/>file:line @ sha"]
    SE["snapshot_entry<br/>per-SHA manifest"]
    CM["commit_ref / branch_ref"]
    AR -->|"derived_from ≥1 (else rejected:<br/>GroundingError + DB trigger)"| CS
    CS -->|"located per-SHA"| SO
    AR -->|"appears in"| SE
    SE -->|"scoped to"| CM
```

Indexing one commit walks that spine end to end — `INGEST → STRUCTURE → INVALIDATE → EXTRACT → SNAPSHOT → SERVE`, with `kb embed` as a separate pass that adds pgvector semantic search on top:

```mermaid
%%{init: {'theme':'base','themeVariables':{'background':'#FAF3E7','primaryColor':'#FAF3E7','primaryTextColor':'#071812','primaryBorderColor':'#1C4434','lineColor':'#3C5B4E','secondaryColor':'#EDE3D2','tertiaryColor':'#F3EAD9','textColor':'#1C4434','fontFamily':'ui-sans-serif, system-ui, sans-serif'}}}%%
flowchart LR
    G["git blobs @ SHA<br/>no checkout"] --> S["tree-sitter spans<br/>content-addressed identity"]
    S --> I["invalidate<br/>diff-based"]
    I --> X["extractors<br/>deterministic · each grounded ≥1 span"]
    X --> N["per-SHA snapshot<br/>manifest of grounded artifacts"]
    N --> EV["eval gate"]
    EV --> SV["serve<br/>humans + AI agents (MCP)"]
    N -. "kb embed (separate pass)" .-> EM["pgvector embeddings<br/>semantic search"]
    EM -.-> SV
```

## Status

**v0.5 — spine (artifact-identity rule v2), six deterministic extractors (cross-file grounding, incl. the library public-API surface, event handlers, and call-graph edges), LLM-grounded descriptions up to per-package architecture overviews, live incremental re-index (`kb watch`), MCP serving, the thirteen knowledge gates, and published Docker images.** Everything here grounds what it claims, and nothing it cannot:

- **Provenance spine** — content-addressed `span_id` (LOCKED); tree-sitter spans with a normalized S-expression fingerprint and per-SHA location; a single-Postgres, Alembic-managed store with content-addressed idempotent writes; the ≥ 1 `derived_from` anti-hallucination invariant enforced in-app *and* by a deferred DB trigger; pygit2 git ingest (no checkout) with a diff-based invalidation seed.
- **Deterministic extractors** — the **import / dependency graph** (grimp resolves the edge, tree-sitter grounds it on the exact import statement, with an honest `approximate` fallback for re-exports / relative / unmappable imports — never a silent loss); the **FastAPI API-contract** extractor, which grounds a single route **across files** (handler in `routes.py` + `response_model` class in `schemas.py`); the **domain-entity** extractor (pydantic / dataclass / SQLAlchemy classes and their fields, grounded on the class definition **and cross-file on the entities they reference** — purely static, with documented detection limits); the **library public-API-surface** extractor (what a package exposes from its `__init__.py` — `__all__`-authoritative, with `__init__` re-exports resolved **cross-file** to the defining function/class — validated against an independent **griffe** static oracle); the **event-handler** extractor (pydantic `@field_validator`/`@model_validator`, FastAPI `@app.on_event`, SQLAlchemy `@event.listens_for` — one artifact per handler, grounded on the handler span and **cross-file** on the class it listens to; the call-form `event.listen(...)` and dynamic names are documented gaps); and the **call-graph edge** extractor (deterministic caller→callee edges — same-module, imported **cross-file**, and `self.` method calls — one `call_edge` per resolved pair with call-site lines aggregated; `obj.method(...)`, `getattr`, inherited self-calls and decorator/default-arg expressions are documented gaps; the deterministic foundation under the future business-process extractor).
- **`kb introspect`** — a sandboxed, network-blocked `app.openapi()` oracle, eval-only and never on the index path, that the API gate scores the static contract against.
- **Read-only MCP server** — `find_provenance`, `get_knowledge`, and `search_knowledge`, each returning provenance-carrying units (method + confidence + freshness).
- **pgvector embeddings + semantic search** — a replaceable embedding provider (sentence-transformers by default, OpenAI optional) populated by a separate `kb embed` pass; torch stays out of the index path.
- **A frozen RAG-over-source baseline** and the **Tier-3 knowledge-vs-RAG recall gate** — the honest A/B that backs the "knowledge > RAG" thesis.
- **LLM-grounded descriptions** — an optional, key-gated `kb describe` pass has an LLM write NL summaries for routes, entities, modules (per file), and **packages** (a per-package architecture overview that synthesizes the import graph + public surface + member-module summaries, grounded on the package's own and its direct submodules' spans); every claim is validated against the target's own spans by a deterministic sub-property gate, so ungrounded claims are *dropped* (the anti-hallucination invariant, with a model in the loop). Stored as `extraction_method = "llm_grounded"`, grounded on code spans.
- **Incremental re-index** — `kb index --parent <sha>` reuses unchanged files' spans from the parent snapshot and parses only the diff; extraction stays full, so the result is identical to a full re-index (a HARD gate proves it). Auto-detects the parent; falls back to full when none is indexed.
- **Thirteen HARD CI eval gates** (see [Development](#development)).

- **A nightly LLM-judged A/B** (optional, key-gated, **non-gating**) — an answerer LLM answers each question from knowbase's grounded context vs a RAG-over-source context, and a judge LLM scores **answer accuracy** (against hand-written gold) + **hallucination**. Tracked metrics on top of recall; it never blocks CI.

**Not done yet** (and deliberately not faked): ADR mining from git history, grounded business-process extraction, and languages beyond Python. See the [Roadmap](#roadmap).

## Quickstart

### Prerequisites

- **Python 3.12+**
- **uv** ([install](https://docs.astral.sh/uv/getting-started/installation/))
- **PostgreSQL 17** — required to *run the daemon*. For the **test suite** you do not need a running server: it spins an **ephemeral local Postgres cluster** via `initdb`/`pg_ctl` (no Docker). You just need the Postgres *binaries* on the machine (e.g. from Postgres.app or a system package). Point at them with `KB_PG_BINDIR` if they are not on `PATH`, or skip the ephemeral cluster entirely by setting `KB_TEST_DB_URL` to an existing database.

### Install

```bash
uv sync --extra dev            # create the venv + install everything
uv sync --extra dev --extra embed   # add the embedding stack (CPU torch) for `kb embed` + semantic search
```

The base `--extra dev` install stays torch-free; the `embed` extra pulls sentence-transformers (CPU-only torch via a pinned index) and is only needed for `kb embed` and `search_knowledge`.

### Run the gates

```bash
uv run pytest src/kb/eval -q   # the thirteen HARD gates (spins an ephemeral local Postgres)
```

### Index a commit

```bash
uv run kb --help
uv run kb index <repo> --sha <sha> --db-url <postgres-url>

# incremental: reuse unchanged files' spans from an already-indexed parent (parse only the diff)
uv run kb index <repo> --sha <new-sha> --parent <old-sha> --db-url <postgres-url>
```

`--sha` accepts any commit-ish (sha, branch, tag, or `HEAD`, the default). The database URL can also come from the `KB_DB_URL` environment variable instead of `--db-url`. A run prints what it produced:

```text
indexed 4f1c2a9b8d3e (full): 12 files (12 parsed, 0 reused), 318 spans, 27 artifacts, 1 gaps
  gaps (unparseable, recorded): src/legacy/broken.py
```

**Incremental re-index** (`--incremental`, or `--parent <sha>` which implies it) reuses the spans of files unchanged since the parent snapshot and parses only changed/new files; the extractors still run fully, so the snapshot is identical to a full re-index (a HARD gate proves it). The parent is auto-detected from the commit's parents (the first already-indexed one); a missing/unindexed parent or a first-party-root change falls back to a full index. This makes `kb index` cheap to wire to a git `post-receive` hook or a CI step: `kb index <repo> --sha <pushed> --parent <previous-head>`.

### Watch a repo

```bash
uv run kb watch <repo> --branch main --interval 30 --db-url <postgres-url>
uv run kb watch <repo> --once   # one tick and exit (cron/CI-friendly)
```

`kb watch` polls a **local** branch ref (no network, no credentials) and indexes every new first-parent commit incrementally, recording its resume point in the `branch_ref` table — the cursor advances per indexed commit, so a restart resumes where it left off. When the branch jumped further than `--max-catchup` (default 50) or was force-pushed, it indexes just the new head against the recorded cursor. Pair it with whatever moves the ref: a **bare repo receiving pushes** (run the watcher next to it), a **cron `git pull`** in a clone, or a **CI step** using `--once`. One database tracks one repository.

Under the hood it runs the spine for that one commit — `INGEST → STRUCTURE → EXTRACT → SNAPSHOT`. For example, an import like `from shop.billing import charge` on line 1 of `shop/orders.py` becomes an `import_edge` artifact (`import:shop.orders->shop.billing`) grounded on the exact `import` span at that `file:line@sha`, with `span_mapping: "exact"`. **"Gaps"** are files that hit a syntax error: they are *recorded*, never silently dropped, so blind spots are visible rather than invisible.

### Add semantic search

```bash
uv run kb embed --db-url <postgres-url>   # separate pass: populate artifact embeddings
```

`kb embed` runs a replaceable embedding provider (sentence-transformers `all-MiniLM-L6-v2` by default, OpenAI optional via `KB_EMBED_PROVIDER=openai`) over the latest snapshot's artifacts and writes them into `artifact.embedding` (pgvector). It is idempotent and torch only loads when this command runs — never on the index path.

### Generate semantic descriptions (LLM-grounded)

```bash
uv run kb describe --db-url <postgres-url>   # separate, key-gated pass (ANTHROPIC_API_KEY / OPENAI_API_KEY)
```

`kb describe` has an LLM (via `kb.llm`, `KB_LLM_PROVIDER` in {`anthropic`,`openai`}) write a short NL summary + structured claims for each route, entity, module (per file), and **package** (a per-package architecture overview, grounded on the package's own and its direct submodules' spans, synthesizing the import graph + public surface + member-module summaries as context) in the latest snapshot. **Every claim is validated against that target's own grounding spans** — claims citing a symbol not in the code are dropped, and a `description` artifact is stored only if something survives, grounded on those code spans (`extraction_method = "llm_grounded"`). It needs an API key, never runs on `kb index`, and the deterministic grounding gate is exercised in CI without a key (stub LLM).

### Serve to an AI agent (MCP)

```bash
uv run kb serve --db-url <postgres-url>   # read-only MCP server over stdio
```

The server exposes three read-only tools, each returning provenance-carrying knowledge units (extraction method + confidence + freshness):

| Tool | Purpose |
| --- | --- |
| `find_provenance(file, line, sha?)` | What grounded knowledge sits at `file:line@sha` — the spans there and the artifacts derived from them. |
| `get_knowledge(target, sha?, token_budget?)` | Resolve a logical key / file / module to its knowledge units, ranked and trimmed to a token budget (omissions reported, never silently truncated). |
| `search_knowledge(query, sha?, k?, token_budget?)` | Cosine-ranked semantic search over the embedded artifacts (requires `kb embed`). |

### Cross-check the API contract (eval-only)

```bash
uv run kb introspect app.main:app --repo <path>   # sandboxed app.openapi() oracle
```

`kb introspect` runs a FastAPI app in a network-blocked sandbox and emits its `openapi()` as JSON — the ground truth the Tier-1 API gate scores the **static** contract extractor against. It executes user code, so it is eval-only and never runs during indexing.

## Run with Docker

Prebuilt images are published to **GHCR**: `ghcr.io/v0ropaev/knowbase` (`:edge` from `master`, `:X.Y.Z`/`:latest` on releases; multi-arch amd64+arm64). The default image is **slim** (no torch — `index` / `migrate` / `serve` / `introspect`); the **`-embed`** tag (e.g. `:edge-embed`) adds CPU-torch for `kb embed` and semantic search.

```bash
docker pull ghcr.io/v0ropaev/knowbase:latest
docker run --rm ghcr.io/v0ropaev/knowbase:latest --help
```

As an **MCP server** (stdio), pointed at your Postgres — this is the form an MCP client launches:

```bash
docker run -i --rm ghcr.io/v0ropaev/knowbase:latest serve --db-url <postgres-url>
```

### Local dev/eval with `docker compose`

The bundled compose brings up a `pgvector` Postgres plus the `kb` CLI built from this checkout:

```bash
docker compose up -d db                                  # Postgres (pgvector)
docker compose run --rm kb migrate                       # apply the schema
docker compose run --rm kb index /workspace --sha HEAD   # index the mounted repo
docker compose run --rm -i kb serve                      # MCP over stdio
```

The compose Postgres also backs the test suite from the host: `KB_TEST_DB_URL=postgresql+psycopg://postgres:postgres@localhost:5432/postgres uv run pytest src/kb/eval -q`. For embeddings, build the image with the embed extra: `docker compose build --build-arg EXTRAS="--extra embed" kb`.

## Architecture

A Python package `kb` (uv, src-layout). Modules and their responsibilities:

| Module | Responsibility |
| --- | --- |
| `kb.ids` | Content-addressed identity hashing (**LOCKED**). `span_id` excludes file path and byte offsets; `artifact_id` refuses to be computed without ≥ 1 grounding span. |
| `kb.structural` | tree-sitter span extraction; the structural fingerprint is a normalized S-expression (named nodes only; comments and docstrings dropped; identifiers/literals kept). Location is recorded per-SHA. |
| `kb.store` | A single PostgreSQL via Alembic; content-addressed idempotent writes; the ≥ 1 `derived_from` invariant enforced in-app and by a deferred constraint trigger. |
| `kb.git` | pygit2 ingest — reads blobs at a SHA (no checkout) — plus the diff-based invalidation seed. |
| `kb.extract.deterministic.imports` | Deterministic import / dependency edges: tree-sitter spans grounded by line, grimp edge resolution. |
| `kb.extract.deterministic.fastapi_contract` | Static FastAPI API-contract extractor; grounds a route across files (handler + `response_model` class), never imports user code. |
| `kb.extract.deterministic.entities` | Static domain-entity extractor — pydantic / dataclass / SQLAlchemy classes + their fields, grounded on the class definition **and, across files, on the entities they reference** (field types + `relationship()`); detection signals and limits recorded in the payload. |
| `kb.extract.deterministic.library_surface` | Static library public-API-surface extractor — one `public_symbol` per name a package exposes from its `__init__.py` (`__all__`-authoritative), `__init__` re-exports resolved **cross-file** to the defining function/class; validated by an independent griffe static oracle. Never imports user code. |
| `kb.extract.deterministic.calls` | Static call-graph edge extractor — one `call_edge` per resolved caller→callee pair (same-module / imported **cross-file** / `self.` method), call-site lines aggregated; only resolved first-party edges are emitted; dynamic/attribute/inherited calls are documented gaps. Never imports user code. |
| `kb.extract.deterministic.events` | Static event-handler extractor — one `event_handler` per handler carrying pydantic validator / FastAPI `on_event` / SQLAlchemy `listens_for` decorator registrations (stacked decorators in `payload.registrations`), grounded on the handler span **and, across files, on the class it listens to**; call-form `event.listen(...)` and dynamic names are documented gaps. |
| `kb.introspect` | Sandboxed, network-blocked `app.openapi()` oracle — eval-only ground truth for the API gate, never on the index path. |
| `kb.mcp` | Read-only MCP server and its provenance-carrying records: `find_provenance`, `get_knowledge`, `search_knowledge`. |
| `kb.embed` | Replaceable embedding adapters (sentence-transformers default, OpenAI optional) + snapshot population. Torch isolated behind the `embed` extra and a lazy import. |
| `kb.rag` | The frozen pgvector RAG-over-source baseline — the "other arm" of the knowledge-vs-RAG A/B (no provenance, no grounding). |
| `kb.extract.semantic` | LLM-grounded extraction (`kb describe`): NL descriptions of routes/entities/modules **and per-package architecture overviews** (rich synthesis from the import graph + public surface + member summaries, grounded on code spans) with a deterministic sub-property gate (`grounding.validate_claims`) that drops any claim not backed by the target's spans. Separate key-gated pass; never on `index`. |
| `kb.daemon.cli` | The `kb` CLI: `index` (full or `--incremental`/`--parent`), `watch` (poll a local branch ref, per-commit incremental catch-up, `branch_ref` cursor), `migrate`, `embed`, `describe`, `serve` (MCP), and `introspect` — all functional. |
| `kb.eval` | Thirteen HARD CI gates (identity reproducibility, adversarial grounding, Tier-1 import oracle, Tier-1 API oracle, Tier-1 entities oracle, Tier-1 library-surface oracle, Tier-1 events oracle, Tier-1 call-graph oracle, Tier-3 knowledge-vs-RAG recall, Tier-4 one-hop invalidation, invariants, semantic grounding floor, incremental re-index equivalence) plus the supporting MCP / embed / store suite. |

Core tables: `commit_ref`, `branch_ref`, `code_span`, `span_occurrence`, `artifact` (now with `embedding vector(384)` + `embedding_model_id`), `artifact_derived_from`, `snapshot_entry`, and `rag_chunk` (the baseline arm).

## Development

```bash
uv sync --extra dev            # venv + install
uv run ruff check src/kb       # lint
uv run mypy                    # strict type-check
uv run pytest src/kb/eval -q   # the thirteen HARD eval gates
```

CI (GitHub Actions, workflow **"CI"**, `.github/workflows/ci.yml`) runs ruff, `mypy --strict`, and the eval gates against a `pgvector/pgvector:pg17` service (with the embedding model cached). The **thirteen HARD gates** that block a merge:

1. **Identity reproducibility** — formatting / comment / docstring / location changes must NOT change `span_id`; a rename MUST. Pure identity core, no database.
2. **Adversarial grounding** — an ungrounded artifact is rejected by *both* layers (the app's `GroundingError` and the DB's deferred `artifact_grounded_check` trigger); a genuinely grounded artifact commits cleanly.
3. **Tier-1 import oracle** — extracted import edges match a hand-labeled oracle, grounded on the actual import statement span; a dynamic import is asserted as a *known* gap, not a silent loss.
4. **Tier-1 API oracle** — the statically-extracted FastAPI contract equals the app's own `openapi()` (from the sandboxed introspect oracle), and the route's cross-file grounding (handler + `response_model`) is asserted.
5. **Tier-1 entities oracle** — extracted pydantic / dataclass / SQLAlchemy entities + their fields match a hand-labeled oracle, each grounded on its class span; a bare declarative `Base` is correctly *not* an entity and a `create_model(...)` model is asserted as a *known* gap.
6. **Tier-1 library-surface oracle** — the statically-extracted public API surface of a package (`__all__`-authoritative, `__init__` re-exports resolved **cross-file**) equals an independent **griffe** static oracle; underscore-private names are excluded, and a third-party / dynamic-`__all__` re-export is grounded-but-flagged, asserted as a *known* gap.
7. **Tier-1 events oracle** — extracted event handlers (pydantic validators, FastAPI `on_event`, SQLAlchemy `listens_for`) match a hand-labeled oracle, each grounded on its handler span and **cross-file** on the class it listens to; the call-form `event.listen(...)` and a dynamic `@app.on_event(EVENT)` name are asserted as *known* gaps.
8. **Tier-1 call-graph oracle** — extracted call edges match a hand-labeled oracle (same-module, cross-file imported — incl. aliases and module-attribute forms — `self.` method calls, and class instantiation), each grounded on the caller **and** callee def spans (cross-file); a mutually recursive pair yields two distinct artifacts (identity-v2 regression); `obj.method(...)`, `getattr`, inherited self-calls and decorator-expression calls are asserted as *known* gaps.
9. **Tier-3 knowledge-vs-RAG recall** — knowbase cross-file recall@k == 1.0 for every cross-file question (API contracts **and** domain entities: in each case one artifact already spans both files, so the floor is *structural*, independent of embedding quality); the RAG arm is reported but **never asserted**, so a model bump can't redden CI.
10. **Tier-4 one-hop invalidation** — a content diff invalidates *exactly* the artifacts whose grounding span changed (set-equality: no over-invalidation, no stale survivors); a version bump invalidates everything.
11. **Invariants** — zero orphans (every snapshot artifact is grounded), and re-indexing the same SHA yields the identical set of artifact ids.
12. **Semantic grounding floor** — the LLM-grounded describer's claims are validated against the artifact's own spans by a deterministic sub-property gate; an adversarial fabricated claim is *dropped*, never stored (run on a stub LLM, so it gates without an API key).
13. **Incremental re-index equivalence** — an incremental re-index (reuse unchanged files' spans from the parent, parse only the diff) yields the *identical* `{logical_key: artifact_id}` snapshot as a full re-index of the same tree, and the parse is provably skipped for unchanged files (counter assertions); a missing/unindexed parent falls back to full.

The identity rules in `kb.ids` (and `kb.structural`) are **LOCKED**: changing one is a breaking change, gated behind a `NORMALIZATION_VERSION` / `extractor_version` bump so existing digests are invalidated rather than silently colliding.

## Roadmap

The honest north star is to show that a grounded **knowledge layer beats RAG-over-code** on real questions. The reason it can is structural: to answer a cross-file contract question, RAG must independently retrieve *two* source chunks across files, while one grounded knowbase artifact already spans both — which is exactly what the Tier-3 gate measures.

```mermaid
%%{init: {'theme':'base','themeVariables':{'background':'#FAF3E7','primaryColor':'#FAF3E7','primaryTextColor':'#071812','primaryBorderColor':'#1C4434','lineColor':'#3C5B4E','secondaryColor':'#EDE3D2','tertiaryColor':'#F3EAD9','textColor':'#1C4434','fontFamily':'ui-sans-serif, system-ui, sans-serif'}}}%%
flowchart LR
    Q["Q: shape of GET /api/orders response?"]
    Q --> A["api:GET /api/orders<br/>(one grounded artifact)"]
    Q --> RAG
    A --> H["routes.py — handler span ✓"]
    A --> M["schemas.py — response_model span ✓"]
    subgraph RAG["RAG-over-source — independent top-k chunks"]
        direction TB
        C1["chunk · routes.py ✓"]
        C2["chunk · schemas.py — only if retrieved"]
        C3["chunk · other.py ✗"]
    end
```

Next milestones:

- [x] **Nightly LLM-judged A/B** (key-gated, non-gating) — grounded-answer accuracy + hallucination rate on top of recall. *(shipped)*
- [x] **LLM-grounded semantic layer** — model-backed artifacts that still carry ≥ 1 span (`extraction_method = "llm_grounded"`): `kb describe` writes span-validated descriptions for routes, entities, modules, and per-package architecture overviews. *(shipped)*
- [x] **Incremental re-index on git push** — `kb index --incremental`/`--parent` reuses unchanged files' spans from the parent snapshot (extraction stays full; equivalence is gated), and `kb watch` polls a local branch ref, indexing each new first-parent commit incrementally with a `branch_ref` cursor (`--max-catchup` guard, `--once` for cron/CI). *(shipped)*
- [ ] **ADR mining** from git / PR history.
- [ ] **Grounded business-process extraction.**
- [ ] **More languages** beyond Python.

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](./CONTRIBUTING.md) for setup, the eval-gate discipline, and pull-request expectations. A useful rule of thumb: if an extractor cannot ground a claim on a code span, it does not get to make the claim.

## Security

Please report vulnerabilities responsibly — see [SECURITY.md](./SECURITY.md). Do not open public issues for security reports.

## License

[AGPL-3.0-or-later](./LICENSE).
