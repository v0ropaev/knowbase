"""First-party layout helpers: discover the importable root and relativize paths to it.

Single-root heuristic for the MVP (``src/`` layout, else repo root). Monorepo / multiple
top-level packages / namespace-package roots are deferred (DESIGN.md §13).
"""

from __future__ import annotations

from collections.abc import Sequence


def discover_first_party_root(paths: Sequence[str]) -> str:
    """``"src"`` if any file lives under ``src/``, else ``""`` (repo root)."""
    return "src" if any(p.startswith("src/") for p in paths) else ""


def relativize_to_root(path: str, root: str) -> str | None:
    """Path relative to the first-party root, or ``None`` if it lies outside it."""
    if root == "":
        return path
    prefix = f"{root}/"
    return path[len(prefix) :] if path.startswith(prefix) else None
