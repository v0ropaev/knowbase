# Contributing to knowbase

Thanks for your interest in knowbase. This document explains how to set up a dev environment, run
the checks, and get a change merged. Please read it before opening a pull request — the project has
a few hard rules (the provenance spine, the LOCKED span identity, the eval gates) that exist to keep
the knowledge layer trustworthy, and a PR that ignores them cannot be merged.

If anything here is unclear or out of date, open an issue. The architecture and the locked design
decisions live in [DESIGN.md](DESIGN.md); this file is about the mechanics of contributing.

## Ground rules

- **Everything is grounded in code.** Nothing is stored unless it is bound to at least one exact
  code span (`file:line@sha`). This `>= 1 derived_from` invariant is enforced in the application
  (`GroundingError`) *and* by a deferred database trigger. Do not add code paths that write
  knowledge artifacts without grounding, and do not weaken either layer.
- **Determinism first.** knowbase is not an AI code generator and not RAG-over-code. The MVP is a
  deterministic provenance spine. LLMs and embeddings are replaceable adapters that come later;
  contributions to the core must be deterministic and testable against ground truth.
- **Span identity is LOCKED.** See ["The LOCKED span-identity rule"](#the-locked-span-identity-rule)
  below before touching `kb.ids` or the structural fingerprint.

## Development environment

knowbase is a Python 3.12+ package (`kb`, src-layout) managed with [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev            # create the venv + install runtime and dev dependencies
```

That installs the runtime deps (tree-sitter, grimp, pygit2, SQLAlchemy, psycopg 3, Alembic, Typer,
sqlparse, …) plus the dev tooling (pytest, ruff, mypy, pyyaml). After syncing, run anything inside
the project venv with `uv run`:

```bash
uv run kb --help               # the CLI (entry point kb = kb.daemon.cli:app)
```

You do **not** need Docker for development.

## The test database

The eval gates exercise a real PostgreSQL (the content-addressed store, the deferred grounding
trigger, invalidation queries), so the test suite needs a Postgres to talk to. There are three ways
it gets one, in priority order:

1. **Existing database (preferred in CI and convenient locally).** Set `KB_TEST_DB_URL` to a
   SQLAlchemy/psycopg URL pointing at an existing, empty database. The suite uses it directly and
   never spins a cluster. Example:

   ```bash
   export KB_TEST_DB_URL="postgresql+psycopg://postgres:postgres@127.0.0.1:5432/postgres"
   ```

   This is exactly what CI does against its PostgreSQL 17 service.

2. **Ephemeral local cluster (the default when `KB_TEST_DB_URL` is unset).** The suite spins a
   throwaway PostgreSQL cluster with `initdb`/`pg_ctl`, runs against it, and tears it down — no
   Docker, no persistent state. You need the PostgreSQL **binaries** available (from Postgres.app,
   Homebrew, or a system package). The suite looks for `initdb`/`pg_ctl` on `PATH` and in common
   install locations.

3. **Pointing at out-of-path binaries.** If your `initdb`/`pg_ctl` are not on `PATH`, set
   `KB_PG_BINDIR` to the directory that holds them:

   ```bash
   export KB_PG_BINDIR="/Applications/Postgres.app/Contents/Versions/17/bin"
   ```

If no usable Postgres is found, the harness fails with a clear message telling you to set
`KB_PG_BINDIR` or `KB_TEST_DB_URL`, or to install PostgreSQL. The migrations under `migrations/` are
applied to whichever database is in play; the target is PostgreSQL 17.

## Running the checks

Three checks gate every change. All three must pass locally before you open a PR, and all three run
in CI:

```bash
uv run ruff check src/kb       # lint
uv run mypy                    # strict type-check
uv run pytest src/kb/eval -q   # the eval gates (provisions a test database as described above)
```

- `ruff check` must report no findings.
- `mypy` runs in `--strict` mode (configured in `pyproject.toml`; packages = `kb`). It must be
  clean. Do not add blanket `# type: ignore`; if a third-party stub is missing, prefer a narrow,
  justified ignore or a typed wrapper. The `kb.eval` package is intentionally excluded from strict
  typing — it is validated by *running*, not by types.
- `pytest src/kb/eval -q` runs the gates. They are the source of truth for correctness; treat a red
  gate as a blocking defect, never as flakiness to retry around.

## The eval gates

The eval suite (`src/kb/eval/`) is five HARD gates. They are not ordinary unit tests — they pin the
guarantees the whole project rests on, and CI fails if any of them fails:

1. **Identity reproducibility** (`identity_test.py`) — span identity is reproducible and location-
   independent. Formatting, comment, docstring, byte-offset, file-path, and commit changes must NOT
   change a `span_id`; a rename MUST. No database is involved; this is the pure identity core.
2. **Adversarial grounding** (`adversarial_test.py`) — the anti-hallucination invariant holds end to
   end. An ungrounded artifact is rejected by the application (`GroundingError`, before any write)
   *and* by the deferred database trigger at COMMIT; a genuinely grounded artifact commits cleanly.
3. **Tier-1 import oracle** (`tier1_imports_test.py`) — deterministic import/dependency edges match
   a hand-labeled oracle exactly, grounded on the actual import-statement span, with known
   static-analysis blind spots asserted as explicit gaps rather than silent losses.
4. **Tier-4 one-hop invalidation** (`tier4_invalidation_test.py`) — a content diff invalidates
   exactly the artifacts whose grounding span changed (no over-invalidation, no stale survivors),
   and a version bump invalidates everything, kept distinct from content-diff invalidation.
5. **Invariants** (`invariants_test.py`) — cross-cutting invariants over a real index: zero orphans
   (every stored artifact is grounded by `>= 1` `derived_from`), and re-indexing the same SHA yields
   the identical set of artifact ids.

### New extractors must ship with a deterministic gate

If you add an extractor (e.g. an API-contract or events extractor), it must ship with a
deterministic gate backed by **ground truth** — a hand-labeled oracle for a small fixture repo,
asserted for exact, grounded output, in the same spirit as the Tier-1 import gate. The bar:

- Build the fixture (use the helpers in `src/kb/eval/_fixtures.py` to construct a git repo) and
  hand-label the expected artifacts and their grounding spans.
- Assert the extractor produces exactly those artifacts, each bound to the correct span.
- Assert any known limitation explicitly as a gap, never let it pass as a silent loss.
- Every emitted artifact must carry `>= 1` `derived_from` edge (see `kb.extract.base`).

An extractor without a deterministic ground-truth gate will not be merged. "It seemed to work on my
repo" is not ground truth.

## Code style

- **Formatting & lint:** ruff, configured in `pyproject.toml` (rule sets `E, F, W, I, N, UP, B, C4,
  RUF`, target `py312`). Run `uv run ruff check src/kb`. Use `uv run ruff check --fix` for
  mechanical fixes and `uv run ruff format` if you want consistent formatting.
- **Line length: 100.**
- **Typing:** mypy `--strict`. New code in `kb` must be fully typed.
- **Tests:** the eval tests live under `src/kb/eval/` and are named `*_test.py` (pytest is
  configured with `python_files = ["*_test.py", "test_*.py"]` and `testpaths = ["src/kb/eval"]`).
  Name new gate files `<thing>_test.py`. Shared, non-test helpers in that package are prefixed with
  an underscore (e.g. `_fixtures.py`, `_pg.py`) so pytest does not collect them.
- Keep modules focused and docstring the *why*, matching the existing house style (every gate file
  opens with a docstring stating what guarantee it pins and the relevant DESIGN.md section).

## The LOCKED span-identity rule

Content-addressed identity is the spine of the system, and it is **LOCKED**. The rules that define a
`span_id` and an artifact id are pinned because changing them silently would let new digests collide
with — or diverge from — digests already stored in real databases.

The identity rules live in:

- `kb.ids` — the pure hashing functions; `NORMALIZATION_VERSION` is defined here. `span_id`
  deliberately excludes the file path and byte offsets.
- `kb.structural.fingerprint` — the normalized S-expression fingerprint (named nodes only; comments
  and docstrings dropped; identifiers and literals kept).
- the structural symbol-path computation that feeds `fq_symbol_path`.

**If you change any span-identity rule** — the fingerprint normalization, what is dropped or kept,
the symbol-path derivation, or the hashing in `kb.ids` — you MUST bump
`kb.ids.NORMALIZATION_VERSION`. This invalidates existing digests safely instead of letting them
collide. (For artifact-level extractor changes, bump the relevant `extractor_version` the same way.)
The identity gate exists precisely to catch unbumped changes: it will fail if the LOCKED behavior
shifts without an explicit version bump. A PR that alters identity rules without bumping the version
will be rejected.

## Commit conventions

Use [Conventional Commits](https://www.conventionalcommits.org/) for commit messages:

```
<type>(<optional scope>): <imperative summary>
```

Common types: `feat`, `fix`, `docs`, `refactor`, `test`, `perf`, `chore`, `ci`. Scope the project
areas where it helps (e.g. `feat(extract): add FastAPI route extractor`, `fix(store): …`,
`test(eval): …`). Keep the summary in the imperative mood and under ~72 characters; put rationale
and context in the body. If a change bumps `NORMALIZATION_VERSION` or is otherwise breaking, mark it
with `!` (e.g. `feat(ids)!: …`) and/or a `BREAKING CHANGE:` footer.

## Pull request flow

1. **Branch from `master`.** Create a focused topic branch (e.g. `feat/api-contract-extractor`).
2. **Keep PRs small and focused.** One logical change per PR. Large, mixed PRs are hard to review
   and slow to merge; split unrelated changes.
3. **Make the checks pass locally** before pushing: `uv run ruff check src/kb`, `uv run mypy`, and
   `uv run pytest src/kb/eval -q` must all be green.
4. **Add or update gates** for behavior you change. New extractors require a deterministic
   ground-truth gate (see above).
5. **Open the PR against `master`.** Describe what changed and why, and call out anything that
   touches the provenance spine, the LOCKED identity rules, or the migrations.
6. **Green CI is required to merge.** The GitHub Actions workflow named **CI**
   (`.github/workflows/ci.yml`) runs ruff, mypy `--strict`, and the eval gates against a PostgreSQL
   17 service. A PR cannot be merged until CI is green.

Review is about correctness and the invariants first; please be patient and responsive to feedback.

## Reporting bugs and proposing features

- **Bugs:** open an issue using the bug-report template:
  [`.github/ISSUE_TEMPLATE/bug_report.yml`](.github/ISSUE_TEMPLATE/bug_report.yml). Include the exact
  command, the repo/SHA you indexed, the database URL scheme (not credentials), and the full error
  and traceback. For anything involving identity or invalidation, the smaller the reproducing
  fixture, the better.
- **Features:** open an issue using the feature-request template:
  [`.github/ISSUE_TEMPLATE/feature_request.yml`](.github/ISSUE_TEMPLATE/feature_request.yml). Describe
  the durable knowledge you want extracted and how it would be grounded in code spans. Proposals
  that fit the roadmap (API-contract extraction, read-only MCP serving, then semantic search) and
  respect the provenance/determinism principles are easiest to land.

For security-sensitive reports, please do not open a public issue; contact the maintainers privately.

## License

knowbase is licensed under **AGPL-3.0-or-later** (see [LICENSE](LICENSE)). By contributing, you agree
that your contributions are licensed under the same terms. Do not add code or assets that are
incompatible with AGPL-3.0-or-later.
