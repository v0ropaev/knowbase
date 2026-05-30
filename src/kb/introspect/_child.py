"""Sandboxed child: emit a FastAPI app's ``openapi()`` as JSON on stdout.

Run as ``python -m kb.introspect._child <module:appvar>``. A *soft* jail is applied BEFORE importing
user code — network calls are blocked and resource limits are set — then the app is imported and its
OpenAPI document printed to stdout (diagnostics to stderr). This is not a kernel sandbox; it matches
the eval-only, opt-in posture in DESIGN.md §12.
"""

from __future__ import annotations

import importlib
import json
import sys
from typing import Any, NoReturn

_ADDRESS_SPACE_BYTES = 1024**3  # ~1 GiB
_MAX_FILE_BYTES = 50 * 1024 * 1024  # bound runaway writes without SIGXFSZ on tiny writes


def _harden() -> None:
    import socket

    def _blocked(*args: object, **kwargs: object) -> NoReturn:
        raise OSError("network disabled in kb introspect sandbox")

    # Block outbound connections WITHOUT replacing the socket class (ssl subclasses socket.socket,
    # so replacing the class breaks `import ssl` and therefore fastapi).
    for attr in ("connect", "connect_ex"):
        setattr(socket.socket, attr, _blocked)
    socket.create_connection = _blocked
    sys.dont_write_bytecode = True  # so RLIMIT_FSIZE never trips on a .pyc write

    try:
        import resource
    except ImportError:  # non-POSIX
        return
    _set_limit(resource, "RLIMIT_CPU", (10, 12))
    _set_limit(resource, "RLIMIT_FSIZE", (_MAX_FILE_BYTES, _MAX_FILE_BYTES))
    _set_limit(resource, "RLIMIT_AS", (_ADDRESS_SPACE_BYTES, _ADDRESS_SPACE_BYTES))


def _set_limit(resource: Any, name: str, limits: tuple[int, int]) -> None:
    const = getattr(resource, name, None)
    if const is None:
        return
    try:
        resource.setrlimit(const, limits)
    except (ValueError, OSError):  # some platforms disallow lowering / RLIMIT_AS
        pass


def main(argv: list[str]) -> int:
    if len(argv) < 2 or ":" not in argv[1]:
        print("usage: python -m kb.introspect._child <module:appvar>", file=sys.stderr)
        return 2
    _harden()
    module_name, _, app_attr = argv[1].partition(":")
    module = importlib.import_module(module_name)
    app: Any = getattr(module, app_attr)
    schema: dict[str, Any] = app.openapi()
    json.dump(schema, sys.stdout)
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
