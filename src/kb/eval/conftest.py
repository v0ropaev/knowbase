"""Pytest DB harness (DESIGN.md §9 step 2).

``db_url`` is a session-scoped ephemeral local Postgres (no Docker) unless ``KB_TEST_DB_URL`` is
set. ``engine`` applies the Alembic migration once. ``conn`` gives each test a connection wrapped
in a rolled-back transaction for isolation.

Note: the ``artifact_grounded_check`` trigger is DEFERRABLE INITIALLY DEFERRED, so it fires at
COMMIT — a rolled-back ``conn`` never triggers it. Tests that need to exercise the grounding
rejection must commit (or ``SET CONSTRAINTS ... IMMEDIATE``); see adversarial_test.py.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Connection, Engine, create_engine

from kb.eval._pg import ephemeral_postgres
from kb.store.migrate import upgrade_to_head


@pytest.fixture(scope="session")
def db_url() -> Iterator[str]:
    override = os.environ.get("KB_TEST_DB_URL")
    if override:
        yield override
    else:
        with ephemeral_postgres() as url:
            yield url


@pytest.fixture(scope="session")
def engine(db_url: str) -> Iterator[Engine]:
    upgrade_to_head(db_url)
    eng = create_engine(db_url, future=True)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def conn(engine: Engine) -> Iterator[Connection]:
    """Function-scoped connection; the surrounding transaction is rolled back for isolation."""
    connection = engine.connect()
    transaction = connection.begin()
    try:
        yield connection
    finally:
        transaction.rollback()
        connection.close()


@pytest.fixture(scope="session")
def st_provider():
    """One local embedding model load per session (sentence-transformers; torch loads lazily)."""
    from kb.embed.providers import SentenceTransformerProvider

    return SentenceTransformerProvider()
