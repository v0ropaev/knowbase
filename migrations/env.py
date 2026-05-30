"""Alembic environment (online only). The DB URL comes from the alembic config (set by
``kb.store.migrate``) or the ``KB_DB_URL`` / ``KB_DB`` environment variables."""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine, pool

config = context.config


def _db_url() -> str:
    return (
        config.get_main_option("sqlalchemy.url")
        or os.environ.get("KB_DB_URL")
        or os.environ.get("KB_DB")
        or "postgresql+psycopg://postgres@127.0.0.1:5432/postgres"
    )


def run_migrations_online() -> None:
    engine = create_engine(_db_url(), poolclass=pool.NullPool, future=True)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=None)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("knowbase migrations require a live database (online mode only)")
run_migrations_online()
