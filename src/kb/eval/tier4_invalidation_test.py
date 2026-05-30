"""HARD GATE — Tier 4: one-hop invalidation set-equality (DESIGN.md §9).

A content diff must invalidate EXACTLY the artifacts whose grounding span changed — no
over-invalidation, no stale survival. A version bump must change every artifact id (full
invalidation), kept distinct from content-diff invalidation.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine

from kb.daemon.pipeline import index_commit
from kb.eval._fixtures import make_git_repo
from kb.extract.deterministic.imports import ImportExtractor
from kb.git import diff as gitdiff
from kb.git import repo as gitrepo
from kb.ids import compute_artifact_id
from kb.store.queries import invalidated_artifact_ids, logical_keys_for_artifacts

# A: orders imports billing.  B: orders imports warehouse instead (make() unchanged).
COMMIT_A = {
    "src/shop/__init__.py": "from shop.orders import make\n",
    "src/shop/orders.py": "from shop.billing import charge\n\ndef make():\n    return charge()\n",
    "src/shop/billing.py": "def charge():\n    return 1\n",
}
COMMIT_B = {
    "src/shop/__init__.py": "from shop.orders import make\n",
    "src/shop/orders.py": "from shop.warehouse import charge\n\ndef make():\n    return charge()\n",
    "src/shop/billing.py": "def charge():\n    return 1\n",
    "src/shop/warehouse.py": "def charge():\n    return 1\n",
}


def test_content_diff_invalidates_exactly_the_changed_edge(engine: Engine, tmp_path: Path) -> None:
    sha_a, sha_b = make_git_repo(tmp_path, [COMMIT_A, COMMIT_B])
    index_commit(engine, str(tmp_path), sha_a, extractors=[ImportExtractor()])

    repo = gitrepo.open_repo(str(tmp_path))
    seed = gitdiff.changed_span_ids(repo, sha_a, sha_b)
    with engine.connect() as conn:
        invalidated = invalidated_artifact_ids(conn, sha_a, seed)
        keys = logical_keys_for_artifacts(conn, sha_a, invalidated)

    # exactly the edge whose import statement changed; shop->shop.orders (unchanged) survives
    assert keys == {"import:shop.orders->shop.billing"}


def test_no_diff_invalidates_nothing(engine: Engine, tmp_path: Path) -> None:
    sha_a, _ = make_git_repo(tmp_path, [COMMIT_A, COMMIT_B])
    index_commit(engine, str(tmp_path), sha_a, extractors=[ImportExtractor()])

    repo = gitrepo.open_repo(str(tmp_path))
    seed = gitdiff.changed_span_ids(repo, sha_a, sha_a)
    assert seed == set()
    with engine.connect() as conn:
        assert invalidated_artifact_ids(conn, sha_a, seed) == set()


def test_version_bump_changes_artifact_id() -> None:
    span = bytes([0x01]) * 32
    v1 = compute_artifact_id(
        derived_from_span_ids=[span], extractor_id="imports", extractor_version="1"
    )
    v2 = compute_artifact_id(
        derived_from_span_ids=[span], extractor_id="imports", extractor_version="2"
    )
    assert v1 != v2  # a version bump fully invalidates (every id changes)
