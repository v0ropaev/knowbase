"""HARD GATE — library public-API surface vs an independent griffe oracle (DESIGN.md §8, §9).

The tree-sitter-static surface extractor must equal griffe's static surface of the same package
(``__all__``-authoritative, re-exports resolved cross-file), with underscore-private names excluded
and every artifact grounded. griffe is a dev-only oracle (a different engine), so the gate is an
INDEPENDENT cross-check, not an author-written one — and it runs fully offline, no API key.

Scope A: the equality is over the top package's ``__init__`` surface (what ``import lib; lib.X``
exposes); both sides are canonicalized to top-level functions/classes. Third-party re-exports are
asserted separately (outside the griffe equality set), since griffe may not resolve a stdlib alias's
kind offline.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, func, select

from kb.daemon.pipeline import index_commit
from kb.eval._fixtures import make_git_repo
from kb.eval._surface import artifact_surface, griffe_surface
from kb.extract.deterministic.library_surface import LibrarySurfaceExtractor
from kb.store import models as m
from kb.store.queries import provenance_for_artifact

# A package whose __init__ re-exports from submodules (cross-file) via __all__, defines one public
# symbol in place (banner), and keeps underscore-private names out of the surface.
FILES = {
    "src/lib/__init__.py": (
        "from .shapes import Circle, Square\n"
        "from .ops import area\n\n"
        "def banner() -> str:\n    return 'lib'\n\n"
        "def _hidden() -> None:\n    return None\n\n"
        "__all__ = ['Circle', 'Square', 'area', 'banner']\n"
    ),
    "src/lib/shapes.py": (
        "class Circle:\n    radius: float\n\n"
        "class Square:\n    side: float\n\n"
        "class _Base:\n    pass\n"
    ),
    "src/lib/ops.py": (
        "def area(x: float) -> float:\n    return x\n\n"
        "def _internal_helper() -> None:\n    return None\n"
    ),
    # A separate package that re-exports a third-party (stdlib) symbol — asserted on the extractor
    # output only, NOT compared to griffe (offline kind-resolution of a stdlib alias is unreliable).
    "src/libx/__init__.py": (
        "from collections import OrderedDict\n\n__all__ = ['OrderedDict']\n"
    ),
}


def _index(engine: Engine, tmp_path: Path) -> str:
    sha = make_git_repo(tmp_path, [FILES])[0]
    index_commit(
        engine, str(tmp_path), sha, extractors=[LibrarySurfaceExtractor()], first_party_root="src"
    )
    return sha


def _payloads(engine: Engine, sha: str) -> list[dict]:
    join = m.snapshot_entry.join(
        m.artifact, m.artifact.c.artifact_id == m.snapshot_entry.c.artifact_id
    )
    with engine.connect() as conn:
        return list(
            conn.execute(
                select(m.artifact.c.payload)
                .select_from(join)
                .where(m.snapshot_entry.c.sha == sha, m.artifact.c.kind == "public_symbol")
            ).scalars()
        )


def _logical_keys(engine: Engine, sha: str) -> set[str]:
    with engine.connect() as conn:
        return set(
            conn.execute(
                select(m.snapshot_entry.c.logical_key).where(
                    m.snapshot_entry.c.sha == sha,
                    m.snapshot_entry.c.logical_key.like("surface:%"),
                )
            ).scalars()
        )


def test_surface_matches_griffe_oracle(engine: Engine, tmp_path: Path) -> None:
    sha = _index(engine, tmp_path)
    extracted = artifact_surface(_payloads(engine, sha), "lib")
    oracle = griffe_surface("lib", [str(tmp_path / "src")])
    assert extracted  # non-empty: re-exports + in-place public symbol were produced
    assert extracted == oracle


def test_reexport_grounded_cross_file(engine: Engine, tmp_path: Path) -> None:
    sha = _index(engine, tmp_path)
    with engine.connect() as conn:
        prov = provenance_for_artifact(conn, sha, "surface:lib.Circle")
    by_role = {(Path(p.file_path).name, p.role) for p in prov}
    assert ("__init__.py", "re_export") in by_role  # surfaced from the package __init__ ...
    assert ("shapes.py", "definition") in by_role  # ... and grounded on the class in another file


def test_underscore_private_excluded(engine: Engine, tmp_path: Path) -> None:
    sha = _index(engine, tmp_path)
    keys = _logical_keys(engine, sha)
    assert "surface:lib._hidden" not in keys
    assert "surface:lib._internal_helper" not in keys
    assert "surface:lib.banner" in keys


def test_defined_in_place_public(engine: Engine, tmp_path: Path) -> None:
    sha = _index(engine, tmp_path)
    banner = next(p for p in _payloads(engine, sha) if p["public_qualified_name"] == "lib.banner")
    assert banner["is_reexport"] is False
    assert banner["defining_module"] == "lib"
    assert banner["symbol_kind"] == "function"


def test_every_surface_symbol_is_grounded(engine: Engine, tmp_path: Path) -> None:
    sha = _index(engine, tmp_path)
    join = m.snapshot_entry.outerjoin(
        m.artifact_derived_from,
        m.artifact_derived_from.c.artifact_id == m.snapshot_entry.c.artifact_id,
    ).join(m.artifact, m.artifact.c.artifact_id == m.snapshot_entry.c.artifact_id)
    with engine.connect() as conn:
        ungrounded = conn.execute(
            select(func.count())
            .select_from(join)
            .where(
                m.snapshot_entry.c.sha == sha,
                m.artifact.c.kind == "public_symbol",
                m.artifact_derived_from.c.artifact_id.is_(None),
            )
        ).scalar()
    assert ungrounded == 0


def test_third_party_reexport_flagged(engine: Engine, tmp_path: Path) -> None:
    sha = _index(engine, tmp_path)
    od = next(
        p for p in _payloads(engine, sha) if p["public_qualified_name"] == "libx.OrderedDict"
    )
    assert od["is_reexport"] is True
    assert od["span_mapping"] == "approximate"
    assert "definition_not_first_party" in od["limitations"]
    with engine.connect() as conn:
        prov = provenance_for_artifact(conn, sha, "surface:libx.OrderedDict")
    # grounded only on the __init__ re-export statement (no first-party definition span)
    assert {(Path(p.file_path).name, p.role) for p in prov} == {("__init__.py", "re_export")}
