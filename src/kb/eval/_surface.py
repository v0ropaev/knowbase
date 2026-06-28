"""Dev/eval-only griffe oracle for the library public-API-surface gate (DESIGN.md §8, §9).

``griffe`` is an INDEPENDENT static analyzer (a different engine than knowbase's tree-sitter
extractor), used here only to validate the extractor's surface — it is NEVER imported on the
``kb index`` path. The oracle is static (``allow_inspection=False``), offline, and in-process, so
the gate needs no network, no API key, and no subprocess sandbox (unlike the FastAPI oracle).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import griffe


@dataclass(frozen=True)
class CanonSymbol:
    qualified_name: str
    symbol_kind: str  # "function" | "class"


def griffe_surface(top_package: str, search_paths: list[str]) -> set[CanonSymbol]:
    """The public functions/classes of ``top_package``'s top level, per griffe's static analysis."""
    pkg = griffe.load(top_package, search_paths=search_paths, allow_inspection=False)
    out: set[CanonSymbol] = set()
    for name, member in pkg.members.items():
        try:
            if not member.is_public:
                continue
            if member.is_function:
                kind = "function"
            elif member.is_class:
                kind = "class"
            else:
                continue  # submodules, attributes, unresolved aliases are out of scope
        except Exception:
            continue  # an alias griffe can't resolve offline isn't part of the asserted surface
        out.add(CanonSymbol(f"{top_package}.{name}", kind))
    return out


def artifact_surface(
    payloads: Iterable[Mapping[str, Any]], top_package: str
) -> set[CanonSymbol]:
    """The same shape from ``public_symbol`` payloads, scoped to ``top_package``'s top-level members
    (Scope A — the encoded blind-spot scope that keeps the griffe equality honest)."""
    prefix = f"{top_package}."
    out: set[CanonSymbol] = set()
    for payload in payloads:
        kind = payload.get("symbol_kind")
        qn = str(payload.get("public_qualified_name", ""))
        if kind not in ("function", "class") or not qn.startswith(prefix):
            continue
        if "." not in qn[len(prefix):]:  # Scope A: only the top package's direct members
            out.add(CanonSymbol(qn, str(kind)))
    return out
