"""Git ingest: resolve commits and enumerate Python blobs at a SHA (DESIGN.md §7 step 1)."""

from __future__ import annotations

from pathlib import Path

from kb.eval._fixtures import make_git_repo
from kb.git import repo as gitrepo


def test_lists_python_files_at_sha(tmp_path: Path) -> None:
    shas = make_git_repo(
        tmp_path,
        [
            {
                "src/shop/__init__.py": "",
                "src/shop/orders.py": "import os\n",
                "README.md": "not python\n",
            }
        ],
    )
    repo = gitrepo.open_repo(str(tmp_path))
    files = dict(gitrepo.iter_python_files_at(repo, shas[0]))
    assert set(files) == {"src/shop/__init__.py", "src/shop/orders.py"}  # README.md excluded
    assert files["src/shop/orders.py"] == b"import os\n"


def test_resolve_sha_and_parents(tmp_path: Path) -> None:
    shas = make_git_repo(
        tmp_path,
        [
            {"a.py": "x = 1\n"},
            {"a.py": "x = 2\n"},
        ],
    )
    repo = gitrepo.open_repo(str(tmp_path))
    assert gitrepo.commit_sha(repo, shas[1]) == shas[1]
    assert gitrepo.parent_shas(repo, shas[1]) == [shas[0]]
    assert gitrepo.parent_shas(repo, shas[0]) == []


def test_blob_read_does_not_need_checkout(tmp_path: Path) -> None:
    """A historical SHA's content is read from the object DB, independent of the working tree."""
    shas = make_git_repo(
        tmp_path,
        [
            {"m.py": "old = 1\n"},
            {"m.py": "new = 2\n"},
        ],
    )
    repo = gitrepo.open_repo(str(tmp_path))
    old = dict(gitrepo.iter_python_files_at(repo, shas[0]))
    new = dict(gitrepo.iter_python_files_at(repo, shas[1]))
    assert old["m.py"] == b"old = 1\n"
    assert new["m.py"] == b"new = 2\n"
