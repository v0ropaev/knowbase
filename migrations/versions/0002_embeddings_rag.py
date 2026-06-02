"""pgvector embeddings + RAG baseline — applies db/0002_embeddings_rag.sql.

Revision ID: 0002_embeddings_rag
Revises: 0001_initial
Create Date: 2026-06-02

Same mechanism as 0001: split the hand-written SQL (sqlparse handles the dollar-quote-free DDL here)
and run each statement on the raw psycopg cursor.
"""

from __future__ import annotations

from pathlib import Path

import sqlparse
from alembic import op

revision = "0002_embeddings_rag"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

# migrations/versions/0002_embeddings_rag.py -> repo root is parents[2]
_SQL_PATH = Path(__file__).resolve().parents[2] / "db" / "0002_embeddings_rag.sql"


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
    op.execute("DROP TABLE IF EXISTS rag_chunk CASCADE")
    op.execute("DROP INDEX IF EXISTS artifact_embedding_hnsw")
    op.execute("ALTER TABLE artifact DROP COLUMN IF EXISTS embedding")
    op.execute("ALTER TABLE artifact DROP COLUMN IF EXISTS embedding_model_id")
