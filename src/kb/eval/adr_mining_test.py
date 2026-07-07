"""HARD GATE — ADR mining floor (DESIGN.md §4, §9): decision candidates are span-grounded.

Uses a STUB LLM provider (fixed output: one real symbol + one fabricated one), so the mining
pass's anti-hallucination invariant is enforced deterministically and without an API key. The
grounding bridge under test is the D5 resolution itself: a ``decision`` artifact is grounded on
the spans its source commit CHANGED (role ``changed``), the fabricated claim is dropped, a commit
whose changed code carries no cited symbol stores NOTHING, a docs-only commit never pays an LLM
call, merges are skipped, and re-mining is idempotent (stored decisions are never re-billed).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pygit2
from sqlalchemy import Engine, select

from kb.daemon.pipeline import index_commit
from kb.eval._fixtures import _SIG, make_git_repo
from kb.extract.deterministic.imports import ImportExtractor
from kb.extract.semantic.mine import MineResult, mine_history
from kb.store import models as m
from kb.store.queries import provenance_for_artifact

REAL = "RetryPolicy"  # defined in the fixture's policy.py (and re-exported by __init__)
FAKE = "nonexistent_symbol_xyz"  # appears nowhere -> must be dropped as a hallucination

_INIT = "from adrm.policy import RetryPolicy\n"
_POLICY_V1 = "class RetryPolicy:\n    max_attempts = 3\n"
_POLICY_V2 = _POLICY_V1 + "\n\ndef apply_backoff(attempt):\n    return 2**attempt\n"
_NOTES = "def tidy():\n    return 'notes'\n"

# Four linear commits: root (decision-bearing), a policy change (decision-bearing), a change that
# touches only code WITHOUT the stub's real symbol (the floor drops everything), and a docs-only
# commit (no changed first-party spans -> the LLM is never called).
COMMITS = [
    {"src/adrm/__init__.py": _INIT, "src/adrm/policy.py": _POLICY_V1},
    {"src/adrm/__init__.py": _INIT, "src/adrm/policy.py": _POLICY_V2},
    {"src/adrm/__init__.py": _INIT, "src/adrm/policy.py": _POLICY_V2, "src/adrm/notes.py": _NOTES},
    {
        "src/adrm/__init__.py": _INIT,
        "src/adrm/policy.py": _POLICY_V2,
        "src/adrm/notes.py": _NOTES,
        "README.md": "docs only\n",
    },
]
MESSAGES = [
    "Adopt RetryPolicy: exponential backoff for flaky upstreams",
    "Make backoff explicit: add apply_backoff to RetryPolicy",
    "chore: tidy notes",
    "docs: readme",
]


class _StubProvider:
    """Deterministic LLMProvider stand-in: one real + one fabricated claim, and a call counter."""

    model_id = "stub:mine-test"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, system: str, user: str, *, max_tokens: int = 1024) -> str:
        self.calls += 1
        return json.dumps(
            {
                "summary": "Stub decision.",
                "claims": [
                    {"text": f"introduces {REAL}", "symbol": REAL},
                    {"text": "cites a fabricated helper", "symbol": FAKE},
                ],
            }
        )


def _index_history(engine: Engine, tmp_path: Path) -> list[str]:
    shas = make_git_repo(tmp_path, COMMITS, messages=MESSAGES)
    for sha in shas:
        index_commit(
            engine, str(tmp_path), sha, extractors=[ImportExtractor()], first_party_root="src"
        )
    return shas


def _mine(
    engine: Engine, tmp_path: Path, start: str, **kw: Any
) -> tuple[MineResult, _StubProvider]:
    stub = _StubProvider()
    result = mine_history(engine, str(tmp_path), stub, start_sha=start, **kw)
    return result, stub


def _decision_rows(engine: Engine, shas: list[str]) -> dict[str, Any]:
    join = m.snapshot_entry.join(
        m.artifact, m.artifact.c.artifact_id == m.snapshot_entry.c.artifact_id
    )
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                m.snapshot_entry.c.sha,
                m.artifact.c.logical_key,
                m.artifact.c.payload,
                m.artifact.c.is_deterministic,
                m.artifact.c.confidence,
                m.snapshot_entry.c.artifact_id,
            )
            .select_from(join)
            .where(m.snapshot_entry.c.sha.in_(shas), m.artifact.c.kind == "decision")
        ).all()
    return {row.sha: row for row in rows}


def test_mine_stores_grounded_decisions(engine: Engine, tmp_path: Path) -> None:
    shas = _index_history(engine, tmp_path)
    result, stub = _mine(engine, tmp_path, shas[-1])

    assert result.scanned == 4
    assert result.mined == 2  # root + the policy change; the floor and the no-code commit yield 0
    assert stub.calls == 3  # docs-only commit never pays an LLM call
    decisions = _decision_rows(engine, shas)
    assert set(decisions) == {shas[0], shas[1]}
    row = decisions[shas[1]]
    assert row.logical_key == f"decision:{shas[1]}"
    symbols = [c["symbol"] for c in row.payload["claims"]]
    assert REAL in symbols  # the grounded claim survives
    assert FAKE not in symbols  # adversarial: the fabricated claim is never stored
    assert row.is_deterministic is False
    assert row.confidence == 0.5  # counted: 1 kept / (1 kept + 1 dropped)
    assert row.payload["message"] == MESSAGES[1]  # verbatim, pinned by the sha in the key
    assert row.payload["sha"] == shas[1]
    assert row.payload["limitations"] == []


def test_decision_grounded_on_changed_spans_only(engine: Engine, tmp_path: Path) -> None:
    shas = _index_history(engine, tmp_path)
    _mine(engine, tmp_path, shas[-1])
    with engine.connect() as conn:
        prov = provenance_for_artifact(conn, shas[1], f"decision:{shas[1]}")
    assert prov  # >= 1 grounding row (D5)
    assert {p.role for p in prov} == {"changed"}
    # the commit touched ONLY policy.py: untouched files never ground the decision
    assert {p.file_path for p in prov} == {"src/adrm/policy.py"}


def test_root_commit_mined_against_empty_tree(engine: Engine, tmp_path: Path) -> None:
    shas = _index_history(engine, tmp_path)
    _mine(engine, tmp_path, shas[-1])
    with engine.connect() as conn:
        prov = provenance_for_artifact(conn, shas[0], f"decision:{shas[0]}")
    # the whole initial tree IS the diff: both files ground the root decision
    assert {p.file_path for p in prov} == {"src/adrm/__init__.py", "src/adrm/policy.py"}


def test_no_grounded_claim_stores_nothing(engine: Engine, tmp_path: Path) -> None:
    shas = _index_history(engine, tmp_path)
    result, _ = _mine(engine, tmp_path, shas[-1])
    # commit 3 changed only notes.py, which carries neither claimed symbol: everything dropped
    assert shas[2] not in _decision_rows(engine, shas)
    assert result.dropped_claims >= 2  # both stub claims died on the notes-only commit


def test_rerun_is_idempotent(engine: Engine, tmp_path: Path) -> None:
    shas = _index_history(engine, tmp_path)
    _mine(engine, tmp_path, shas[-1])
    first = {s: bytes(r.artifact_id) for s, r in _decision_rows(engine, shas).items()}

    rerun, stub = _mine(engine, tmp_path, shas[-1])
    assert rerun.mined == 0
    assert rerun.skipped_already_mined == 2  # stored decisions are never re-billed
    assert stub.calls == 1  # only the no-decision commit is re-asked (documented cost)

    forced, _ = _mine(engine, tmp_path, shas[-1], force=True)
    assert forced.mined == 2  # force re-mines, but content-addressing keeps the ids
    after = {s: bytes(r.artifact_id) for s, r in _decision_rows(engine, shas).items()}
    assert after == first


def test_merge_commit_is_skipped(engine: Engine, tmp_path: Path) -> None:
    shas = _index_history(engine, tmp_path)
    repo = pygit2.Repository(str(tmp_path))
    tree = repo.revparse_single(shas[-1]).peel(pygit2.Commit).tree.id
    merge = str(repo.create_commit("HEAD", _SIG, _SIG, "merge branch", tree, [shas[-1], shas[1]]))
    index_commit(engine, str(tmp_path), merge, extractors=[ImportExtractor()],
                 first_party_root="src")

    result, stub = _mine(engine, tmp_path, merge, max_commits=1)
    assert result.skipped_merges == 1
    assert stub.calls == 0
    assert merge not in _decision_rows(engine, [merge])
