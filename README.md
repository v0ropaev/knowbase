# knowbase

A versioned, **provenance-grounded knowledge layer** over a codebase, served to humans and AI
agents (via MCP). Not RAG-over-code and not an AI codegen — it *extracts* durable knowledge
(architecture, domain entities, API contracts, dependencies, business processes) from a repository
and keeps it current relative to git history. LLMs and embeddings are replaceable adapters; the
value is stable extraction + git-versioned knowledge.

See [DESIGN.md](DESIGN.md) for the architecture and locked decisions.

## Status

Early MVP. Building the **provenance spine** first (content-addressed spans, deterministic
extractors, eval gates) before any LLM/serving layer. See the design doc for scope.

## Development

```bash
uv sync --extra dev          # create venv + install
uv run pytest                # run the eval gates (spins an ephemeral local Postgres)
uv run kb --help             # CLI
```

Tests need a local PostgreSQL **binary** (`initdb`/`pg_ctl`, e.g. from Postgres.app or a system
package) — they spin an ephemeral throwaway cluster, no Docker required. Override with
`KB_TEST_DB_URL` to point at an existing database, or set `KB_PG_BINDIR` to the directory holding
`initdb`/`pg_ctl`.

License: AGPL-3.0-or-later.
