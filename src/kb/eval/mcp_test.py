"""HARD GATE: the read-only MCP server serves grounded, provenance-carrying knowledge (§10).

Drives the two tools through an in-memory fastmcp ``Client`` (no subprocess). Every served unit must
carry provenance + extraction method + confidence + freshness, and the cross-file API contract must
come back grounded on both the handler and the response-model class in another module.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client
from sqlalchemy import Engine, select

from kb.daemon.pipeline import index_commit
from kb.eval._fixtures import make_git_repo
from kb.eval.tier1_api_test import FILES  # the cross-file FastAPI fixture
from kb.extract.deterministic.fastapi_contract import FastAPIExtractor
from kb.mcp.server import build_server
from kb.store import models as m


@pytest.fixture
def indexed(engine: Engine, tmp_path: Path) -> tuple[Engine, str]:
    sha = make_git_repo(tmp_path, [FILES])[0]
    index_commit(
        engine, str(tmp_path), sha, extractors=[FastAPIExtractor()], first_party_root="src"
    )
    return engine, sha


def _data(result: object) -> dict:
    structured = getattr(result, "structured_content", None)
    return structured if structured is not None else result.data


def _handler_line(engine: Engine, sha: str, fq_symbol_path: str) -> int:
    join = m.code_span.join(m.span_occurrence, m.span_occurrence.c.span_id == m.code_span.c.span_id)
    with engine.connect() as conn:
        line = conn.execute(
            select(m.span_occurrence.c.start_line)
            .select_from(join)
            .where(
                m.code_span.c.fq_symbol_path == fq_symbol_path,
                m.span_occurrence.c.sha == sha,
            )
        ).scalar()
    assert line is not None
    return int(line)


async def test_get_knowledge_returns_cross_file_route(indexed: tuple[Engine, str]) -> None:
    engine, _ = indexed
    async with Client(build_server(engine)) as client:
        res = _data(await client.call_tool("get_knowledge", {"target": "api:GET /api/orders"}))
    unit = next(u for u in res["units"] if u["logical_key"] == "api:GET /api/orders")
    # every unit carries the trust envelope
    assert unit["extraction_method"] == "deterministic"
    assert 0.0 <= unit["confidence"] <= 1.0
    assert unit["freshness"] == "current"
    assert unit["provenance"]
    # the cross-file grounding is the thesis: handler in routes.py + response_model in schemas.py
    roles = {p["role"] for p in unit["provenance"]}
    files = {p["file"] for p in unit["provenance"]}
    assert {"handler", "response_model"} <= roles
    assert any(f.endswith("routes.py") for f in files)
    assert any(f.endswith("schemas.py") for f in files)


async def test_find_provenance_on_handler_line_returns_route(indexed: tuple[Engine, str]) -> None:
    engine, sha = indexed
    line = _handler_line(engine, sha, "app.routes.list_orders")
    async with Client(build_server(engine)) as client:
        res = _data(
            await client.call_tool("find_provenance", {"file": "src/app/routes.py", "line": line})
        )
    units = [u for hit in res["hits"] for u in hit["knowledge"]]
    assert any(u["logical_key"] == "api:GET /api/orders" for u in units)
    for u in units:
        assert {"provenance", "extraction_method", "confidence", "freshness"} <= u.keys()


async def test_token_budget_reports_omitted(indexed: tuple[Engine, str]) -> None:
    engine, _ = indexed
    async with Client(build_server(engine)) as client:
        res = _data(await client.call_tool("get_knowledge", {"target": "api:", "token_budget": 1}))
    assert res["total_matched"] >= 2
    assert res["omitted"] == res["total_matched"] - res["returned"]
    assert res["omitted"] > 0  # budget=1 forces trimming, reported not silent
