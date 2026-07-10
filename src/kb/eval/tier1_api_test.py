"""HARD GATE — Tier 1: FastAPI contract vs the app's own openapi() oracle (DESIGN.md §9).

The honest thesis test: routes live in ``app.routes`` but their response models live in
``app.schemas`` (cross-file). The extractor must match the runtime ``app.openapi()`` oracle AND
ground each route on both the handler span and the response-model class span in the *other* module.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, func, select

from kb.daemon.pipeline import index_commit
from kb.eval._fixtures import make_git_repo
from kb.eval._openapi import canonical_routes_from_artifacts, canonical_routes_from_openapi
from kb.extract.deterministic.fastapi_contract import FastAPIExtractor
from kb.introspect import introspect_app
from kb.store import models as m

FILES = {
    "src/app/__init__.py": "",
    "src/app/schemas.py": (
        "from pydantic import BaseModel\n\n"
        "class OrderOut(BaseModel):\n    id: int\n    total: float\n\n"
        "class OrderIn(BaseModel):\n    item: str\n"
    ),
    "src/app/routes.py": (
        "from typing import List\n"
        "from fastapi import APIRouter\n"
        "from app.schemas import OrderOut, OrderIn\n\n"
        "router = APIRouter()\n\n"
        "@router.get('/orders', response_model=List[OrderOut])\n"
        "def list_orders(limit: int = 10):\n    return []\n\n"
        "@router.post('/orders', response_model=OrderOut, status_code=201)\n"
        "def create_order(body: OrderIn):\n    return body\n\n"
        "@router.get('/orders/{order_id}', response_model=OrderOut)\n"
        "def get_order(order_id: int):\n    return None\n\n"
        "@router.get('/orders/latest', response_model=OrderOut)\n"
        "@router.get('/orders/first', response_model=OrderOut)\n"
        "def stacked_order():\n    return None\n"
    ),
    "src/app/main.py": (
        "from fastapi import FastAPI\n"
        "from app.routes import router\n\n"
        "app = FastAPI()\n"
        "app.include_router(router, prefix='/api')\n\n"
        "@app.get('/internal', include_in_schema=False)\n"
        "def internal():\n    return {}\n"
    ),
}


def _index(engine: Engine, tmp_path: Path) -> str:
    sha = make_git_repo(tmp_path, [FILES])[0]
    index_commit(
        engine, str(tmp_path), sha, extractors=[FastAPIExtractor()], first_party_root="src"
    )
    return sha


def _api_payloads(engine: Engine, sha: str) -> list[dict]:
    join = m.snapshot_entry.join(
        m.artifact, m.artifact.c.artifact_id == m.snapshot_entry.c.artifact_id
    )
    with engine.connect() as conn:
        return list(
            conn.execute(
                select(m.artifact.c.payload)
                .select_from(join)
                .where(m.snapshot_entry.c.sha == sha, m.artifact.c.kind == "api_route")
            ).scalars()
        )


def test_contract_matches_openapi_oracle(engine: Engine, tmp_path: Path) -> None:
    sha = _index(engine, tmp_path)
    oracle = introspect_app("app.main:app", cwd=str(tmp_path), extra_sys_path=["src"]).openapi
    extracted = canonical_routes_from_artifacts(_api_payloads(engine, sha))
    assert extracted == canonical_routes_from_openapi(oracle)


def test_every_api_route_is_grounded(engine: Engine, tmp_path: Path) -> None:
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
                m.artifact.c.kind == "api_route",
                m.artifact_derived_from.c.artifact_id.is_(None),
            )
        ).scalar()
    assert ungrounded == 0


def test_response_model_grounded_cross_file(engine: Engine, tmp_path: Path) -> None:
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
            select(m.artifact.c.payload, m.code_span.c.fq_symbol_path, m.code_span.c.span_kind)
            .select_from(join)
            .where(
                m.snapshot_entry.c.sha == sha,
                m.artifact.c.kind == "api_route",
                m.artifact_derived_from.c.role == "response_model",
            )
        ).all()
    assert rows  # the resolved routes have a response_model edge
    for row in rows:
        assert row.span_kind == "class"
        class_module = row.fq_symbol_path.rsplit(".", 1)[0]
        assert class_module == "app.schemas"  # the model lives in a DIFFERENT module ...
        assert class_module != row.payload["handler_module"]  # ... than the handler (app.routes)


def test_include_in_schema_false_extracted_but_excluded(engine: Engine, tmp_path: Path) -> None:
    sha = _index(engine, tmp_path)
    payloads = _api_payloads(engine, sha)
    # the route IS extracted statically (we see it)...
    assert any(p["path"] == "/internal" and p["include_in_schema"] is False for p in payloads)
    # ...but the canonicalizer drops it, matching the oracle's blind spot
    assert all(r.path != "/internal" for r in canonical_routes_from_artifacts(payloads))


def test_stacked_routes_same_evidence_distinct_artifacts(engine: Engine, tmp_path: Path) -> None:
    """Identity-v2 regression (the fastapi twin of the events/entities collision): two routes
    stacked on ONE handler sharing ONE response model have an identical evidence-span set — they
    must still be TWO artifacts with distinct ids (logical_key is part of artifact_id)."""
    sha = _index(engine, tmp_path)
    join = m.snapshot_entry.join(
        m.artifact, m.artifact.c.artifact_id == m.snapshot_entry.c.artifact_id
    )
    with engine.connect() as conn:
        rows = conn.execute(
            select(m.snapshot_entry.c.logical_key, m.snapshot_entry.c.artifact_id)
            .select_from(join)
            .where(
                m.snapshot_entry.c.sha == sha,
                m.artifact.c.kind == "api_route",
                m.artifact.c.payload["handler"].astext == "app.routes.stacked_order",
            )
        ).all()
    assert len(rows) == 2  # both stacked registrations survive as their own artifacts
    assert len({bytes(r.artifact_id) for r in rows}) == 2  # ... with DISTINCT identities
    assert len({r.logical_key for r in rows}) == 2


def test_status_code_captured(engine: Engine, tmp_path: Path) -> None:
    sha = _index(engine, tmp_path)
    post = next(
        p
        for p in _api_payloads(engine, sha)
        if p["method"] == "POST" and p["path"] == "/api/orders"
    )
    assert post["status_code"] == 201
