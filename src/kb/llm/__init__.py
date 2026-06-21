"""Replaceable LLM adapters for the optional, nightly, NON-gating LLM-judged A/B (DESIGN.md §1, §9).

Used only by ``kb.eval.tier3_llm_judge_test``; never on the index or serve path. Heavy SDK imports
(anthropic / openai) are lazy so importing this package is cheap and collection-safe even when those
packages are not installed.
"""
