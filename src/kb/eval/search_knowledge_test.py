"""search_knowledge MCP tool: semantic retrieval returns grounded cross-file units (§10)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client
from sqlalchemy import Engine

from kb.daemon.pipeline import index_commit
from kb.embed.population import embed_snapshot
from kb.eval._fixtures import make_git_repo
from kb.eval.tier1_api_test import FILES
from kb.extract.deterministic.fastapi_contract import FastAPIExtractor
from kb.mcp.server import build_server


@pytest.fixture
def embedded(engine: Engine, tmp_path: Path, st_provider) -> tuple[Engine, str]:
    sha = make_git_repo(tmp_path, [FILES])[0]
    index_commit(
        engine, str(tmp_path), sha, extractors=[FastAPIExtractor()], first_party_root="src"
    )
    embed_snapshot(engine, sha, st_provider)
    return engine, sha


def _data(result: object) -> dict:
    structured = getattr(result, "structured_content", None)
    return structured if structured is not None else result.data


async def test_search_knowledge_returns_cross_file_unit(embedded: tuple[Engine, str]) -> None:
    engine, sha = embedded
    async with Client(build_server(engine)) as client:
        res = _data(
            await client.call_tool(
                "search_knowledge",
                {"query": "response model of listing orders", "sha": sha, "k": 5},
            )
        )
    assert res["total_matched"] > 0
    unit = next(u for u in res["units"] if u["logical_key"].startswith("api:GET /api/orders"))
    assert unit["extraction_method"] == "deterministic"
    assert unit["freshness"] == "current"
    files = {p["file"] for p in unit["provenance"]}
    assert any(f.endswith("routes.py") for f in files)
    assert any(f.endswith("schemas.py") for f in files)
