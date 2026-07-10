"""ADR-candidate mining from local git history — a separate, key-gated pass (DESIGN.md §4, §9).

ADRs are not extractable from code (§4): the *why* lives in commit messages. The provenance bridge
that keeps D5 intact is the commit's own diff — a ``decision`` artifact is grounded on the spans
its source commit CHANGED (present at the commit, absent at its first parent; role ``changed``),
so the decision candidate points at the exact code that carries it. The commit message is stored
VERBATIM in the payload as a fact (immutable, pinned by the sha in the logical key) — it is
context, never grounding. Claims pass the same deterministic ``validate_claims`` floor as
``kb describe``: a claim must cite an identifier of the changed code, fabricated claims are
dropped, and a commit with no surviving claim stores nothing.

Slice 2 (``kb mine --prs``, strictly opt-in — the only networked step in the toolchain): a
squash-merged commit's PR title/body (``kb.git.forge``) enrich the prompt and the payload. PR
text is MUTABLE after merge, so a digest of the capped text is folded into
``framework_versions`` (identity-bearing — the sink-registry precedent): an edited PR can never
silently swap a payload under an unchanged id. A failed fetch degrades to the byte-identical
plain artifact and is counted per run (a failure is a property of the RUN, not of the
knowledge). Exactly two identities exist per (sha, model): plain and enriched(digest).

Never on the ``kb index`` path. Idempotent per (model, prompt): ``artifact_id`` folds in
model_id + prompt; one artifact per mined commit (``decision:{sha}`` — deterministic, unlike an
LLM-written slug).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import pygit2
from sqlalchemy import Connection, Engine, select

from kb.extract.base import DerivedEdge, ExtractedArtifact
from kb.extract.semantic.describe import _parse_json
from kb.extract.semantic.grounding import validate_claims
from kb.git.diff import changed_span_ids
from kb.git.forge import PRProvider, PRText, pr_number_from_subject
from kb.git.repo import open_repo, resolve_commit
from kb.llm.providers import LLMProvider
from kb.store import models as m
from kb.store.queries import ChangedSpanRow, changed_span_rows, is_sha_indexed
from kb.store.writer import write_grounded_artifact, write_snapshot_entry

EXTRACTOR_ID = "llm_mine"
EXTRACTOR_VERSION = "2"  # v2: Laplace add-one confidence (kept/(kept+dropped+1))
PROMPT_VERSION = "2"  # v2: claim text must be a factual sentence, never just the identifier
_PR_PROMPT_VERSION = "2+pr1"  # the enriched branch pins its own prompt shape (repo precedent)
_BODY_CAP = 6000  # prompt changed-span body cap (validation still runs over the grounding set)
_MESSAGE_CAP = 2000  # verbatim commit-message cap (prompt + payload)
_PR_TITLE_CAP = 300  # PR title cap (prompt + payload + digest input)
_PR_BODY_CAP = _MESSAGE_CAP  # PR body cap (symmetric with the commit message)
_MAX_GROUNDING_SPANS = 40  # grounding-set cap for huge commits (retro-flagged, never silent)
_FILES_CAP = 20  # files_changed payload/prompt list cap

_SYSTEM = (
    "You extract the technical or architectural DECISION a git commit records, using ONLY the "
    "commit message and the changed source spans provided. Respond with STRICT JSON and nothing "
    'else: {"summary": "<= 2 sentences: the decision and its why", "claims": [{"text": "...", '
    '"symbol": "<one identifier that appears verbatim in the changed code>"}]}. Each claim\'s '
    '"text" must be one factual sentence stating what was decided or changed - never just the '
    "identifier itself. Every claim must cite a real identifier from the changed code; never "
    'invent names. If the commit records no decision, return {"summary": "", "claims": []}.'
)

# The enriched branch may NOT run under a system prompt saying "ONLY the commit message":
# the PR description is a legitimate extra input there.
_SYSTEM_PR = (
    "You extract the technical or architectural DECISION a git commit records, using ONLY the "
    "commit message, its pull-request description, and the changed source spans provided. "
    "Respond with STRICT JSON and nothing "
    'else: {"summary": "<= 2 sentences: the decision and its why", "claims": [{"text": "...", '
    '"symbol": "<one identifier that appears verbatim in the changed code>"}]}. Each claim\'s '
    '"text" must be one factual sentence stating what was decided or changed - never just the '
    "identifier itself. Every claim must cite a real identifier from the changed code; never "
    'invent names. If the commit records no decision, return {"summary": "", "claims": []}.'
)


@dataclass(frozen=True)
class MineResult:
    start_sha: str
    scanned: int
    mined: int
    skipped_merges: int
    skipped_unindexed: int
    skipped_already_mined: int
    dropped_claims: int
    pr_enriched: int = 0  # stored decisions that carry PR text
    pr_fetch_failed: int = 0  # PR fetches that degraded to plain mining


@dataclass(frozen=True)
class _MineOutcome:
    stored: bool
    dropped: int
    enriched: bool
    fetch_failed: bool


def mine_history(
    engine: Engine,
    repo_path: str,
    provider: LLMProvider,
    *,
    start_sha: str,
    max_commits: int = 20,
    force: bool = False,
    first_party_root: str | None = None,
    pr_provider: PRProvider | None = None,
) -> MineResult:
    """Mine decision candidates from the first-parent chain starting at ``start_sha``.

    Walks at most ``max_commits`` commits (LLM calls are the cost — the walk itself is cheap).
    Merge commits are skipped (squash-merge flow is fully covered by ``pr_provider``; grounding
    a classic merge on its first-parent diff is an open correctness question), unindexed commits
    are skipped (grounding needs the snapshot's occurrences), already-mined commits are skipped
    unless ``force`` (LLM-cost idempotency; re-running never pays for stored decisions). With a
    ``pr_provider``, a commit whose subject carries ``(#NN)`` is enriched with the PR's
    title/body; without one, behavior is byte-identical to slice 1. Each commit is one
    transaction, so a crash keeps every decision mined so far.
    """
    repo = open_repo(repo_path)
    scanned = mined = skipped_merges = skipped_unindexed = skipped_already_mined = 0
    dropped_total = pr_enriched = pr_fetch_failed = 0
    current: str | None = str(resolve_commit(repo, start_sha).id)
    while current is not None and scanned < max_commits:
        scanned += 1
        commit = resolve_commit(repo, current)
        parents = [str(oid) for oid in commit.parent_ids]
        with engine.begin() as conn:
            if len(parents) > 1:
                skipped_merges += 1
            elif not is_sha_indexed(conn, current):
                skipped_unindexed += 1
            elif not force and _already_mined(conn, current):
                skipped_already_mined += 1
            else:
                outcome = _mine_one(
                    conn,
                    repo,
                    provider,
                    sha=current,
                    parent=parents[0] if parents else None,
                    first_party_root=first_party_root,
                    pr_provider=pr_provider,
                )
                mined += int(outcome.stored)
                dropped_total += outcome.dropped
                pr_enriched += int(outcome.enriched and outcome.stored)
                pr_fetch_failed += int(outcome.fetch_failed)
        current = parents[0] if parents else None
    return MineResult(
        start_sha=start_sha,
        scanned=scanned,
        mined=mined,
        skipped_merges=skipped_merges,
        skipped_unindexed=skipped_unindexed,
        skipped_already_mined=skipped_already_mined,
        dropped_claims=dropped_total,
        pr_enriched=pr_enriched,
        pr_fetch_failed=pr_fetch_failed,
    )


def _already_mined(conn: Connection, sha: str) -> bool:
    stmt = (
        select(m.snapshot_entry.c.logical_key)
        .where(m.snapshot_entry.c.sha == sha, m.snapshot_entry.c.logical_key == f"decision:{sha}")
        .limit(1)
    )
    return conn.execute(stmt).first() is not None


def _mine_one(
    conn: Connection,
    repo: pygit2.Repository,
    provider: LLMProvider,
    *,
    sha: str,
    parent: str | None,
    first_party_root: str | None,
    pr_provider: PRProvider | None = None,
) -> _MineOutcome:
    """Mine one commit.

    A root commit is mined against the empty tree (its whole tree IS the diff — initial commits
    often carry a decision). A commit whose diff touches no indexed span (docs-only) never calls
    the LLM — nor the PR provider. A ``decision`` artifact is stored (grounded on the changed
    spans, role ``changed``) only if >= 1 claim survives span-validation; otherwise nothing is
    stored (anti-hallucination). With PR text, the artifact carries pr_* payload facts and a
    text digest in ``framework_versions``; a failed fetch degrades to the byte-identical plain
    artifact.
    """
    commit = resolve_commit(repo, sha)
    if parent is None:
        span_ids = None  # root: every snapshot span is "changed" vs the empty tree
    else:
        span_ids = changed_span_ids(repo, sha, parent, first_party_root=first_party_root)
        if not span_ids:
            return _MineOutcome(False, 0, False, False)  # docs-only: no LLM, no PR fetch
    rows = changed_span_rows(conn, sha, span_ids)
    if not rows:
        return _MineOutcome(False, 0, False, False)  # spans outside the indexed snapshot
    total = len(rows)
    grounding = rows[:_MAX_GROUNDING_SPANS]
    limitations = ["grounding_capped"] if total > _MAX_GROUNDING_SPANS else []
    message = commit.message[:_MESSAGE_CAP]
    files_changed = sorted({r.file_path for r in rows})[:_FILES_CAP]

    pr: PRText | None = None
    fetch_failed = False
    if pr_provider is not None:
        pr_number = pr_number_from_subject(commit.message)
        if pr_number is not None:
            pr = pr_provider.fetch(pr_number)
            fetch_failed = pr is None

    prompt = _build_prompt(sha, message, files_changed, grounding, pr)
    system = _SYSTEM if pr is None else _SYSTEM_PR
    data = _parse_json(provider.complete(system, prompt, max_tokens=600))
    if data is None:
        return _MineOutcome(False, 0, False, fetch_failed)
    raw_claims = [c for c in data.get("claims", []) if isinstance(c, dict)]
    kept, dropped = validate_claims(
        raw_claims, [r.raw_text for r in grounding], [r.fq_symbol_path for r in grounding]
    )
    if not kept:
        return _MineOutcome(False, len(dropped), False, fetch_failed)  # store nothing
    payload = {
        "sha": sha,
        "message": message,
        "author": commit.author.name,
        "authored_at": commit.commit_time,
        "summary": str(data.get("summary", ""))[:500],
        "claims": kept,
        "dropped_claims": len(dropped),
        "files_changed": files_changed,
        "total_changed_spans": total,
        "limitations": limitations,
    }
    framework_versions: dict[str, str] = {}
    if pr is not None:
        payload["pr_number"] = pr.number
        payload["pr_title"] = pr.title[:_PR_TITLE_CAP]
        payload["pr_body"] = pr.body[:_PR_BODY_CAP]
        # PR text is mutable after merge: pin the CAPPED text (what actually fed the prompt)
        # into identity, so an edited PR yields a NEW id instead of a silent payload swap.
        framework_versions["pr_text"] = _pr_text_digest(pr)
    artifact = ExtractedArtifact(
        kind="decision",
        logical_key=f"decision:{sha}",
        payload=payload,
        derived_from=[DerivedEdge(r.span_id, "changed") for r in grounding],
        extractor_id=EXTRACTOR_ID,
        extractor_version=EXTRACTOR_VERSION,
        prompt_version=PROMPT_VERSION if pr is None else _PR_PROMPT_VERSION,
        model_id=provider.model_id,
        framework_versions=framework_versions,
        is_deterministic=False,
        # Laplace add-one: the +1 prices unknown-unknowns, so llm_grounded never reaches 1.0
        confidence=len(kept) / (len(kept) + len(dropped) + 1),
    )
    artifact_id = write_grounded_artifact(conn, artifact)
    write_snapshot_entry(conn, sha, artifact.logical_key, artifact_id)
    return _MineOutcome(True, len(dropped), pr is not None, fetch_failed)


def _pr_text_digest(pr: PRText) -> str:
    capped = f"{pr.title[:_PR_TITLE_CAP]}\x1f{pr.body[:_PR_BODY_CAP]}"
    return hashlib.sha256(capped.encode("utf-8")).hexdigest()[:12]


def _build_prompt(
    sha: str,
    message: str,
    files_changed: list[str],
    grounding: list[ChangedSpanRow],
    pr: PRText | None = None,
) -> str:
    parts: list[str] = []
    used = 0
    for row in grounding:
        block = f"# {row.fq_symbol_path}\n{row.raw_text}"
        if parts and used + len(block) > _BODY_CAP:
            break
        parts.append(block)
        used += len(block)
    body = "\n\n".join(parts)
    files = json.dumps(files_changed)
    pr_section = (
        ""
        if pr is None
        else f"PR #{pr.number}: {pr.title[:_PR_TITLE_CAP]}\n{pr.body[:_PR_BODY_CAP]}\n\n"
    )
    return (
        f"Commit {sha[:12]}\nMessage:\n{message}\n\n"
        f"{pr_section}"
        f"Files changed: {files}\n\nChanged spans:\n{body}"
    )
