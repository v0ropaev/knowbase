# knowbase

> A versioned, provenance-grounded **knowledge layer** over a codebase — served to humans and AI agents. Not RAG-over-code.

<div align="center">

[![CI](https://github.com/v0ropaev/knowbase/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/v0ropaev/knowbase/actions/workflows/ci.yml)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](./LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Checked with mypy](https://img.shields.io/badge/mypy-strict-blue)](https://mypy-lang.org/)
[![GHCR image](https://img.shields.io/badge/ghcr.io-knowbase-2496ED?logo=docker&logoColor=white)](https://github.com/v0ropaev/knowbase/pkgs/container/knowbase)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://github.com/v0ropaev/knowbase/pulls)

</div>

knowbase turns a git repository into a **Knowledge Layer**: a queryable, git-versioned model of what a codebase *means* — its architecture, domain entities, API contracts, dependencies, events, and business processes — where **every fact is bound to the exact lines of code it came from** (`file:line@sha`).

The one thing that makes it different: it does not embed your code and hope. It **extracts** durable knowledge and grounds each unit in a real code span. LLMs and embeddings are *replaceable adapters* around that spine — swap the model, the knowledge and its provenance stay.

```mermaid
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

Artifacts are content-addressed the same way — over their byte-sorted, de-duplicated grounding spans plus `extractor_id`/`extractor_version` (and `prompt_version`/`model_id` for model-backed extractors). Re-indexing the same commit reproduces the identical set of artifact ids.

The spine is a handful of content-addressed tables: each **artifact** carries ≥ 1 `derived_from` edge to a **code_span**, spans are located **per-SHA** in `span_occurrence`, and a per-SHA **snapshot** ties the grounded artifacts to a commit.

```mermaid
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

**v0.2 — spine + the first knowledge extractors, MCP serving, and the knowledge-vs-RAG gate.** Everything here grounds what it claims, and nothing it cannot:

- **Provenance spine** — content-addressed `span_id` (LOCKED); tree-sitter spans with a normalized S-expression fingerprint and per-SHA location; a single-Postgres, Alembic-managed store with content-addressed idempotent writes; the ≥ 1 `derived_from` anti-hallucination invariant enforced in-app *and* by a deferred DB trigger; pygit2 git ingest (no checkout) with a diff-based invalidation seed.
- **Deterministic extractors** — the **import / dependency graph** (grimp resolves the edge, tree-sitter grounds it on the exact import statement, with an honest `approximate` fallback for re-exports / relative / unmappable imports — never a silent loss); the **FastAPI API-contract** extractor, which grounds a single route **across files** (handler in `routes.py` + `response_model` class in `schemas.py`); and the **domain-entity** extractor (pydantic / dataclass / SQLAlchemy classes and their fields, grounded on the class definition **and cross-file on the entities they reference** — purely static, with documented detection limits).
- **`kb introspect`** — a sandboxed, network-blocked `app.openapi()` oracle, eval-only and never on the index path, that the API gate scores the static contract against.
- **Read-only MCP server** — `find_provenance`, `get_knowledge`, and `search_knowledge`, each returning provenance-carrying units (method + confidence + freshness).
- **pgvector embeddings + semantic search** — a replaceable embedding provider (sentence-transformers by default, OpenAI optional) populated by a separate `kb embed` pass; torch stays out of the index path.
- **A frozen RAG-over-source baseline** and the **Tier-3 knowledge-vs-RAG recall gate** — the honest A/B that backs the "knowledge > RAG" thesis.
- **Eight HARD CI eval gates** (see [Development](#development)).

- **A nightly LLM-judged A/B** (optional, key-gated, **non-gating**) — an answerer LLM answers each question from knowbase's grounded context vs a RAG-over-source context, and a judge LLM scores **answer accuracy** (against hand-written gold) + **hallucination**. Tracked metrics on top of recall; it never blocks CI.

**Not done yet** (and deliberately not faked): the semantic / **LLM-grounded** extraction layer, ADR mining from git history, grounded business-process extraction, incremental re-index on git push, and languages beyond Python. See the [Roadmap](#roadmap).

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
uv run pytest src/kb/eval -q   # the eight HARD gates (spins an ephemeral local Postgres)
```

### Index a commit

```bash
uv run kb --help
uv run kb index <repo> --sha <sha> --db-url <postgres-url>
```

`--sha` accepts any commit-ish (sha, branch, tag, or `HEAD`, the default). The database URL can also come from the `KB_DB_URL` environment variable instead of `--db-url`. A run prints what it produced:

```text
indexed 4f1c2a9b8d3e: 12 files, 318 spans, 27 artifacts, 1 gaps
  gaps (unparseable, recorded): src/legacy/broken.py
```

Under the hood it runs the spine for that one commit — `INGEST → STRUCTURE → EXTRACT → SNAPSHOT`. For example, an import like `from shop.billing import charge` on line 1 of `shop/orders.py` becomes an `import_edge` artifact (`import:shop.orders->shop.billing`) grounded on the exact `import` span at that `file:line@sha`, with `span_mapping: "exact"`. **"Gaps"** are files that hit a syntax error: they are *recorded*, never silently dropped, so blind spots are visible rather than invisible.

### Add semantic search

```bash
uv run kb embed --db-url <postgres-url>   # separate pass: populate artifact embeddings
```

`kb embed` runs a replaceable embedding provider (sentence-transformers `all-MiniLM-L6-v2` by default, OpenAI optional via `KB_EMBED_PROVIDER=openai`) over the latest snapshot's artifacts and writes them into `artifact.embedding` (pgvector). It is idempotent and torch only loads when this command runs — never on the index path.

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
| `kb.introspect` | Sandboxed, network-blocked `app.openapi()` oracle — eval-only ground truth for the API gate, never on the index path. |
| `kb.mcp` | Read-only MCP server and its provenance-carrying records: `find_provenance`, `get_knowledge`, `search_knowledge`. |
| `kb.embed` | Replaceable embedding adapters (sentence-transformers default, OpenAI optional) + snapshot population. Torch isolated behind the `embed` extra and a lazy import. |
| `kb.rag` | The frozen pgvector RAG-over-source baseline — the "other arm" of the knowledge-vs-RAG A/B (no provenance, no grounding). |
| `kb.daemon.cli` | The `kb` CLI: `index`, `migrate`, `embed`, `serve` (MCP), and `introspect` — all functional. |
| `kb.eval` | Eight HARD CI gates (identity reproducibility, adversarial grounding, Tier-1 import oracle, Tier-1 API oracle, Tier-1 entities oracle, Tier-3 knowledge-vs-RAG recall, Tier-4 one-hop invalidation, invariants) plus the supporting MCP / embed / store suite. |

Core tables: `commit_ref`, `branch_ref`, `code_span`, `span_occurrence`, `artifact` (now with `embedding vector(384)` + `embedding_model_id`), `artifact_derived_from`, `snapshot_entry`, and `rag_chunk` (the baseline arm).

## Development

```bash
uv sync --extra dev            # venv + install
uv run ruff check src/kb       # lint
uv run mypy                    # strict type-check
uv run pytest src/kb/eval -q   # the eight HARD eval gates
```

CI (GitHub Actions, workflow **"CI"**, `.github/workflows/ci.yml`) runs ruff, `mypy --strict`, and the eval gates against a `pgvector/pgvector:pg17` service (with the embedding model cached). The **eight HARD gates** that block a merge:

1. **Identity reproducibility** — formatting / comment / docstring / location changes must NOT change `span_id`; a rename MUST. Pure identity core, no database.
2. **Adversarial grounding** — an ungrounded artifact is rejected by *both* layers (the app's `GroundingError` and the DB's deferred `artifact_grounded_check` trigger); a genuinely grounded artifact commits cleanly.
3. **Tier-1 import oracle** — extracted import edges match a hand-labeled oracle, grounded on the actual import statement span; a dynamic import is asserted as a *known* gap, not a silent loss.
4. **Tier-1 API oracle** — the statically-extracted FastAPI contract equals the app's own `openapi()` (from the sandboxed introspect oracle), and the route's cross-file grounding (handler + `response_model`) is asserted.
5. **Tier-1 entities oracle** — extracted pydantic / dataclass / SQLAlchemy entities + their fields match a hand-labeled oracle, each grounded on its class span; a bare declarative `Base` is correctly *not* an entity and a `create_model(...)` model is asserted as a *known* gap.
6. **Tier-3 knowledge-vs-RAG recall** — knowbase cross-file recall@k == 1.0 for every cross-file question (API contracts **and** domain entities: in each case one artifact already spans both files, so the floor is *structural*, independent of embedding quality); the RAG arm is reported but **never asserted**, so a model bump can't redden CI.
7. **Tier-4 one-hop invalidation** — a content diff invalidates *exactly* the artifacts whose grounding span changed (set-equality: no over-invalidation, no stale survivors); a version bump invalidates everything.
8. **Invariants** — zero orphans (every snapshot artifact is grounded), and re-indexing the same SHA yields the identical set of artifact ids.

The identity rules in `kb.ids` (and `kb.structural`) are **LOCKED**: changing one is a breaking change, gated behind a `NORMALIZATION_VERSION` / `extractor_version` bump so existing digests are invalidated rather than silently colliding.

## Roadmap

The honest north star is to show that a grounded **knowledge layer beats RAG-over-code** on real questions. The reason it can is structural: to answer a cross-file contract question, RAG must independently retrieve *two* source chunks across files, while one grounded knowbase artifact already spans both — which is exactly what the Tier-3 gate measures.

```mermaid
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
- [ ] **LLM-grounded semantic layer** — model-backed artifacts that still carry ≥ 1 span (`extraction_method = "llm_grounded"`).
- [ ] **Incremental re-index on git push** — turn the diff-based invalidation seed into live updates.
- [ ] **ADR mining** from git / PR history.
- [ ] **Grounded business-process extraction.**
- [ ] **More languages** beyond Python.

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](./CONTRIBUTING.md) for setup, the eval-gate discipline, and pull-request expectations. A useful rule of thumb: if an extractor cannot ground a claim on a code span, it does not get to make the claim.

## Security

Please report vulnerabilities responsibly — see [SECURITY.md](./SECURITY.md). Do not open public issues for security reports.

## License

[AGPL-3.0-or-later](./LICENSE).
