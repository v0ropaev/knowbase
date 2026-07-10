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
import pytest
from sqlalchemy import Engine, select

from kb.daemon.pipeline import index_commit
from kb.eval._fixtures import _SIG, make_git_repo
from kb.extract.deterministic.imports import ImportExtractor
from kb.extract.semantic.mine import MineResult, mine_history
from kb.git.forge import GitHubPRProvider, PRText, origin_slug, pr_number_from_subject
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
    "Adopt RetryPolicy: exponential backoff for flaky upstreams",  # no (#NN): byte-compat case
    "Make backoff explicit: add apply_backoff to RetryPolicy (#7)",
    "chore: tidy notes (#8)",
    "docs: readme",
]


class _StubProvider:
    """Deterministic LLMProvider stand-in: one real + one fabricated claim, and a call counter."""

    model_id = "stub:mine-test"

    def __init__(self) -> None:
        self.calls = 0
        self.systems: list[str] = []
        self.prompts: list[str] = []

    def complete(self, system: str, user: str, *, max_tokens: int = 1024) -> str:
        self.calls += 1
        self.systems.append(system)
        self.prompts.append(user)
        return json.dumps(
            {
                "summary": "Stub decision.",
                "claims": [
                    {"text": f"introduces {REAL}", "symbol": REAL},
                    {"text": "cites a fabricated helper", "symbol": FAKE},
                ],
            }
        )


class _StubPRProvider:
    """Deterministic PRProvider stand-in with a fetch journal."""

    def __init__(self, prs: dict[int, PRText]) -> None:
        self.prs = prs
        self.fetched: list[int] = []

    def fetch(self, number: int) -> PRText | None:
        self.fetched.append(number)
        return self.prs.get(number)


PR7 = PRText(number=7, title="Make backoff explicit", body="## Why\n\nRetryPolicy needs a knob.")
PR8 = PRText(number=8, title="Tidy notes", body="")


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
                m.artifact.c.prompt_version,
                m.artifact.c.framework_versions,
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
    assert row.confidence == pytest.approx(1 / 3)  # Laplace: 1 kept / (1 kept + 1 dropped + 1)
    assert row.confidence < 1.0  # llm_grounded never reaches the deterministic layer's 1.0
    assert row.prompt_version == "2"  # deliberate friction: catches an un-bumped prompt change
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


def test_prs_enriches_payload_and_identity(engine: Engine, tmp_path: Path) -> None:
    """ADR slice 2: PR text enriches payload + prompt, and its digest is identity-bearing."""
    shas = _index_history(engine, tmp_path)
    # force=True: identical fixture content = identical shas across this session DB, so an
    # earlier test may have left enriched rows behind — re-mine to pin a true plain baseline
    _mine(engine, tmp_path, shas[-1], force=True)
    plain = {s: bytes(r.artifact_id) for s, r in _decision_rows(engine, shas).items()}

    prs = _StubPRProvider({7: PR7, 8: PR8})
    result, _ = _mine(engine, tmp_path, shas[-1], force=True, pr_provider=prs)
    assert result.pr_enriched == 1  # (#7) stored with PR text; (#8) dies on the floor
    assert result.pr_fetch_failed == 0
    assert prs.fetched == [8, 7]  # newest-first walk; root/docs commits never hit the provider

    rows = _decision_rows(engine, shas)
    enriched = rows[shas[1]]
    assert enriched.payload["pr_number"] == 7
    assert enriched.payload["pr_title"] == PR7.title
    assert enriched.payload["pr_body"] == PR7.body
    assert enriched.prompt_version == "2+pr1"  # the enriched prompt shape carries its own pin
    digest = enriched.framework_versions["pr_text"]
    assert len(digest) == 12 and all(c in "0123456789abcdef" for c in digest)
    assert bytes(enriched.artifact_id) != plain[shas[1]]  # enrichment IS a new identity
    # the root commit has no (#NN): byte-compatible with plain mining even under --prs
    root = rows[shas[0]]
    assert bytes(root.artifact_id) == plain[shas[0]]
    assert "pr_number" not in root.payload
    assert root.framework_versions == {}

    # LLM-cost idempotency holds under --prs too: stored decisions are never re-fetched
    fetched_before = len(prs.fetched)
    rerun, _ = _mine(engine, tmp_path, shas[-1], pr_provider=prs)
    assert rerun.skipped_already_mined == 2
    assert len(prs.fetched) == fetched_before + 1  # only the no-decision (#8) commit re-asks


def test_claims_still_span_validated_with_pr(engine: Engine, tmp_path: Path) -> None:
    """The PR section feeds the prompt, but the floor still validates against changed spans."""
    shas = _index_history(engine, tmp_path)
    prs = _StubPRProvider({7: PR7, 8: PR8})
    stub = _StubProvider()
    mine_history(
        engine, str(tmp_path), stub, start_sha=shas[-1], force=True, pr_provider=prs
    )
    enriched = _decision_rows(engine, shas)[shas[1]]
    symbols = [c["symbol"] for c in enriched.payload["claims"]]
    assert REAL in symbols
    assert FAKE not in symbols
    assert enriched.confidence == pytest.approx(1 / 3)
    pr_prompt = next(p for p in stub.prompts if "PR #7:" in p)
    assert PR7.body in pr_prompt  # the PR section sits in the user prompt ...
    assert any("pull-request description" in s for s in stub.systems)  # ... under _SYSTEM_PR


