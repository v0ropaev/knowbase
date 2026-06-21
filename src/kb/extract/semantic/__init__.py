"""LLM-grounded semantic extraction (DESIGN.md §4, §9) — the first model-backed knowledge layer.

Runs as a separate, key-gated pass (``kb describe``), never on the deterministic ``kb index`` path.
Every produced claim is validated against the artifact's own grounding spans by a deterministic
sub-property gate (``grounding.validate_claims``); unvalidated claims are dropped — the
anti-hallucination invariant, enforced without a model in the loop so it is gateable in CI.
"""
