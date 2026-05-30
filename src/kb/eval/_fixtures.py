"""Build throwaway git repositories programmatically for tests.

Each commit is described by a full ``{relative_path: source}`` mapping; the builder makes the
working tree match exactly (so files can appear, change, and disappear across commits) and returns
the resulting SHAs in order. Avoids vendoring nested ``.git`` directories into the main repo.
"""

from __future__ import annotations

from pathlib import Path

import pygit2

_SIG = pygit2.Signature("knowbase-test", "test@knowbase.local", time=1_700_000_000, offset=0)


def make_git_repo(path: Path, commits: list[dict[str, str]]) -> list[str]:
    """Create a repo at ``path`` with one commit per mapping; return the ordered list of SHAs."""
    repo = pygit2.init_repository(str(path), bare=False)
    shas: list[str] = []
    parents: list[str] = []
    for files in commits:
        _clear_py_files(path)
        index = repo.index
        index.clear()
        for rel, content in files.items():
            target = path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            index.add(rel)
        index.write()
        tree = index.write_tree()
        oid = repo.create_commit("HEAD", _SIG, _SIG, "commit", tree, parents)
        parents = [oid]
        shas.append(str(oid))
    return shas


def _clear_py_files(path: Path) -> None:
    for py in path.rglob("*.py"):
        if ".git" not in py.parts:
            py.unlink()
