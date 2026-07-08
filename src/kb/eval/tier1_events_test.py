"""HARD GATE — Tier 1: event handlers vs a hand-labeled oracle (DESIGN.md §9, §14).

The extractor is static, so the oracle is hand-labeled (a runtime oracle would execute user code):
``EXPECTED_EVENTS`` enumerates every registration in the fixture — decorator-form AND module-level
call-form ``event.listen(...)`` (one ``event_handler`` artifact per handler; stacked decorators
and call sites appear as multiple entries in its ``payload.registrations``). The thesis
assertions: a SQLAlchemy listener is grounded CROSS-FILE on the class it listens to; a call-form
registration whose listen() lives in a THIRD module grounds ONE artifact across three files
(handler + target class + registration site); pydantic validators are grounded on their owner
model class; and the documented blind spots — ``listen(...)`` inside a function body, a lambda
listener, and a dynamic ``@app.on_event(EVENT)`` name — are asserted as *known* gaps, never a
silent loss.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, func, select

from kb.daemon.pipeline import index_commit
from kb.eval._fixtures import make_git_repo
from kb.extract.deterministic.events import EventExtractor
from kb.store import models as m
from kb.store.queries import provenance_for_artifact

FILES = {
    "src/shop/__init__.py": "",
    # pydantic: field + model validators, plus a dynamic-field-name validator (kept + flagged)
    "src/shop/models.py": (
        "from pydantic import BaseModel, field_validator, model_validator\n\n"
        "FIELD = 'pct'\n\n\n"
        "class Order(BaseModel):\n"
        "    id: int\n"
        "    total: float\n\n"
        "    @field_validator('total')\n"
        "    @classmethod\n"
        "    def check_total(cls, v):\n        return v\n\n"
        "    @model_validator(mode='after')\n"
        "    def finalize(self):\n        return self\n\n\n"
        "class Discount(BaseModel):\n"
        "    pct: float\n\n"
        "    @field_validator(FIELD)\n"
        "    @classmethod\n"
        "    def check(cls, v):\n        return v\n"
    ),
    # the SQLAlchemy model lives HERE ...
    "src/shop/db.py": (
        "from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column\n\n\n"
        "class Base(DeclarativeBase):\n    pass\n\n\n"
        "class OrderRow(Base):\n"
        "    __tablename__ = 'orders'\n"
        "    id: Mapped[int] = mapped_column(primary_key=True)\n"
    ),
    # ... and its listeners in a DIFFERENT file (cross-file grounding), incl. stacked decorators,
    # a non-first-party target, and a same-module call-form registration.
    "src/shop/hooks.py": (
        "from sqlalchemy import event\n"
        "from sqlalchemy.orm import Session\n"
        "from shop.db import OrderRow\n\n\n"
        "@event.listens_for(OrderRow, 'after_insert')\n"
        "def audit_insert(mapper, connection, target):\n    pass\n\n\n"
        "@event.listens_for(OrderRow, 'after_insert')\n"
        "@event.listens_for(OrderRow, 'after_update')\n"
        "def audit_multi(mapper, connection, target):\n    pass\n\n\n"
        "@event.listens_for(Session, 'before_flush')\n"
        "def on_flush(session, ctx, instances):\n    pass\n\n\n"
        "def audit_update(mapper, connection, target):\n    pass\n\n\n"
        "def audit_delete(mapper, connection, target):\n    pass\n\n\n"
        "event.listen(OrderRow, 'before_update', audit_update)\n"
    ),
    # a THIRD module wires an imported handler (three-file provenance), plus the two call-form
    # KNOWN GAPS: listen() inside a function body and a lambda listener
    "src/shop/wiring.py": (
        "from sqlalchemy import event\n"
        "from shop.db import OrderRow\n"
        "from shop.hooks import audit_delete\n\n\n"
        "def setup():\n"
        "    event.listen(OrderRow, 'after_insert', audit_delete)\n\n\n"
        "event.listen(OrderRow, 'after_delete', audit_delete)\n"
        "event.listen(OrderRow, 'before_insert', lambda m, c, t: None)\n"
    ),
    # fastapi lifecycle + the dynamic-event-name KNOWN GAP
    "src/shop/main.py": (
        "from fastapi import FastAPI\n\n"
        "app = FastAPI()\n"
        "EVENT = 'shutdown'\n\n\n"
        "@app.on_event('startup')\n"
        "def on_startup():\n    pass\n\n\n"
        "@app.on_event(EVENT)\n"
        "def on_stop():\n    pass\n"
    ),
}

# (family, event, handler_fq, anchor) — anchor = target | owner_class | decorator_object per family
EXPECTED_EVENTS = {
    ("pydantic_field_validator", None, "shop.models.Order.check_total", "shop.models.Order"),
    ("pydantic_model_validator", None, "shop.models.Order.finalize", "shop.models.Order"),
    ("pydantic_field_validator", None, "shop.models.Discount.check", "shop.models.Discount"),
    ("fastapi_on_event", "startup", "shop.main.on_startup", "app"),
    ("sqlalchemy_listens_for", "after_insert", "shop.hooks.audit_insert", "OrderRow"),
    ("sqlalchemy_listens_for", "after_insert", "shop.hooks.audit_multi", "OrderRow"),
    ("sqlalchemy_listens_for", "after_update", "shop.hooks.audit_multi", "OrderRow"),
    ("sqlalchemy_listens_for", "before_flush", "shop.hooks.on_flush", "Session"),
    ("sqlalchemy_listen", "before_update", "shop.hooks.audit_update", "OrderRow"),
    ("sqlalchemy_listen", "after_delete", "shop.hooks.audit_delete", "OrderRow"),
}
KNOWN_GAP_FUNCTION_BODY_EVENT = "after_insert"  # listen() inside setup() — conditional, skipped
KNOWN_GAP_LAMBDA_EVENT = "before_insert"  # lambda listener — no handler span, skipped
KNOWN_GAP_DYNAMIC_EVENT = "shop.main.on_stop"  # @app.on_event(EVENT) — dynamic name, skipped


def _index(engine: Engine, tmp_path: Path) -> str:
    sha = make_git_repo(tmp_path, [FILES])[0]
    index_commit(engine, str(tmp_path), sha, extractors=[EventExtractor()], first_party_root="src")
    return sha


def _payloads(engine: Engine, sha: str) -> list[dict]:
    join = m.snapshot_entry.join(
        m.artifact, m.artifact.c.artifact_id == m.snapshot_entry.c.artifact_id
    )
    with engine.connect() as conn:
        return list(
            conn.execute(
                select(m.artifact.c.payload)
                .select_from(join)
                .where(m.snapshot_entry.c.sha == sha, m.artifact.c.kind == "event_handler")
            ).scalars()
        )


def test_events_match_oracle(engine: Engine, tmp_path: Path) -> None:
    sha = _index(engine, tmp_path)
    extracted = {
        (
            reg["family"],
            reg["event"],
            p["handler"],
            reg["target"] or p["owner_class"] or reg["decorator_object"],
        )
        for p in _payloads(engine, sha)
        for reg in p["registrations"]
    }
    assert extracted == EXPECTED_EVENTS  # incl. BOTH stacked audit_multi registrations


def test_fields_mode_and_flags_captured(engine: Engine, tmp_path: Path) -> None:
    sha = _index(engine, tmp_path)
    by_handler = {p["handler"]: p for p in _payloads(engine, sha)}
    assert by_handler["shop.models.Order.check_total"]["registrations"][0]["fields"] == ["total"]
    assert by_handler["shop.models.Order.finalize"]["registrations"][0]["mode"] == "after"
    dynamic = by_handler["shop.models.Discount.check"]
    assert dynamic["registrations"][0]["fields"] == []  # a variable field name is not guessed ...
    assert "dynamic_field_names" in dynamic["limitations"]  # ... but honestly flagged


def test_call_form_listen_extracted(engine: Engine, tmp_path: Path) -> None:
    """The v1 call-form KNOWN GAP is now extracted: same-module event.listen(...) registration."""
    sha = _index(engine, tmp_path)
    update = next(p for p in _payloads(engine, sha) if p["handler"] == "shop.hooks.audit_update")
    assert update["families"] == ["sqlalchemy_listen"]
    reg = update["registrations"][0]
    assert reg["form"] == "call"
    assert reg["target_resolved"] is True
    assert reg["registration_module"] == "shop.hooks"
    assert update["detection_signals"] == ["sqlalchemy_listen_call"]
    with engine.connect() as conn:
        prov = {
            (p.file_path, p.role)
            for p in provenance_for_artifact(conn, sha, "event:shop.hooks.audit_update")
        }
    assert prov == {
        ("src/shop/hooks.py", "handler"),
        ("src/shop/db.py", "target_class"),
        ("src/shop/hooks.py", "registration_site"),  # module span != function span, same file
    }


def test_cross_module_listen_grounded_three_files(engine: Engine, tmp_path: Path) -> None:
    """listen() in wiring.py registers a handler from hooks.py on a class from db.py — ONE
    artifact grounded across THREE files."""
    sha = _index(engine, tmp_path)
    delete = next(p for p in _payloads(engine, sha) if p["handler"] == "shop.hooks.audit_delete")
    assert delete["handler_module"] == "shop.hooks"  # the handler's module, not the wiring's
    assert delete["registrations"][0]["registration_module"] == "shop.wiring"
    with engine.connect() as conn:
        prov = {
            (p.file_path, p.role)
            for p in provenance_for_artifact(conn, sha, "event:shop.hooks.audit_delete")
        }
    assert prov == {
        ("src/shop/hooks.py", "handler"),
        ("src/shop/db.py", "target_class"),
        ("src/shop/wiring.py", "registration_site"),
    }


def test_listen_inside_function_body_is_a_known_gap(engine: Engine, tmp_path: Path) -> None:
    """The listen() inside setup() is conditional (runs only if setup() runs) — never extracted."""
    sha = _index(engine, tmp_path)
    delete = next(p for p in _payloads(engine, sha) if p["handler"] == "shop.hooks.audit_delete")
    events = [r["event"] for r in delete["registrations"]]
    assert events == ["after_delete"]  # only the top-level registration ...
    assert KNOWN_GAP_FUNCTION_BODY_EVENT not in events  # ... never the one inside setup()


def test_lambda_listener_is_a_known_gap(engine: Engine, tmp_path: Path) -> None:
    """A lambda listener has no handler span to ground — skipped, never a wrong guess."""
    sha = _index(engine, tmp_path)
    all_events = {
        reg["event"] for p in _payloads(engine, sha) for reg in p["registrations"]
    }
    assert KNOWN_GAP_LAMBDA_EVENT not in all_events


def test_dynamic_event_name_is_a_known_gap(engine: Engine, tmp_path: Path) -> None:
    sha = _index(engine, tmp_path)
    assert all(p["handler"] != KNOWN_GAP_DYNAMIC_EVENT for p in _payloads(engine, sha))


def test_every_event_handler_is_grounded(engine: Engine, tmp_path: Path) -> None:
    sha = _index(engine, tmp_path)
    join = m.snapshot_entry.outerjoin(
        m.artifact_derived_from,
        m.artifact_derived_from.c.artifact_id == m.snapshot_entry.c.artifact_id,
    ).join(m.artifact, m.artifact.c.artifact_id == m.snapshot_entry.c.artifact_id)
    with engine.connect() as conn:
        ungrounded = conn.execute(
            select(func.count())
            .select_from(join)
            .where(
                m.snapshot_entry.c.sha == sha,
                m.artifact.c.kind == "event_handler",
                m.artifact_derived_from.c.artifact_id.is_(None),
            )
        ).scalar()
    assert ungrounded == 0


def test_handler_role_on_function_or_method_span(engine: Engine, tmp_path: Path) -> None:
    sha = _index(engine, tmp_path)
    join = (
        m.snapshot_entry.join(
            m.artifact, m.artifact.c.artifact_id == m.snapshot_entry.c.artifact_id
        )
        .join(
            m.artifact_derived_from,
            m.artifact_derived_from.c.artifact_id == m.artifact.c.artifact_id,
        )
        .join(m.code_span, m.code_span.c.span_id == m.artifact_derived_from.c.span_id)
    )
    with engine.connect() as conn:
        rows = conn.execute(
            select(m.artifact.c.payload, m.code_span.c.fq_symbol_path, m.code_span.c.span_kind)
            .select_from(join)
            .where(
                m.snapshot_entry.c.sha == sha,
                m.artifact.c.kind == "event_handler",
                m.artifact_derived_from.c.role == "handler",
            )
        ).all()
    assert rows
    for row in rows:
        assert row.span_kind in ("function", "method")
        assert row.fq_symbol_path == row.payload["handler"]
        assert row.payload["span_mapping"] == "exact"


def test_sqlalchemy_target_grounded_cross_file(engine: Engine, tmp_path: Path) -> None:
    sha = _index(engine, tmp_path)
    with engine.connect() as conn:
        prov = {
            (p.file_path, p.role)
            for p in provenance_for_artifact(conn, sha, "event:shop.hooks.audit_insert")
        }
    assert ("src/shop/hooks.py", "handler") in prov  # the listener ...
    assert ("src/shop/db.py", "target_class") in prov  # ... spans the class file it listens to


def test_non_first_party_target_flagged(engine: Engine, tmp_path: Path) -> None:
    sha = _index(engine, tmp_path)
    flush = next(p for p in _payloads(engine, sha) if p["handler"] == "shop.hooks.on_flush")
    assert flush["registrations"][0]["target_resolved"] is False
    assert "target_not_first_party" in flush["limitations"]
    with engine.connect() as conn:
        roles = {
            p.role for p in provenance_for_artifact(conn, sha, "event:shop.hooks.on_flush")
        }
    assert roles == {"handler"}  # grounded, but only on the handler span


def test_pydantic_owner_class_grounded(engine: Engine, tmp_path: Path) -> None:
    sha = _index(engine, tmp_path)
    with engine.connect() as conn:
        prov = {
            (p.file_path, p.role)
            for p in provenance_for_artifact(conn, sha, "event:shop.models.Order.check_total")
        }
    assert ("src/shop/models.py", "handler") in prov
    assert ("src/shop/models.py", "owner_class") in prov
