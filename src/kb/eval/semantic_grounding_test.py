"""HARD GATE — semantic floor (DESIGN.md §9): LLM-grounded claims are span-validated.

Uses a STUB LLM provider (fixed output: one real symbol + one fabricated one), so the
anti-hallucination invariant of the LLM layer is enforced **deterministically and without an API
key** — it gates in normal CI. The describer must store only the grounded claim and drop the
fabricated one; the description is grounded (role `describes`) on its target's spans and served as
`llm_grounded`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select

from kb.daemon.pipeline import index_commit
from kb.eval._fixtures import make_git_repo
from kb.eval.tier1_api_test import FILES
from kb.extract.deterministic.calls import CallGraphExtractor
from kb.extract.deterministic.events import EventExtractor
from kb.extract.deterministic.fastapi_contract import FastAPIExtractor
from kb.extract.deterministic.paths import ProcessPathExtractor
from kb.extract.semantic.describe import _build_prompt, describe_snapshot
from kb.extract.semantic.grounding import validate_claims
from kb.store import models as m
from kb.store.queries import ArtifactSpanRow, provenance_for_artifact

REAL = "OrderOut"  # appears in the fixture (schemas.py + the routes' response_model)
FAKE = "nonexistent_symbol_xyz"  # appears nowhere -> must be dropped as a hallucination

# A process-path fixture (tier1_processes_test style, unique "lp" module names so the shared
# session DB can't collide): a route handler in ONE file calls a helper in ANOTHER file whose body
# hits a sink from the fixture's own .kb/sinks.yaml. The handler references OrderOut, so REAL is a
# token of the path's entrypoint span and must survive validation on the process path too.
LP_FILES = {
    ".kb/sinks.yaml": (
        "version: 1\nextend: true\nsinks:\n"
        '  - name: label_write\n    patterns: ["*.record_label"]\n'
    ),
    "src/lp/__init__.py": "",
    "src/lp/schemas.py": "class OrderOut:\n    order_id: int\n",
    "src/lp/api.py": (
        "from fastapi import APIRouter\n"
        "from lp.schemas import OrderOut\n"
        "from lp.billing import charge\n\n"
        "router = APIRouter()\n\n\n"
        "@router.post('/labels')\n"
        "def create_label():\n    charge()\n    return OrderOut()\n"
    ),
    "src/lp/billing.py": (
        "class Books:\n    def record_label(self, kind):\n        return kind\n\n\n"
        "books = Books()\n\n\n"
        "def charge():\n    return books.record_label('label')\n"
    ),
}


# An event-handler fixture (unique "evd" module names + unique content, so the shared session DB
# can't collide on the sha): a SQLAlchemy listener in ONE file listens to a class defined in
# ANOTHER file — cross-file grounding, and OrderOut is a token of both spans.
EVD_FILES = {
    "src/evd/__init__.py": "",
    "src/evd/db.py": "class OrderOut:\n    order_id: int\n    total: float\n",
    "src/evd/hooks.py": (
        "from sqlalchemy import event\n"
        "from evd.db import OrderOut\n\n\n"
        "@event.listens_for(OrderOut, 'after_insert')\n"
        "def audit_insert(mapper, connection, target):\n    pass\n"
    ),
}

# A nested-package fixture (unique "rvw" names): proves the repo overview's grounding is BOUNDED —
# a grandchild module must never ground it (its own nearer package overview covers it).
RVW_FILES = {
    "src/rvw/__init__.py": "",
    "src/rvw/core.py": "class OrderOut:\n    review_id: int\n",
    "src/rvw/inner/__init__.py": "",
    "src/rvw/inner/impl.py": "def deep():\n    return 'grandchild'\n",
}


class _StubProvider:
    """Deterministic stand-in for an LLMProvider: always returns one real + one fabricated claim.

    Records every ``system`` prompt it sees, so tests can assert the repo overview runs under its
    own system prompt without importing private constants.
    """

    model_id = "stub:describe-test"

    def __init__(self) -> None:
        self.systems: list[str] = []

    def complete(self, system: str, user: str, *, max_tokens: int = 1024) -> str:
        self.systems.append(system)
        return json.dumps(
            {
                "summary": "Stub description.",
                "claims": [
                    {"text": f"returns {REAL}", "symbol": REAL},
                    {"text": "calls a fabricated helper", "symbol": FAKE},
                ],
            }
        )


def _index(engine: Engine, tmp_path: Path) -> str:
    sha = make_git_repo(tmp_path, [FILES])[0]
    index_commit(
        engine, str(tmp_path), sha, extractors=[FastAPIExtractor()], first_party_root="src"
    )
    return sha


def test_validator_drops_fabricated_symbol() -> None:
    claims = [{"text": "a", "symbol": REAL}, {"text": "b", "symbol": FAKE}]
    kept, dropped = validate_claims(
        claims, ["class OrderOut(BaseModel):\n    id: int\n"], ["app.schemas.OrderOut"]
    )
    assert [c["symbol"] for c in kept] == [REAL]
    assert [c["symbol"] for c in dropped] == [FAKE]


def _description_rows(engine: Engine, sha: str) -> list[Any]:
    join = m.snapshot_entry.join(
        m.artifact, m.artifact.c.artifact_id == m.snapshot_entry.c.artifact_id
    )
    with engine.connect() as conn:
        return conn.execute(
            select(
                m.artifact.c.logical_key,
                m.artifact.c.payload,
                m.artifact.c.is_deterministic,
                m.artifact.c.confidence,
                m.artifact.c.prompt_version,
            )
            .select_from(join)
            .where(m.snapshot_entry.c.sha == sha, m.artifact.c.kind == "description")
        ).all()


def test_describe_stores_only_grounded_claims(engine: Engine, tmp_path: Path) -> None:
    sha = _index(engine, tmp_path)
    result = describe_snapshot(engine, sha, _StubProvider())

    assert result.described > 0
    assert result.dropped_claims > 0  # the fabricated claim was dropped on every artifact

    rows = _description_rows(engine, sha)
    assert rows
    with engine.connect() as conn:
        for row in rows:
            symbols = [c["symbol"] for c in row.payload["claims"]]
            assert REAL in symbols  # the grounded claim survives
            assert FAKE not in symbols  # adversarial: the hallucinated claim is never stored
            assert row.is_deterministic is False  # surfaced as llm_grounded
            assert row.confidence == pytest.approx(1 / 3)  # Laplace: 1 / (1 + 1 + 1)
            assert row.confidence < 1.0  # 1.0 stays reserved for the deterministic layer
            prov_files = {p.file_path for p in provenance_for_artifact(conn, sha, row.logical_key)}
            assert prov_files  # grounded on its target's spans (>= 1 file)


def test_module_descriptions_are_grounded(engine: Engine, tmp_path: Path) -> None:
    """The same span-validation gate covers per-module descriptions (DESIGN.md §9)."""
    sha = _index(engine, tmp_path)
    describe_snapshot(engine, sha, _StubProvider())

    rows = _description_rows(engine, sha)
    modules = {
        r.payload["target_logical_key"]: r
        for r in rows
        if r.payload["target_kind"] == "module"
    }
    assert modules  # modules are described, not just artifacts

    # Modules where OrderOut occurs (defined in app.schemas, imported/used in app.routes) ARE
    # described; the fabricated symbol is dropped on the module path too (adversarial).
    described = set(modules)
    assert "app.schemas" in described or "app.routes" in described
    for row in modules.values():
        symbols = [c["symbol"] for c in row.payload["claims"]]
        assert REAL in symbols
        assert FAKE not in symbols
        assert row.prompt_version == "1"  # non-repo prompts are contract: byte-identical

    # Modules with no occurrence of OrderOut (e.g. app.main, app.__init__) get NO description:
    # every claim was a hallucination relative to the file's spans, so nothing is stored.
    assert "app.main" not in described
    assert "app.__init__" not in described


def test_package_descriptions_are_grounded(engine: Engine, tmp_path: Path) -> None:
    """The same span-validation gate covers per-package architecture overviews (DESIGN.md §9)."""
    sha = _index(engine, tmp_path)
    describe_snapshot(engine, sha, _StubProvider())

    rows = _description_rows(engine, sha)
    packages = {
        r.payload["target_logical_key"]: r
        for r in rows
        if r.payload["target_kind"] == "package"
    }
    # `app` is a package (src/app/__init__.py exists); its member spans (the __init__ + schemas /
    # routes / main) contain OrderOut, so the package overview is produced.
    assert "app" in packages
    row = packages["app"]
    assert row.logical_key == "desc:package:app"
    assert row.prompt_version == "1"  # non-repo prompts are contract: byte-identical
    symbols = [c["symbol"] for c in row.payload["claims"]]
    assert REAL in symbols  # the grounded claim survives on the package path
    assert FAKE not in symbols  # adversarial: the fabricated claim is dropped on the package path
    assert row.is_deterministic is False  # surfaced as llm_grounded
    with engine.connect() as conn:
        prov_files = {p.file_path for p in provenance_for_artifact(conn, sha, row.logical_key)}
    assert prov_files  # grounded on the package's code spans (>= 1 file)


def test_event_handler_descriptions_are_grounded(engine: Engine, tmp_path: Path) -> None:
    """The same span-validation gate covers LLM descriptions of event handlers (DESIGN.md §9)."""
    sha = make_git_repo(tmp_path, [EVD_FILES])[0]
    index_commit(
        engine, str(tmp_path), sha, extractors=[EventExtractor()], first_party_root="src"
    )
    describe_snapshot(engine, sha, _StubProvider())

    rows = [
        r for r in _description_rows(engine, sha) if r.payload["target_kind"] == "event_handler"
    ]
    assert len(rows) == 1  # exactly the one extracted handler gets a description
    row = rows[0]
    assert row.logical_key == "desc:event:evd.hooks.audit_insert"
    assert row.payload["target_logical_key"] == "event:evd.hooks.audit_insert"
    symbols = [c["symbol"] for c in row.payload["claims"]]
    assert REAL in symbols  # the grounded claim survives on the event-handler path
    assert FAKE not in symbols  # adversarial: the fabricated claim is dropped here too
    assert row.is_deterministic is False  # surfaced as llm_grounded
    with engine.connect() as conn:
        prov_files = {p.file_path for p in provenance_for_artifact(conn, sha, row.logical_key)}
    # cross-file: the handler span AND the listened-to class it resolves to
    assert prov_files == {"src/evd/hooks.py", "src/evd/db.py"}


def test_repo_overview_is_grounded(engine: Engine, tmp_path: Path) -> None:
    """The same span-validation gate covers the whole-repo overview (DESIGN.md §9)."""
    sha = _index(engine, tmp_path)
    stub = _StubProvider()
    describe_snapshot(engine, sha, stub)

    # the repo call runs LAST and under its OWN system prompt (facts synthesis); every other
    # target this run shares the one generic system prompt
    assert stub.systems[-1] != stub.systems[0]
    assert len(set(stub.systems[:-1])) == 1

    rows = [r for r in _description_rows(engine, sha) if r.payload["target_kind"] == "repo"]
    assert len(rows) == 1  # exactly ONE repo overview per snapshot
    row = rows[0]
    assert row.logical_key == "desc:repo"
    assert row.payload["target_logical_key"] == "repo"
    assert row.prompt_version == "2"  # the repo prompt carries its own identity-bearing version
    symbols = [c["symbol"] for c in row.payload["claims"]]
    assert REAL in symbols  # the grounded claim survives on the repo path
    assert FAKE not in symbols  # adversarial: the fabricated claim is dropped on the repo path
    assert row.is_deterministic is False  # surfaced as llm_grounded
    with engine.connect() as conn:
        prov_files = {p.file_path for p in provenance_for_artifact(conn, sha, row.logical_key)}
    # exactly the top package `app` + its direct children — the bounded top-level surface
    assert prov_files == {
        "src/app/__init__.py",
        "src/app/schemas.py",
        "src/app/routes.py",
        "src/app/main.py",
    }


def test_repo_prompt_body_packing_is_fair() -> None:
    """With ``span_cap`` no single large file monopolizes the prompt body (the dogfooding fix);
    without it the historical greedy packing stays byte-identical for every other kind."""
    huge = ArtifactSpanRow(b"a", "pkg.big", "x" * 7000)
    small = ArtifactSpanRow(b"b", "pkg.small", "class OrderOut: ...")

    greedy = _build_prompt("repo", {}, [huge, small])
    assert "pkg.big" in greedy
    assert "pkg.small" not in greedy  # guard: the pre-fix greedy behavior, kept for other kinds

    fair = _build_prompt("repo", {}, [huge, small], span_cap=400)
    assert "pkg.big" in fair
    assert "pkg.small" in fair  # guard: the fix — both spans get a fair slice


def test_repo_overview_grounding_is_bounded(engine: Engine, tmp_path: Path) -> None:
    """A grandchild module never grounds the repo overview (its own package overview covers it)."""
    sha = make_git_repo(tmp_path, [RVW_FILES])[0]
    index_commit(engine, str(tmp_path), sha, extractors=[], first_party_root="src")
    describe_snapshot(engine, sha, _StubProvider())

    rows = [r for r in _description_rows(engine, sha) if r.payload["target_kind"] == "repo"]
    assert len(rows) == 1
    with engine.connect() as conn:
        prov_files = {p.file_path for p in provenance_for_artifact(conn, sha, "desc:repo")}
    assert "src/rvw/core.py" in prov_files  # a direct child grounds the overview ...
    assert "src/rvw/inner/impl.py" not in prov_files  # ... a grandchild never does


def test_process_path_descriptions_are_grounded(engine: Engine, tmp_path: Path) -> None:
    """The same span-validation gate covers LLM-labeled process paths (DESIGN.md §9, §14 item 2)."""
    sha = make_git_repo(tmp_path, [LP_FILES])[0]
    index_commit(
        engine,
        str(tmp_path),
        sha,
        # ORDER is load-bearing: ProcessPathExtractor is second-order and must run last.
        extractors=[FastAPIExtractor(), CallGraphExtractor(), ProcessPathExtractor()],
        first_party_root="src",
    )
    describe_snapshot(engine, sha, _StubProvider())

    rows = [r for r in _description_rows(engine, sha) if r.payload["target_kind"] == "process_path"]
    assert len(rows) == 1  # exactly the one materialized path gets a label
    row = rows[0]
    assert row.logical_key == "desc:process:lp.api.create_label->label_write@lp.billing.charge"
    symbols = [c["symbol"] for c in row.payload["claims"]]
    assert REAL in symbols  # the grounded claim survives on the process path
    assert FAKE not in symbols  # adversarial: the fabricated claim is dropped here too
    assert row.is_deterministic is False  # surfaced as llm_grounded
    with engine.connect() as conn:
        prov_files = {p.file_path for p in provenance_for_artifact(conn, sha, row.logical_key)}
    assert len(prov_files) >= 2  # grounded across the path's files (multi-file provenance)
