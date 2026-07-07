"""HARD GATE — Tier 1: call-graph edges vs a hand-labeled oracle (DESIGN.md §9, §14 item 2).

Fully static (tree-sitter re-parse; never imports/executes user code), precision-first: only edges
resolved to a first-party def span are emitted, and every documented blind spot — ``obj.method``,
``getattr``, ``super()``, inherited ``self`` calls, decorator-expression calls — is asserted as a
*known* gap, never a silent wrong guess. The mutual-recursion pair is an extractor-level regression
of identity rule v2 (``kb.ids.ARTIFACT_ID_VERSION``): two edges over the identical evidence span
set must stay two distinct artifacts.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, func, select

from kb.daemon.pipeline import index_commit
from kb.eval._fixtures import make_git_repo
from kb.extract.deterministic.calls import CallGraphExtractor
from kb.store import models as m
from kb.store.queries import provenance_for_artifact

FILES = {
    "src/cg/__init__.py": "",
    "src/cg/util.py": (
        "def helper():\n    return 1\n\n\n"
        "def ping():\n    return pong()\n\n\n"
        "def pong():\n    return ping()\n\n\n"
        "def loop(n):\n    return loop(n)\n"
    ),
    "src/cg/models.py": (
        "class Order:\n"
        "    def total(self):\n        return self.compute()\n\n"
        "    def compute(self):\n        return 0\n\n\n"
        "class Rush(Order):\n"
        "    def go(self):\n        return self.total()\n\n"
        "    def parent_total(self):\n        return super().total()\n"
    ),
    "src/cg/svc.py": (
        "import cg.util\n"
        "from cg.models import Order\n"
        "from cg.util import helper as h\n\n\n"
        "def build():\n"
        "    order = Order()\n"
        "    total = h()\n"
        "    return h(), order, total\n\n\n"
        "def fanout():\n    return cg.util.helper()\n\n\n"
        "def local():\n    return build()\n"
    ),
    "src/cg/rel.py": (
        "from .util import pong\n\n\n"
        "def relay():\n    return pong()\n"
    ),
    "src/cg/edge.py": (
        "import cg.util as u\n\n\n"
        "def deco(fn):\n    return fn\n\n\n"
        "SEED = u.helper()\n\n\n"
        "def use(order):\n    return order.compute()\n\n\n"
        "def dyn():\n    f = getattr(u, 'helper')\n    return f()\n\n\n"
        "@deco(u.ping())\n"
        "def wrapped():\n    return 0\n\n\n"
        "def outer():\n"
        "    def inner():\n        return u.helper()\n"
        "    return inner\n"
    ),
}

# Hand-labeled oracle: (caller_fq, callee_fq, resolution).
EXPECTED_EDGES = {
    ("cg.util.ping", "cg.util.pong", "same_module"),
    ("cg.util.pong", "cg.util.ping", "same_module"),
    ("cg.util.loop", "cg.util.loop", "same_module"),
    ("cg.models.Order.total", "cg.models.Order.compute", "self"),
    ("cg.svc.build", "cg.models.Order", "imported"),
    ("cg.svc.build", "cg.util.helper", "imported"),
    ("cg.svc.fanout", "cg.util.helper", "imported"),
    ("cg.svc.local", "cg.svc.build", "same_module"),
    ("cg.rel.relay", "cg.util.pong", "imported"),
    ("cg.edge", "cg.util.helper", "imported"),
    ("cg.edge.outer.inner", "cg.util.helper", "imported"),
}
# Deliberate blind spots — callers that must produce NO edge (obj.method, getattr, a decorator
# expression, inherited self-call, super(), and the outer function whose only call is nested).
KNOWN_GAP_CALLERS = {
    "cg.edge.use",
    "cg.edge.dyn",
    "cg.edge.wrapped",
    "cg.models.Rush.go",
    "cg.models.Rush.parent_total",
    "cg.edge.outer",
}


def _index(engine: Engine, tmp_path: Path) -> str:
    sha = make_git_repo(tmp_path, [FILES])[0]
    index_commit(
        engine, str(tmp_path), sha, extractors=[CallGraphExtractor()], first_party_root="src"
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
                .where(m.snapshot_entry.c.sha == sha, m.artifact.c.kind == "call_edge")
            ).scalars()
        )


def test_call_edges_match_oracle(engine: Engine, tmp_path: Path) -> None:
    sha = _index(engine, tmp_path)
    extracted = {(p["caller"], p["callee"], p["resolution"]) for p in _payloads(engine, sha)}
    assert extracted == EXPECTED_EDGES


def test_mutual_recursion_distinct_artifacts(engine: Engine, tmp_path: Path) -> None:
    """Identity-v2 regression at the extractor level: two edges, one evidence span set."""
    sha = _index(engine, tmp_path)
    join = m.snapshot_entry.join(
        m.artifact, m.artifact.c.artifact_id == m.snapshot_entry.c.artifact_id
    )
    with engine.connect() as conn:
        rows = conn.execute(
            select(m.snapshot_entry.c.logical_key, m.snapshot_entry.c.artifact_id,
                   m.artifact.c.payload)
            .select_from(join)
            .where(
                m.snapshot_entry.c.sha == sha,
                m.snapshot_entry.c.logical_key.in_(
                    ["call:cg.util.ping->cg.util.pong", "call:cg.util.pong->cg.util.ping"]
                ),
            )
        ).all()
    by_key = {r.logical_key: r for r in rows}
    assert len(by_key) == 2  # both directions survive
    assert len({bytes(r.artifact_id) for r in rows}) == 2  # distinct ids, identical span set
    assert by_key["call:cg.util.ping->cg.util.pong"].payload["caller"] == "cg.util.ping"
    assert by_key["call:cg.util.pong->cg.util.ping"].payload["caller"] == "cg.util.pong"


def test_instantiation_edge(engine: Engine, tmp_path: Path) -> None:
    sha = _index(engine, tmp_path)
    order = next(
        p for p in _payloads(engine, sha)
        if p["caller"] == "cg.svc.build" and p["callee"] == "cg.models.Order"
    )
    assert order["callee_kind"] == "class"  # instantiation is a real dependency edge
    assert order["caller_kind"] == "function"
    assert order["resolution"] == "imported"


def test_module_caller_and_merged_lines(engine: Engine, tmp_path: Path) -> None:
    sha = _index(engine, tmp_path)
    by_edge = {(p["caller"], p["callee"]): p for p in _payloads(engine, sha)}
    seed = by_edge[("cg.edge", "cg.util.helper")]
    assert seed["caller_kind"] == "module"  # a module-level call grounds on the module span
    assert len(seed["lines"]) == 1
    build = by_edge[("cg.svc.build", "cg.util.helper")]
    assert len(build["lines"]) == 2  # two h() sites merged into ONE artifact
    assert build["lines"] == sorted(build["lines"])


def test_self_call_grounded_on_same_class_method(engine: Engine, tmp_path: Path) -> None:
    sha = _index(engine, tmp_path)
    key = "call:cg.models.Order.total->cg.models.Order.compute"
    with engine.connect() as conn:
        prov = {(p.file_path, p.role) for p in provenance_for_artifact(conn, sha, key)}
    assert prov == {("src/cg/models.py", "caller"), ("src/cg/models.py", "callee")}


def test_cross_file_provenance(engine: Engine, tmp_path: Path) -> None:
    """The thesis property: ONE artifact spans the caller's and the callee's files."""
    sha = _index(engine, tmp_path)
    key = "call:cg.svc.build->cg.models.Order"
    with engine.connect() as conn:
        prov = {(p.file_path, p.role) for p in provenance_for_artifact(conn, sha, key)}
    assert ("src/cg/svc.py", "caller") in prov
    assert ("src/cg/models.py", "callee") in prov


def test_known_gaps_not_extracted(engine: Engine, tmp_path: Path) -> None:
    sha = _index(engine, tmp_path)
    payloads = _payloads(engine, sha)
    assert all(p["caller"] not in KNOWN_GAP_CALLERS for p in payloads)
    # the decorator-expression call u.ping() produced nothing: pong stays ping's only caller
    ping_callers = {p["caller"] for p in payloads if p["callee"] == "cg.util.ping"}
    assert ping_callers == {"cg.util.pong"}


def test_nested_def_attribution(engine: Engine, tmp_path: Path) -> None:
    sha = _index(engine, tmp_path)
    callers = {p["caller"] for p in _payloads(engine, sha)}
    assert "cg.edge.outer.inner" in callers  # the call attributes to the NESTED function ...
    assert "cg.edge.outer" not in callers  # ... never to its enclosing one (no-descend rule)


def test_every_call_edge_is_grounded(engine: Engine, tmp_path: Path) -> None:
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
                m.artifact.c.kind == "call_edge",
                m.artifact_derived_from.c.artifact_id.is_(None),
            )
        ).scalar()
    assert ungrounded == 0
    join2 = (
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
            select(m.artifact.c.payload, m.code_span.c.span_kind, m.artifact_derived_from.c.role)
            .select_from(join2)
            .where(m.snapshot_entry.c.sha == sha, m.artifact.c.kind == "call_edge")
        ).all()
    assert rows
    for row in rows:
        assert row.payload["span_mapping"] == "exact"
        if row.role == "caller":
            assert row.span_kind in ("module", "function", "method")
        else:
            assert row.role == "callee"
            assert row.span_kind in ("function", "class", "method")


def test_direct_recursion_single_span(engine: Engine, tmp_path: Path) -> None:
    sha = _index(engine, tmp_path)
    with engine.connect() as conn:
        prov = provenance_for_artifact(conn, sha, "call:cg.util.loop->cg.util.loop")
    assert len(prov) == 1  # caller == callee span: one grounding row (PK-safe) ...
    assert prov[0].role == "caller"  # ... keeping the caller role
