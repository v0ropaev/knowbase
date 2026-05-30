# knowbase

> A versioned, provenance-grounded **knowledge layer** over a codebase — served to humans and AI agents. Not RAG-over-code.

<div align="center">

[![CI](https://github.com/v0ropaev/knowbase/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/v0ropaev/knowbase/actions/workflows/ci.yml)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](./LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Checked with mypy](https://img.shields.io/badge/mypy-strict-blue)](https://mypy-lang.org/)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://github.com/v0ropaev/knowbase/pulls)

</div>

knowbase turns a git repository into a **Knowledge Layer**: a queryable, git-versioned model of what a codebase *means* — its architecture, domain entities, API contracts, dependencies, events, and business processes — where **every fact is bound to the exact lines of code it came from** (`file:line@sha`).

The one thing that makes it different: it does not embed your code and hope. It **extracts** durable knowledge and grounds each unit in a real code span. LLMs and embeddings are *replaceable adapters* around that spine — swap the model, the knowledge and its provenance stay.

```
   The usual pipeline (lossy, opaque):
     Repository ──▶ Embeddings ──▶ AI Agent
                   (chunks, no provenance, drifts from HEAD)

   knowbase (grounded, versioned):
     Repository ──▶ Knowledge Extraction ──▶ Knowledge Layer ──▶ AI Agent
                    (deterministic + LLM       (provenance + method +
                     adapters)                  confidence + freshness)
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

```
  INGEST          STRUCTURE             EXTRACT                SNAPSHOT          SERVE
  ──────          ─────────             ───────                ────────          ─────
  git blobs   ─▶  tree-sitter spans ─▶  deterministic +    ─▶  per-SHA       ─▶  humans +
  at a SHA        + content-addressed   (later) model          manifest of       AI agents
  (no checkout)   identity              extractors,            grounded          (MCP)
                                        each grounded          artifacts
                                        ≥1 span
```

## Status

**v0.1 — the MVP "provenance spine."** This release ships the foundation that everything else hangs off of, and nothing it cannot ground:

- **Structural identity** — content-addressed `span_id` (LOCKED); tree-sitter span extraction with a normalized S-expression fingerprint; per-SHA location.
- **Single-Postgres store** — Alembic-managed schema; content-addressed, idempotent writes; the ≥ 1 `derived_from` anti-hallucination invariant enforced in-app *and* by a deferred DB trigger.
- **Git ingest** — pygit2 reads blobs at a SHA (no checkout); diff-based invalidation seed.
- **Deterministic import extractor** — first-party module→module dependency edges; grimp resolves the edge, tree-sitter grounds it on the exact import statement (with an honest `approximate` fallback for re-exports / relative / unmappable imports, never a silent loss).
- **Five HARD CI eval gates** (see [Development](#development)).

**Not done yet** (and deliberately not faked): the semantic / LLM extraction layer, the read-only **MCP server** (`find_provenance`, `get_knowledge`), the **FastAPI API-contract extractor**, embeddings + pgvector semantic search, and the pgvector RAG A/B baseline. The `kb serve` and `kb introspect` commands are stubs today. See the [Roadmap](#roadmap).

## Quickstart

### Prerequisites

- **Python 3.12+**
- **uv** ([install](https://docs.astral.sh/uv/getting-started/installation/))
- **PostgreSQL 17** — required to *run the daemon*. For the **test suite** you do not need a running server: it spins an **ephemeral local Postgres cluster** via `initdb`/`pg_ctl` (no Docker). You just need the Postgres *binaries* on the machine (e.g. from Postgres.app or a system package). Point at them with `KB_PG_BINDIR` if they are not on `PATH`, or skip the ephemeral cluster entirely by setting `KB_TEST_DB_URL` to an existing database.

### Install

```bash
uv sync --extra dev            # create the venv + install everything
```

### Run the gates

```bash
uv run pytest src/kb/eval -q   # the five HARD gates (spins an ephemeral local Postgres)
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

## Architecture

A Python package `kb` (uv, src-layout). Modules and their responsibilities:

| Module | Responsibility |
| --- | --- |
| `kb.ids` | Content-addressed identity hashing (**LOCKED**). `span_id` excludes file path and byte offsets; `artifact_id` refuses to be computed without ≥ 1 grounding span. |
| `kb.structural` | tree-sitter span extraction; the structural fingerprint is a normalized S-expression (named nodes only; comments and docstrings dropped; identifiers/literals kept). Location is recorded per-SHA. |
| `kb.store` | A single PostgreSQL via Alembic; content-addressed idempotent writes; the ≥ 1 `derived_from` invariant enforced in-app and by a deferred constraint trigger. |
| `kb.git` | pygit2 ingest — reads blobs at a SHA (no checkout) — plus the diff-based invalidation seed. |
| `kb.extract.deterministic.imports` | Deterministic import / dependency edges: tree-sitter spans grounded by line, grimp edge resolution. |
| `kb.daemon.cli` | The `kb` CLI (`kb index`; `serve` / `introspect` are stubs for the next milestone). |
| `kb.eval` | Five HARD CI gates (identity reproducibility, adversarial grounding, Tier-1 import oracle, Tier-4 one-hop invalidation, invariants). |

Core tables: `commit_ref`, `code_span`, `span_occurrence`, `artifact`, `artifact_derived_from`, `snapshot_entry`.

## Development

```bash
uv sync --extra dev            # venv + install
uv run ruff check src/kb       # lint
uv run mypy                    # strict type-check
uv run pytest src/kb/eval -q   # the five HARD eval gates
```

CI (GitHub Actions, workflow **"CI"**, `.github/workflows/ci.yml`) runs ruff, `mypy --strict`, and the eval gates against a PostgreSQL 17 service. The **five HARD gates** that block a merge:

1. **Identity reproducibility** — formatting / comment / docstring / location changes must NOT change `span_id`; a rename MUST. Pure identity core, no database.
2. **Adversarial grounding** — an ungrounded artifact is rejected by *both* layers (the app's `GroundingError` and the DB's deferred `artifact_grounded_check` trigger); a genuinely grounded artifact commits cleanly.
3. **Tier-1 import oracle** — extracted import edges match a hand-labeled oracle, grounded on the actual import statement span; a dynamic import is asserted as a *known* gap, not a silent loss.
4. **Tier-4 one-hop invalidation** — a content diff invalidates *exactly* the artifacts whose grounding span changed (set-equality: no over-invalidation, no stale survivors); a version bump invalidates everything.
5. **Invariants** — zero orphans (every snapshot artifact is grounded), and re-indexing the same SHA yields the identical set of artifact ids.

The identity rules in `kb.ids` (and `kb.structural`) are **LOCKED**: changing one is a breaking change, gated behind a `NORMALIZATION_VERSION` / `extractor_version` bump so existing digests are invalidated rather than silently colliding.

## Roadmap

The honest north star is to show that a grounded **knowledge layer beats RAG-over-code** on real questions. Next milestones:

- [ ] **FastAPI API-contract extractor** — the first real test of the "knowledge > RAG" thesis.
- [ ] **Read-only MCP server** — `find_provenance`, `get_knowledge`.
- [ ] **pgvector RAG baseline + A/B** against the knowledge layer.
- [ ] **Embeddings + semantic search** (pgvector) as a replaceable adapter.
- [ ] **ADR mining** from git / PR history.
- [ ] **Grounded business-process extraction.**
- [ ] **More languages** beyond Python.

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](./CONTRIBUTING.md) for setup, the eval-gate discipline, and pull-request expectations. A useful rule of thumb: if an extractor cannot ground a claim on a code span, it does not get to make the claim.

## Security

Please report vulnerabilities responsibly — see [SECURITY.md](./SECURITY.md). Do not open public issues for security reports.

## License

[AGPL-3.0-or-later](./LICENSE).
