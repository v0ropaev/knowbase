"""Initial schema — applies the canonical db/0001_initial.sql verbatim.

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-30

The schema is maintained as hand-written SQL (db/0001_initial.sql) because it contains a plpgsql
function + a DEFERRABLE CONSTRAINT TRIGGER that Alembic's table ops cannot express. We split the
file into statements (sqlparse handles the dollar-quoted body) and run each on the raw psycopg
cursor with no params, so psycopg does not treat the '%' in the RAISE message as a placeholder.
"""

from __future__ import annotations

from pathlib import Path

import sqlparse
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

# migrations/versions/0001_initial.py -> repo root is parents[2]
_SQL_PATH = Path(__file__).resolve().parents[2] / "db" / "0001_initial.sql"

_DROP_ORDER = (
    "snapshot_entry",
    "artifact_derived_from",
    "artifact",
    "span_occurrence",
    "code_span",
    "branch_ref",
    "commit_ref",
)


def _statements(raw: str) -> list[str]:
    out = []
    for chunk in sqlparse.split(raw):
        stmt = chunk.strip()
        if stmt and stmt.rstrip(";").upper() not in ("BEGIN", "COMMIT"):
            out.append(stmt)
    return out


def upgrade() -> None:
    raw = _SQL_PATH.read_text()
    cursor = op.get_bind().connection.cursor()
    try:
        for stmt in _statements(raw):
            cursor.execute(stmt)
    finally:
        cursor.close()


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS {', '.join(_DROP_ORDER)} CASCADE")
    op.execute("DROP FUNCTION IF EXISTS assert_artifact_is_grounded() CASCADE")
