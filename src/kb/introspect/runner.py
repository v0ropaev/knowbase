"""Parent side of ``kb introspect``: spawn the sandboxed child and parse its OpenAPI JSON."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


class IntrospectError(RuntimeError):
    """The sandboxed introspection failed (timeout, nonzero exit, or invalid output)."""


@dataclass(frozen=True)
class IntrospectResult:
    app_import: str
    openapi: dict[str, Any]


def introspect_app(
    app_import: str,
    *,
    cwd: str | None = None,
    extra_sys_path: Sequence[str] = (),
    timeout_s: float = 15.0,
    python: str | None = None,
) -> IntrospectResult:
    """Import ``<module:appvar>`` in a sandboxed subprocess and return its ``app.openapi()`` dict.

    ``cwd`` is the repo root the app imports from; ``extra_sys_path`` are subdirs of it (e.g.
    ``["src"]``) added to ``PYTHONPATH``. The repo root itself is always added (flat-layout apps).
    """
    base = cwd or os.getcwd()
    search = [os.path.join(base, p) for p in extra_sys_path if p]
    search.append(base)
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    previous = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join([*search, *([previous] if previous else [])])

    cmd = [python or sys.executable, "-m", "kb.introspect._child", app_import]
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout_s
        )
    except subprocess.TimeoutExpired as exc:
        raise IntrospectError(f"introspect timed out after {timeout_s}s") from exc

    if proc.returncode != 0:
        raise IntrospectError(
            f"introspect failed (exit {proc.returncode}): {proc.stderr.strip()[:500]}"
        )
    try:
        schema: dict[str, Any] = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise IntrospectError(f"introspect produced invalid JSON: {proc.stdout[:200]!r}") from exc
    return IntrospectResult(app_import=app_import, openapi=schema)
