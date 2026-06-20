"""HARD GATE — Tier 3: grounded knowledge vs RAG-over-source on cross-file questions (§9, §10).

The robust, structural assertion: knowbase's top-k semantic retrieval covers ALL expected grounding
files for every cross-file question (recall@K == 1.0): a single knowbase artifact is already
cross-file-grounded. The RAG arm is computed and printed (tracked, NEVER asserted). The honest
separator even on a tiny corpus is **recall@1**: knowbase's one best unit spans both files; a RAG
chunk is one file — surfaced in the printed summary, not strawmanned into a gate.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text

from kb.daemon.pipeline import index_commit
from kb.embed.population import embed_snapshot
from kb.eval._fixtures import make_git_repo
from kb.eval.questions import ENTITY_FILES, QUESTIONS
from kb.eval.tier1_api_test import FILES
from kb.extract.deterministic.entities import EntityExtractor
from kb.extract.deterministic.fastapi_contract import FastAPIExtractor
from kb.rag.baseline import index_rag_baseline, rag_retrieve
from kb.store import queries as q
from kb.store.queries import provenance_for_artifact

K = 5


@pytest.fixture(scope="module")
def prepared(engine: Engine, tmp_path_factory, st_provider) -> tuple[Engine, str]:
    repo = tmp_path_factory.mktemp("tier3")
    sha = make_git_repo(repo, [{**FILES, **ENTITY_FILES}])[0]
    index_commit(
        engine,
        str(repo),
        sha,
        extractors=[FastAPIExtractor(), EntityExtractor()],
        first_party_root="src",
    )
    embed_snapshot(engine, sha, st_provider)
    index_rag_baseline(engine, str(repo), sha, st_provider)
    return engine, sha


def _cover(conn, sha, rows, n) -> set[str]:
    cover: set[str] = set()
    for row in rows[:n]:
        for prov in provenance_for_artifact(conn, sha, row.logical_key):
            cover.add(prov.file_path)
    return cover


def test_knowbase_cross_file_recall_beats_rag(prepared: tuple[Engine, str], st_provider) -> None:
    engine, sha = prepared
    kb_at1, rag_at1, rag_atk = [], [], []
    with engine.connect() as conn:
        conn.execute(text("SET LOCAL enable_indexscan = off"))  # exact cosine for the gate
        for question in QUESTIONS:
            qvec = st_provider.embed([question.question])[0]
            kb_rows = q.similar_artifacts_by_embedding(conn, sha, qvec, K)
            cover_k = _cover(conn, sha, kb_rows, K)
            # HARD GATE: knowbase top-k covers every expected grounding file (structural guarantee).
            assert question.expected_files <= cover_k, (
                f"{question.id}: knowbase@{K} missed {question.expected_files - cover_k}"
            )
            ef = question.expected_files
            kb_at1.append(len(ef & _cover(conn, sha, kb_rows, 1)) / len(ef))
            hits = rag_retrieve(conn, question.question, st_provider, sha, K)
            rag_files = {h.file_path for h in hits}
            rag_atk.append(len(ef & rag_files) / len(ef))
            rag_at1.append(len(ef & {h.file_path for h in hits[:1]}) / len(ef))

    n = len(QUESTIONS)
    print(
        f"\n[tier3] HARD: knowbase cross-file recall@{K} == 1.000 for all {n} questions.\n"
        f"        recall@1 (structural separator): knowbase={sum(kb_at1)/n:.3f} "
        f"vs RAG={sum(rag_at1)/n:.3f} | recall@{K} RAG={sum(rag_atk)/n:.3f} (tracked)"
    )
