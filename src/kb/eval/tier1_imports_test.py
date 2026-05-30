"""HARD GATE — Tier 1: import edges vs a hand-labeled oracle (DESIGN.md §9).

The hand-labeled ``EXPECTED_EDGES`` is the real oracle. The dynamic-import module is a deliberate
static-analysis blind spot, asserted as a KNOWN gap (not a silent loss). Exact edges must be
grounded on the actual import statement span.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, func, select

from kb.daemon.pipeline import index_commit
from kb.eval._fixtures import make_git_repo
from kb.extract.deterministic.imports import ImportExtractor
from kb.store import models as m

# A src-layout package: a re-exporting __init__, a normal import, and a dynamic import.
FILES = {
    "src/shop/__init__.py": "from shop.orders import make\n",
    "src/shop/orders.py": (
        "import os\nfrom shop.billing import charge\n\ndef make():\n    return charge()\n"
    ),
    "src/shop/billing.py": "def charge():\n    return 1\n",
    "src/shop/dyn.py": "import importlib\nm = importlib.import_module('shop.billing')\n",
}

# Hand-labeled first-party edges (the oracle). `import os` is external -> out of scope.
EXPECTED_EDGES = {("shop", "shop.orders"), ("shop.orders", "shop.billing")}
KNOWN_GAP = ("shop.dyn", "shop.billing")  # dynamic import: invisible to static analysis


def _index(engine: Engine, tmp_path: Path) -> str:
    sha = make_git_repo(tmp_path, [FILES])[0]
    index_commit(engine, str(tmp_path), sha, extractors=[ImportExtractor()])
    return sha


def _edge_payloads(engine: Engine, sha: str) -> list[dict]:
    join = m.snapshot_entry.join(
        m.artifact, m.artifact.c.artifact_id == m.snapshot_entry.c.artifact_id
    )
    with engine.connect() as conn:
        return list(
            conn.execute(
                select(m.artifact.c.payload)
                .select_from(join)
                .where(m.snapshot_entry.c.sha == sha, m.artifact.c.kind == "import_edge")
            ).scalars()
        )


def test_structure_pass_populated_spans(engine: Engine, tmp_path: Path) -> None:
    sha = _index(engine, tmp_path)
    with engine.connect() as conn:
        spans = conn.execute(
            select(func.count())
            .select_from(m.span_occurrence)
            .where(m.span_occurrence.c.sha == sha)
        ).scalar()
    assert spans and spans > 0


def test_import_edges_match_oracle(engine: Engine, tmp_path: Path) -> None:
    sha = _index(engine, tmp_path)
    edges = {(p["importer"], p["imported"]) for p in _edge_payloads(engine, sha)}
    assert edges == EXPECTED_EDGES


def test_dynamic_import_is_a_known_gap(engine: Engine, tmp_path: Path) -> None:
    sha = _index(engine, tmp_path)
    edges = {(p["importer"], p["imported"]) for p in _edge_payloads(engine, sha)}
    assert KNOWN_GAP not in edges  # documented blind spot, surfaced — not silently "found"


def test_exact_edges_grounded_on_import_statements(engine: Engine, tmp_path: Path) -> None:
    sha = _index(engine, tmp_path)
    join = (
        m.snapshot_entry.join(
            m.artifact, m.artifact.c.artifact_id == m.snapshot_entry.c.artifact_id
        )
        .join(
            m.artifact_derived_from,
            m.artifact_derived_from.c.artifact_id == m.artifact.c.artifact_id,
        )
        .join(m.code_span, m.code_span.c.span_id == m.artifact_derived_from.c.span_id)
    )
    with engine.connect() as conn:
        rows = conn.execute(
            select(m.artifact.c.payload, m.code_span.c.span_kind)
            .select_from(join)
            .where(m.snapshot_entry.c.sha == sha, m.artifact.c.kind == "import_edge")
        ).all()
    assert rows  # every edge is grounded (>=1 derived_from)
    for row in rows:
        if row.payload["span_mapping"] == "exact":
            assert row.span_kind == "import"
