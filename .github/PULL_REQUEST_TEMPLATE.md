## Summary

<!-- What does this PR do, and why? One or two sentences. Link any related issue (e.g. Closes #123). -->

## What changed

<!-- A short, reviewable list of the concrete changes. Call out any change to the schema, migrations,
     the identity/normalization rules, or the public CLI/MCP surface. -->

-

## Test plan

<!-- How did you verify this? Reference the relevant eval gates. All gates run via
     `uv run pytest src/kb/eval -q` (CI spins a PostgreSQL 17 service; locally an ephemeral cluster). -->

- [ ] Identity reproducibility gate (`identity_test`)
- [ ] Adversarial grounding gate (`adversarial_test`)
- [ ] Tier-1 import oracle (`tier1_imports_test`)
- [ ] Tier-4 one-hop invalidation (`tier4_invalidation_test`)
- [ ] Invariants gate (`invariants_test`)
- [ ] New / updated tests for this change (describe below)

<!-- Notes on what you ran and observed: -->

## Checklist

- [ ] `uv run ruff check src/kb` passes
- [ ] `uv run mypy` passes (`--strict`)
- [ ] `uv run pytest src/kb/eval -q` passes (all five gates green)
- [ ] Any new deterministic extractor ships with its own deterministic eval gate (oracle / golden)
- [ ] Every newly stored unit is bound to >= 1 exact code span (provenance invariant upheld)
- [ ] The identity rule is not silently broken — if normalization/fingerprint semantics changed, `kb.ids.NORMALIZATION_VERSION` is bumped
- [ ] Schema changes include an Alembic migration
- [ ] Docs updated (README / DESIGN / CHANGELOG) where behavior or commands changed
