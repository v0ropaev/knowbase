"""Throwaway local PostgreSQL for tests — no Docker required.

Spins an ephemeral cluster with ``initdb``/``pg_ctl`` (Postgres.app, Homebrew, or a system
package), on a free port with trust auth and a private socket dir, then tears it down. Override
the binaries with ``KB_PG_BINDIR``; or skip the cluster entirely by pointing ``KB_TEST_DB_URL`` at
an existing (empty) database. In CI, a real PG service plus ``KB_TEST_DB_URL`` is the natural path.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import socket
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

_CANDIDATE_BINDIRS = (
    "/Applications/Postgres.app/Contents/Versions/latest/bin",
    "/opt/homebrew/opt/postgresql@17/bin",
    "/opt/homebrew/opt/postgresql@16/bin",
    "/usr/local/opt/postgresql@16/bin",
    "/usr/lib/postgresql/17/bin",
    "/usr/lib/postgresql/16/bin",
)


def find_bindir() -> Path:
    env = os.environ.get("KB_PG_BINDIR")
    if env:
        return Path(env)
    on_path = shutil.which("pg_ctl")
    if on_path:
        return Path(on_path).parent
    for candidate in _CANDIDATE_BINDIRS:
        if (Path(candidate) / "pg_ctl").exists():
            return Path(candidate)
    raise RuntimeError(
        "no PostgreSQL binaries found; set KB_PG_BINDIR or KB_TEST_DB_URL, or install PostgreSQL"
    )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.contextmanager
def ephemeral_postgres(user: str = "kb", dbname: str = "postgres") -> Iterator[str]:
    """Yield a ``postgresql+psycopg://`` URL for a throwaway cluster; tear it down on exit."""
    bindir = find_bindir()
    tmp = Path(tempfile.mkdtemp(prefix="kb-pg-"))
    data = tmp / "data"
    port = _free_port()
    started = False
    try:
        subprocess.run(
            [str(bindir / "initdb"), "-D", str(data), "-U", user, "--auth=trust", "-E", "UTF8"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                str(bindir / "pg_ctl"),
                "-D",
                str(data),
                "-o",
                f"-p {port} -k {tmp} -c listen_addresses=127.0.0.1",
                "-l",
                str(tmp / "log"),
                "-w",
                "start",
            ],
            check=True,
            capture_output=True,
        )
        started = True
        yield f"postgresql+psycopg://{user}@127.0.0.1:{port}/{dbname}"
    finally:
        if started:
            subprocess.run(
                [str(bindir / "pg_ctl"), "-D", str(data), "-w", "-m", "immediate", "stop"],
                check=False,
                capture_output=True,
            )
        shutil.rmtree(tmp, ignore_errors=True)
