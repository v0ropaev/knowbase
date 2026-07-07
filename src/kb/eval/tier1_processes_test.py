"""HARD GATE — Tier 1: deterministic business-process paths vs a hand-labeled oracle (§9, §14).

The DESIGN §9 hard floor holds by construction: every sink claim IS a registry match on the
materialized path, every endpoint IS an extracted entrypoint. The fixture ships its own
``.kb/sinks.yaml`` override, proving the whole git-blob → pipeline materialization → registry-merge
chain; a reachable call cycle proves the BFS cannot hang; the flagship path grounds ONE artifact
across THREE files — the thesis apex.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, func, select

from kb.daemon.pipeline import index_commit
from kb.eval._fixtures import make_git_repo
from kb.extract.deterministic.calls import CallGraphExtractor
from kb.extract.deterministic.events import EventExtractor
from kb.extract.deterministic.fastapi_contract import FastAPIExtractor
from kb.extract.deterministic.paths import _BUILTIN_SINKS, ProcessPathExtractor
from kb.store import models as m
from kb.store.queries import provenance_for_artifact

FILES = {
    ".kb/sinks.yaml": (
        "version: 1\nextend: true\nsinks:\n"
        '  - name: ledger_write\n    patterns: ["*.record_ledger"]\n'
    ),
    "src/bp/__init__.py": "",
    "src/bp/api.py": (
        "from fastapi import APIRouter\n"
        "import requests\n"
        "from bp.svc import place, lookup\n\n"
        "router = APIRouter()\n\n\n"
        "@router.post('/orders')\n"
        "def create_order():\n    return place()\n\n\n"
        "@router.get('/orders')\n"
        "def list_orders():\n    return lookup()\n\n\n"
        "@router.post('/ping')\n"
        "def ping():\n    return requests.post('http://x')\n"
    ),
    "src/bp/svc.py": (
        "from bp.repo import save_order\n\n\n"
        "def place():\n    spin()\n    return save_order()\n\n\n"
        "def lookup():\n    return 1\n\n\n"
        "def spin():\n    return unspin()\n\n\n"
        "def unspin():\n    return spin()\n"
    ),
    "src/bp/repo.py": (
        "from bp.ledger import ledger\n\n\n"
        "def save_order():\n    return ledger.record_ledger('order')\n"
    ),
    "src/bp/ledger.py": (
        "class Ledger:\n    def record_ledger(self, kind):\n        return kind\n\n\n"
        "ledger = Ledger()\n"
    ),
    "src/bp/boot.py": (
        "from fastapi import FastAPI\n"
        "from bp.repo import save_order\n\n"
        "app = FastAPI()\n\n\n"
        "@app.on_event('startup')\n"
        "def warmup():\n    return save_order()\n"
    ),
}

# Hand-labeled oracle: (entrypoint, kind, reference, steps, sink_name, terminal, depth).
EXPECTED_PATHS = {
    ("bp.api.create_order", "api_route", "POST /orders",
     ("bp.api.create_order", "bp.svc.place", "bp.repo.save_order"),
     "ledger_write", "bp.repo.save_order", 2),
    ("bp.api.ping", "api_route", "POST /ping",
     ("bp.api.ping",), "http_call", "bp.api.ping", 0),
    ("bp.boot.warmup", "event_handler", "fastapi_on_event:startup",
     ("bp.boot.warmup", "bp.repo.save_order"),
     "ledger_write", "bp.repo.save_order", 1),
}
NO_PATH_ENTRYPOINTS = {"bp.api.list_orders"}  # its call chain reaches no sink


def _extractors(**kw: int) -> list:
    # ORDER is load-bearing: ProcessPathExtractor is second-order and must run last.
    return [FastAPIExtractor(), EventExtractor(), CallGraphExtractor(),
            ProcessPathExtractor(**kw)]


def _index(engine: Engine, tmp_path: Path, **kw: int) -> str:
    sha = make_git_repo(tmp_path, [FILES])[0]
    index_commit(engine, str(tmp_path), sha, extractors=_extractors(**kw),
                 first_party_root="src")
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
                .where(m.snapshot_entry.c.sha == sha, m.artifact.c.kind == "process_path")
            ).scalars()
        )


def test_process_paths_match_oracle(engine: Engine, tmp_path: Path) -> None:
    sha = _index(engine, tmp_path)  # completing at all proves the spin<->unspin cycle can't hang
    extracted = {
        (p["entrypoint"], p["entrypoint_kind"], p["entrypoint_reference"],
         tuple(p["steps"]), p["sink"]["name"], p["terminal"], p["depth"])
        for p in _payloads(engine, sha)
    }
    assert extracted == EXPECTED_PATHS
    assert all(p["entrypoint"] not in NO_PATH_ENTRYPOINTS for p in _payloads(engine, sha))


def test_multi_file_provenance_thesis(engine: Engine, tmp_path: Path) -> None:
    """The apex assertion: ONE artifact grounded across THREE files."""
    sha = _index(engine, tmp_path)
    key = "process:bp.api.create_order->ledger_write@bp.repo.save_order"
    with engine.connect() as conn:
        prov = {(p.file_path, p.role) for p in provenance_for_artifact(conn, sha, key)}
    assert prov == {
        ("src/bp/api.py", "entrypoint"),
        ("src/bp/svc.py", "step"),
        ("src/bp/repo.py", "terminal"),
    }


def test_flagship_payload_shape(engine: Engine, tmp_path: Path) -> None:
    sha = _index(engine, tmp_path)
    flagship = next(
        p for p in _payloads(engine, sha) if p["entrypoint"] == "bp.api.create_order"
    )
    assert flagship["edges"] == [
        {"caller": "bp.api.create_order", "callee": "bp.svc.place", "resolution": "imported"},
        {"caller": "bp.svc.place", "callee": "bp.repo.save_order", "resolution": "imported"},
    ]
    assert flagship["sink"]["matches"] == [
        {"text": "ledger.record_ledger", "pattern": "*.record_ledger", "line": 5}
    ]
    assert flagship["span_mapping"] == "exact"
    assert flagship["limitations"] == []
    join = m.snapshot_entry.join(
        m.artifact, m.artifact.c.artifact_id == m.snapshot_entry.c.artifact_id
    )
    with engine.connect() as conn:
        fw = conn.execute(
            select(m.artifact.c.framework_versions)
            .select_from(join)
            .where(
                m.snapshot_entry.c.sha == sha,
                m.snapshot_entry.c.logical_key
                == "process:bp.api.create_order->ledger_write@bp.repo.save_order",
            )
        ).scalar()
    assert fw and "sink_registry" in fw  # the effective registry is identity-bearing


def test_zero_hop_path(engine: Engine, tmp_path: Path) -> None:
    sha = _index(engine, tmp_path)
    with engine.connect() as conn:
        prov = provenance_for_artifact(conn, sha, "process:bp.api.ping->http_call@bp.api.ping")
    assert len(prov) == 1  # entrypoint == terminal span: one grounding row ...
    assert prov[0].role == "entrypoint"  # ... keeping the entrypoint role


def test_override_registry_is_live(engine: Engine, tmp_path: Path) -> None:
    assert all(r.name != "ledger_write" for r in _BUILTIN_SINKS)  # override-only sink ...
    sha = _index(engine, tmp_path)
    sinks = {p["sink"]["name"] for p in _payloads(engine, sha)}
    assert "ledger_write" in sinks  # ... proves .kb/sinks.yaml -> materialization -> merge


def test_every_process_path_is_grounded(engine: Engine, tmp_path: Path) -> None:
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
                m.artifact.c.kind == "process_path",
                m.artifact_derived_from.c.artifact_id.is_(None),
            )
        ).scalar()
    assert ungrounded == 0


def test_determinism_reindex(engine: Engine, tmp_path: Path) -> None:
    sha = _index(engine, tmp_path)

    def ids() -> set[bytes]:
        with engine.connect() as conn:
            join = m.snapshot_entry.join(
                m.artifact, m.artifact.c.artifact_id == m.snapshot_entry.c.artifact_id
            )
            return {
                bytes(r)
                for r in conn.execute(
                    select(m.snapshot_entry.c.artifact_id)
                    .select_from(join)
                    .where(
                        m.snapshot_entry.c.sha == sha, m.artifact.c.kind == "process_path"
                    )
                ).scalars()
            }

    first = ids()
    index_commit(engine, str(tmp_path), sha, extractors=_extractors(), first_party_root="src")
    assert ids() == first
    assert len(first) == len(EXPECTED_PATHS)


def test_depth_cap(engine: Engine, tmp_path: Path) -> None:
    # a marker file makes the sha unique: the shared session DB already holds this fixture
    # indexed at the DEFAULT depth under the identical-content sha
    files = {**FILES, "src/bp/depth_marker.py": "DEPTH_CAP_FIXTURE = 1\n"}
    sha = make_git_repo(tmp_path, [files])[0]
    index_commit(engine, str(tmp_path), sha, extractors=_extractors(max_depth=1),
                 first_party_root="src")
    entrypoints = {p["entrypoint"] for p in _payloads(engine, sha)}
    # the 2-hop flagship exceeds max_depth=1 and is not emitted; 0/1-hop paths survive
    assert "bp.api.create_order" not in entrypoints
    assert {"bp.api.ping", "bp.boot.warmup"} <= entrypoints
