"""Eval-only runtime oracle: run a user's FastAPI app in a sandbox and emit its openapi().

Used to produce Tier-1 API ground truth. NOTHING on the ``kb index`` path imports this package —
the serving extractor is fully static (DESIGN.md §8, §12).
"""

from kb.introspect.runner import IntrospectError, IntrospectResult, introspect_app

__all__ = ["IntrospectError", "IntrospectResult", "introspect_app"]
