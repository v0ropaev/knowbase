"""TRACKED (NON-gating) — Tier 3: LLM-judged answer quality, knowbase context vs RAG context (§9).

Beyond Tier-3 *recall* (tier3_rag_test), this measures *answer quality*: an answerer LLM answers
each question from the knowbase-grounded context and, separately, from the RAG-over-source context.
A judge LLM scores each answer against a hand-written GOLD reference for **accuracy**, and flags
**hallucination** (a claim unsupported by that arm's own provided context).

This is **nightly, key-gated, and NON-gating**: it self-skips without an API key, asserts only that
the A/B actually ran (never the win), and writes a metrics JSON for the CI artifact. The
deterministic Tier-3 recall gate remains the hard floor. Self-judging bias is accepted here (see
DESIGN §9); a distinct judge model can be set via KB_LLM_JUDGE_MODEL.
"""

from __future__ import annotations

import json
import os
import re

import pytest
from sqlalchemy import Engine

from kb.daemon.pipeline import index_commit
from kb.embed.population import embed_snapshot
from kb.eval._fixtures import make_git_repo
from kb.eval.questions import ENTITY_FILES, GOLD, QUESTIONS
from kb.eval.tier1_api_test import FILES
from kb.extract.deterministic.entities import EntityExtractor
from kb.extract.deterministic.fastapi_contract import FastAPIExtractor
from kb.llm.providers import default_llm_provider, has_llm_key
from kb.mcp.records import summarize
from kb.rag.baseline import index_rag_baseline, rag_retrieve
from kb.store import queries as q
from kb.store.queries import provenance_for_artifact

pytestmark = pytest.mark.skipif(
    not has_llm_key(), reason="no LLM API key (set ANTHROPIC_API_KEY or OPENAI_API_KEY)"
)

K = 5
WIN_ACCURACY_MARGIN = 0.15  # pre-registered: knowbase wins iff acc margin >= this AND hall <= RAG's

ANSWER_SYSTEM = (
    "Answer the question using ONLY the provided context about a codebase. Be concise and "
    "specific. If the context does not contain the answer, say you cannot tell from the context."
)
JUDGE_SYSTEM = "You are a strict grader. Respond with ONE JSON object and nothing else."


@pytest.fixture(scope="module")
def prepared(engine: Engine, tmp_path_factory, st_provider) -> tuple[Engine, str]:
    repo = tmp_path_factory.mktemp("tier3_llm")
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


def _knowbase_context(conn, sha, question, st_provider) -> str:
    qvec = st_provider.embed([question])[0]
    blocks = []
    for row in q.similar_artifacts_by_embedding(conn, sha, qvec, K):
        prov = provenance_for_artifact(conn, sha, row.logical_key)
        prov_str = ", ".join(f"{p.file_path}:{p.start_line}" for p in prov)
        blocks.append(
            f"[{row.logical_key}] kind={row.kind}\n"
            f"summary: {summarize(row.kind, row.payload)}\n"
            f"details: {json.dumps(row.payload, default=str)[:600]}\n"
            f"provenance: {prov_str}"
        )
    return "\n\n".join(blocks) if blocks else "(no knowledge units found)"


def _rag_context(conn, sha, question, st_provider) -> str:
    hits = rag_retrieve(conn, question, st_provider, sha, K)
    blocks = [f"# {h.file_path}:{h.start_line}-{h.end_line}\n{h.raw_text}" for h in hits]
    return "\n\n".join(blocks) if blocks else "(no source chunks found)"


def _answer(provider, question: str, context: str) -> str:
    return provider.complete(ANSWER_SYSTEM, f"Context:\n{context}\n\nQuestion: {question}")


def _judge(provider, question: str, gold: str, answer: str, context: str) -> dict:
    prompt = (
        f"Question: {question}\n"
        f"Gold answer: {gold}\n"
        f"Candidate answer: {answer}\n\n"
        f"Candidate's source context:\n{context}\n\n"
        'Return JSON {"accuracy": 0|1, "hallucinated": 0|1, "note": "..."} where accuracy=1 iff '
        "the candidate conveys the gold answer's key facts (paraphrase is fine), and "
        "hallucinated=1 iff the candidate states a fact not supported by its source context."
    )
    return _parse_verdict(provider.complete(JUDGE_SYSTEM, prompt, max_tokens=300))


def _parse_verdict(raw: str) -> dict:
    match = re.search(r"\{.*\}", raw, re.S)
    if match:
        try:
            data = json.loads(match.group(0))
            return {
                "accuracy": int(bool(data.get("accuracy"))),
                "hallucinated": int(bool(data.get("hallucinated"))),
                "note": str(data.get("note", ""))[:200],
                "parse_error": False,
            }
        except json.JSONDecodeError:
            pass
    return {"accuracy": 0, "hallucinated": 0, "note": "unparseable", "parse_error": True}


def test_llm_judged_ab(prepared: tuple[Engine, str], st_provider) -> None:
    engine, sha = prepared
    answerer = default_llm_provider()
    judge = default_llm_provider(os.environ.get("KB_LLM_JUDGE_MODEL"))

    records = []
    with engine.connect() as conn:
        for question in QUESTIONS:
            gold = GOLD[question.id]
            kb_ctx = _knowbase_context(conn, sha, question.question, st_provider)
            rag_ctx = _rag_context(conn, sha, question.question, st_provider)
            kb_ans = _answer(answerer, question.question, kb_ctx)
            rag_ans = _answer(answerer, question.question, rag_ctx)
            kb_v = _judge(judge, question.question, gold, kb_ans, kb_ctx)
            rag_v = _judge(judge, question.question, gold, rag_ans, rag_ctx)
            records.append({"id": question.id, "knowbase": kb_v, "rag": rag_v})

    n = len(records)

    def mean(arm: str, key: str) -> float:
        return sum(r[arm][key] for r in records) / n

    kb_acc, rag_acc = mean("knowbase", "accuracy"), mean("rag", "accuracy")
    kb_hall, rag_hall = mean("knowbase", "hallucinated"), mean("rag", "hallucinated")
    win = (kb_acc - rag_acc >= WIN_ACCURACY_MARGIN) and (kb_hall <= rag_hall)

    summary = {
        "answerer": answerer.model_id,
        "judge": judge.model_id,
        "n": n,
        "k": K,
        "knowbase": {"accuracy": kb_acc, "hallucination": kb_hall},
        "rag": {"accuracy": rag_acc, "hallucination": rag_hall},
        "pre_registered_threshold": {
            "accuracy_margin": WIN_ACCURACY_MARGIN,
            "hallucination": "knowbase <= rag",
        },
        "win": win,
        "records": records,
    }
    out_path = os.environ.get("KB_LLM_AB_METRICS", "tier3_llm_ab_metrics.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)

    print(
        f"\n[tier3-llm] answerer={answerer.model_id} judge={judge.model_id} "
        f"n={n} (TRACKED, non-gating)\n"
        f"  accuracy:      knowbase={kb_acc:.3f}  RAG={rag_acc:.3f}\n"
        f"  hallucination: knowbase={kb_hall:.3f}  RAG={rag_hall:.3f}\n"
        f"  pre-registered win (acc margin >= {WIN_ACCURACY_MARGIN} and hall <= RAG): "
        f"{'PASS' if win else 'not met'}  -> {out_path}"
    )

    # NON-gating: assert only that the A/B actually ran for every question — never the win.
    assert n == len(QUESTIONS)
    assert all(r["knowbase"]["note"] is not None for r in records)
