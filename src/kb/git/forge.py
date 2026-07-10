"""GitHub PR-text adapter — the ONLY networked module in the toolchain (DESIGN.md §4, §11).

Feeds ADR-mining slice 2 (``kb mine --prs``, strictly opt-in): a squash-merged commit's subject
carries its PR number (``... (#NN)``), and the PR's title/body are rich decision prose that the
commit message often abbreviates. PR text is *context for the LLM and a payload fact — never
grounding* (the decision stays grounded on the commit's changed spans, D5), and because PR text
is MUTABLE after merge (unlike a commit message pinned by its sha), the miner folds a digest of
the fetched text into ``framework_versions`` so an edited PR can never silently swap a payload
under an unchanged ``artifact_id`` (the sink-registry precedent).

Fetch failures (404, network down, rate limit, malformed JSON) degrade softly to ``None`` — the
mining run then stores the byte-identical plain artifact and counts the failure; a batch of paid
LLM calls must not die on one flaky request. Never on the ``kb index`` / ``kb serve`` paths.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

import pygit2

_GITHUB_URL = re.compile(
    r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+)/([^/]+?)(?:\.git)?/?$"
)
_SUBJECT_PR = re.compile(r"\(#(\d+)\)\s*$")


@dataclass(frozen=True)
class PRText:
    number: int
    title: str
    body: str  # GitHub's null body is normalized to ""


class PRProvider(Protocol):
    def fetch(self, number: int) -> PRText | None: ...


class GitHubPRProvider:
    """Fetch a PR's title/body from api.github.com (anonymous works for public repos)."""

    def __init__(self, slug: str, token: str | None = None, timeout: float = 10.0) -> None:
        self._slug = slug
        self._token = token
        self._timeout = timeout
        self._cache: dict[int, PRText] = {}  # successes only: a transient failure must not stick

    def fetch(self, number: int) -> PRText | None:
        cached = self._cache.get(number)
        if cached is not None:
            return cached
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "knowbase",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        request = urllib.request.Request(
            f"https://api.github.com/repos/{self._slug}/pulls/{number}", headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError):  # incl. HTTPError, bad JSON
            return None  # fail-soft: the run degrades to plain mining and counts the failure
        if not isinstance(data, dict):
            return None
        pr = PRText(
            number=number,
            title=str(data.get("title") or ""),
            body=str(data.get("body") or ""),
        )
        self._cache[number] = pr
        return pr


def origin_slug(repo: pygit2.Repository) -> str | None:
    """``owner/name`` parsed from the ``origin`` remote, or None (no origin / not github.com)."""
    try:
        url = repo.remotes["origin"].url or ""
    except KeyError:
        return None
    match = _GITHUB_URL.match(url)
    if match is None:
        return None
    return f"{match.group(1)}/{match.group(2)}"


def pr_number_from_subject(message: str) -> int | None:
    """The squash-merge PR number from a commit subject (``... (#NN)``), or None.

    Anchored to the subject's end, so ``Revert "... (#28)" (#30)`` correctly yields the revert's
    own PR (#30). Classic ``Merge pull request #N`` commits are NOT matched — merge commits stay
    skipped by the miner (grounding a merge on its first-parent diff is an open question).
    """
    subject = message.splitlines()[0] if message else ""
    match = _SUBJECT_PR.search(subject)
    return int(match.group(1)) if match else None
