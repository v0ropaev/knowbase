"""Programmatic Alembic helpers (used by the test harness and the CLI)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

# src/kb/store/migrate.py -> repo root is parents[3]
_REPO_ROOT = Path(__file__).resolve().parents[3]
_MIGRATIONS = _REPO_ROOT / "migrations"


def alembic_config(db_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def upgrade_to_head(db_url: str) -> None:
    """Apply all migrations up to head against ``db_url``."""
    command.upgrade(alembic_config(db_url), "head")
