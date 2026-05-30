"""SQLAlchemy engine factory over psycopg 3 (DESIGN.md §11). Core only — no ORM."""

from __future__ import annotations

import os

from sqlalchemy import Engine, create_engine

DEFAULT_DB_URL = "postgresql+psycopg://postgres@127.0.0.1:5432/postgres"


def resolve_db_url(explicit: str | None = None) -> str:
    """Resolve the database URL: explicit arg > ``KB_DB_URL`` env > ``KB_DB`` env > default."""
    return explicit or os.environ.get("KB_DB_URL") or os.environ.get("KB_DB") or DEFAULT_DB_URL


def make_engine(db_url: str | None = None) -> Engine:
    """A SQLAlchemy ``Engine`` bound to the resolved URL (driver: ``postgresql+psycopg``)."""
    return create_engine(resolve_db_url(db_url), future=True)
