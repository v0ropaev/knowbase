"""Derivation of ``fq_symbol_path`` — the symbol component of span identity (DESIGN.md §5).

Pure string logic. The module fqname is derived from a path relative to the first-party root, but
note: the *path* is not stored in identity — only the resulting dotted module name is, so moving a
file changes identity only if its import path changes.
"""

from __future__ import annotations

from collections.abc import Sequence


def module_fqname(rel_path: str) -> str:
    """Dotted module name from a POSIX path relative to the first-party root.

    ``shop/orders/service.py`` -> ``shop.orders.service``;
    ``shop/orders/__init__.py`` -> ``shop.orders``.
    """
    parts = rel_path.split("/")
    last = parts[-1]
    if last == "__init__.py":
        parts = parts[:-1]
    elif last.endswith(".py"):
        parts[-1] = last[: -len(".py")]
    return ".".join(p for p in parts if p)


def symbol_fqname(module: str, name_stack: Sequence[str]) -> str:
    """Fully-qualified symbol path: module + nested class/function names.

    (``shop.orders``, ``["OrderService", "create"]``) -> ``shop.orders.OrderService.create``.
    """
    if not name_stack:
        return module
    return ".".join([module, *name_stack])


def import_fqname(module: str, ordinal: int) -> str:
    """Synthetic symbol path for an import statement (it has no name symbol). The 0-based
    ``ordinal`` is the import's position in source order within the module, so duplicate imports
    (``import os`` twice) remain distinct and reproducible (resolved decision, DESIGN.md §5)."""
    return f"{module}::import@{ordinal}"
