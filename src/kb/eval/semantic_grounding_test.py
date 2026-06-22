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

from sqlalchemy import Engine, select

from kb.daemon.pipeline import index_commit
from kb.eval._fixtures import make_git_repo
from kb.eval.tier1_api_test import FILES
from kb.extract.deterministic.fastapi_contract import FastAPIExtractor
from kb.extract.semantic.describe import describe_snapshot
from kb.extract.semantic.grounding import validate_claims
from kb.store import models as m
from kb.store.queries import provenance_for_artifact

REAL = "OrderOut"  # appears in the fixture (schemas.py + the routes' response_model)
FAKE = "nonexistent_symbol_xyz"  # appears nowhere -> must be dropped as a hallucination


class _StubProvider:
    """Deterministic stand-in for an LLMProvider: always returns one real + one fabricated claim."""

    model_id = "stub:describe-test"

    def complete(self, system: str, user: str, *, max_tokens: int = 1024) -> str:
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

    # Modules with no occurrence of OrderOut (e.g. app.main, app.__init__) get NO description:
    # every claim was a hallucination relative to the file's spans, so nothing is stored.
    assert "app.main" not in described
    assert "app.__init__" not in described
