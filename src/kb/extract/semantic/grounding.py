"""Deterministic sub-property gate for LLM-grounded claims (DESIGN.md §9 — the semantic hard floor).

A claim is kept only if the code identifier it cites actually appears in the artifact's grounding
spans (their source text or a fully-qualified symbol path). Everything else is dropped. No model is
in the loop here, so the anti-hallucination invariant is enforced deterministically and gated in CI
without an API key.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _tokens(texts: Sequence[str]) -> set[str]:
    out: set[str] = set()
    for text in texts:
        out.update(_IDENT.findall(text))
    return out


def _innermost_identifier(symbol: str) -> str | None:
    """The innermost identifier of a cited symbol (``shop.models.Order`` / ``List[OrderOut]`` -> the
    last identifier token), so dotted paths and simple generics still match a grounded name."""
    found = _IDENT.findall(symbol)
    return found[-1] if found else None


def validate_claims(
    claims: Sequence[dict[str, Any]],
    span_texts: Sequence[str],
    fq_paths: Sequence[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split ``claims`` into ``(kept, dropped)``.

    A claim is kept iff the innermost identifier of its ``symbol`` is a token of the grounding
    spans' source text or one of their ``fq_symbol_path`` components. A claim with no ``symbol``, or
    one whose symbol does not appear in the code, is dropped (treated as a hallucination).
    """
    grounded = _tokens(span_texts) | _tokens(fq_paths)
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for claim in claims:
        symbol = claim.get("symbol")
        ident = _innermost_identifier(symbol) if isinstance(symbol, str) else None
        if ident is not None and ident in grounded:
            kept.append(claim)
        else:
            dropped.append(claim)
    return kept, dropped
