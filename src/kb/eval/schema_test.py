"""Verify the migration produced the full schema (DESIGN.md §9 step 2 verify)."""

from __future__ import annotations

from sqlalchemy import Connection, text

_EXPECTED_TABLES = {
    "commit_ref",
    "branch_ref",
    "code_span",
    "span_occurrence",
    "artifact",
    "artifact_derived_from",
    "snapshot_entry",
}


def test_all_tables_present(conn: Connection) -> None:
    rows = conn.execute(
        text(
            "select table_name from information_schema.tables where table_schema = 'public'"
        )
    ).scalars()
    assert _EXPECTED_TABLES.issubset(set(rows))


def test_grounding_trigger_and_function_present(conn: Connection) -> None:
    trig = conn.execute(
        text("select tgname from pg_trigger where tgname = 'artifact_grounded_check'")
    ).scalar()
    assert trig == "artifact_grounded_check"
    func = conn.execute(
        text("select proname from pg_proc where proname = 'assert_artifact_is_grounded'")
    ).scalar()
    assert func == "assert_artifact_is_grounded"


def test_artifact_check_constraints_present(conn: Connection) -> None:
    rows = conn.execute(
        text(
            "select conname from pg_constraint where conname like 'artifact_%'"
        )
    ).scalars()
    names = set(rows)
    assert "artifact_confidence_range" in names
    assert "artifact_deterministic_no_llm_keys" in names
