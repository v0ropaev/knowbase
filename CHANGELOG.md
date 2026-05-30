# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- (nothing yet)

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

[Unreleased]: https://github.com/v0ropaev/knowbase/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/v0ropaev/knowbase/releases/tag/v0.1.0