def test_pr_edit_changes_artifact_id(engine: Engine, tmp_path: Path) -> None:
    """Anti-silent-swap: editing a merged PR's text yields a NEW artifact identity."""
    shas = _index_history(engine, tmp_path)
    _mine(engine, tmp_path, shas[-1], force=True, pr_provider=_StubPRProvider({7: PR7}))
    first = bytes(_decision_rows(engine, shas)[shas[1]].artifact_id)

    edited = PRText(number=7, title=PR7.title, body=PR7.body + "\n\nEdited after merge.")
    _mine(engine, tmp_path, shas[-1], force=True, pr_provider=_StubPRProvider({7: edited}))
    row = _decision_rows(engine, shas)[shas[1]]
    assert bytes(row.artifact_id) != first  # manifest re-pointed to the new identity
    assert row.payload["pr_body"] == edited.body


def test_pr_fetch_failure_degrades_to_plain(engine: Engine, tmp_path: Path) -> None:
    """A failed fetch is a property of the RUN: the artifact is byte-identical to plain mining."""
    shas = _index_history(engine, tmp_path)
    _mine(engine, tmp_path, shas[-1], force=True)  # pin a true plain baseline (shared session DB)
    plain = {s: bytes(r.artifact_id) for s, r in _decision_rows(engine, shas).items()}

    failing = _StubPRProvider({})  # every fetch returns None
    result, _ = _mine(engine, tmp_path, shas[-1], force=True, pr_provider=failing)
    assert result.pr_fetch_failed == 2  # (#7) and (#8) both degraded
    assert result.pr_enriched == 0
    rows = _decision_rows(engine, shas)
    assert bytes(rows[shas[1]].artifact_id) == plain[shas[1]]  # identical identity ...
    assert "pr_number" not in rows[shas[1]].payload  # ... and no pr_* payload keys
    assert rows[shas[1]].framework_versions == {}


def test_origin_slug_and_subject_parsing(tmp_path: Path) -> None:
    repo = pygit2.init_repository(str(tmp_path / "slugrepo"), bare=True)
    assert origin_slug(repo) is None  # no origin remote
    for url in (
        "https://github.com/o/r",
        "https://github.com/o/r.git",
        "git@github.com:o/r.git",
        "ssh://git@github.com/o/r",
    ):
        repo.remotes.delete("origin") if "origin" in [rm.name for rm in repo.remotes] else None
        repo.remotes.create("origin", url)
        assert origin_slug(repo) == "o/r", url
    repo.remotes.delete("origin")
    repo.remotes.create("origin", "https://gitlab.com/o/r.git")
    assert origin_slug(repo) is None  # the adapter is github.com-only (documented)

    assert pr_number_from_subject("feat: thing (#42)") == 42
    assert pr_number_from_subject("feat: thing (#42)\n\nbody (#7)") == 42
    assert pr_number_from_subject('Revert "feat: thing (#28)" (#30)') == 30
    assert pr_number_from_subject("feat: no pr ref") is None
    assert pr_number_from_subject("") is None


def test_github_provider_fetch_and_degrade(monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import urllib.error
    import urllib.request

    seen: dict[str, Any] = {}

    class _Resp(io.BytesIO):
        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *args: object) -> None:
            self.close()

    def ok(request: urllib.request.Request, timeout: float = 0) -> _Resp:
        seen["url"] = request.full_url
        seen["auth"] = request.headers.get("Authorization")
        return _Resp(b'{"title": "T", "body": null}')

    calls = {"n": 0}

    def counting_ok(request: urllib.request.Request, timeout: float = 0) -> _Resp:
        calls["n"] += 1
        return ok(request, timeout)

    monkeypatch.setattr(urllib.request, "urlopen", counting_ok)
    provider = GitHubPRProvider("o/r", token="tok")
    pr = provider.fetch(5)
    assert pr == PRText(number=5, title="T", body="")  # null body normalized to ""
    assert seen["url"] == "https://api.github.com/repos/o/r/pulls/5"
    assert seen["auth"] == "Bearer tok"
    assert provider.fetch(5) == pr and calls["n"] == 1  # memoized: one network call per PR

    def not_found(request: urllib.request.Request, timeout: float = 0) -> _Resp:
        raise urllib.error.HTTPError(request.full_url, 404, "nf", None, None)  # type: ignore[arg-type]

    monkeypatch.setattr(urllib.request, "urlopen", not_found)
    assert GitHubPRProvider("o/r").fetch(5) is None  # fail-soft

    def netdown(request: urllib.request.Request, timeout: float = 0) -> _Resp:
        raise urllib.error.URLError("down")

    monkeypatch.setattr(urllib.request, "urlopen", netdown)
    assert GitHubPRProvider("o/r").fetch(5) is None  # fail-soft
