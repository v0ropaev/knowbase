"""HARD GATE — Tier 1: domain entities vs a hand-labeled oracle (DESIGN.md §4, §9).

The hand-labeled ``EXPECTED_ENTITIES`` / ``EXPECTED_FIELDS`` are the real oracle (importing the
models to introspect them would execute user code). A bare declarative ``Base`` must NOT be an
entity, and a dynamically-built model (``create_model``) is a deliberate static-analysis blind spot,
asserted as a KNOWN gap — not a silent loss. Every entity is grounded on its class-definition span.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, select

from kb.daemon.pipeline import index_commit
from kb.eval._fixtures import make_git_repo
from kb.extract.deterministic.entities import EntityExtractor
from kb.store import models as m
from kb.store.queries import provenance_for_artifact

# A src-layout module: a pydantic model, a dataclass, a SQLAlchemy model (plus a bare declarative
# Base that is NOT an entity), and a dynamically-built model (invisible to static parsing).
FILES = {
    "src/shop/__init__.py": "",
    "src/shop/models.py": (
        "from dataclasses import dataclass\n"
        "from pydantic import BaseModel, create_model\n"
        "from sqlalchemy import Column, Integer\n"
        "from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column\n"
        "\n\n"
        "class Order(BaseModel):\n"
        "    id: int\n"
        "    total: float = 0.0\n"
        "    note: str | None = None\n"
        "\n\n"
        "@dataclass\n"
        "class LineItem:\n"
        "    sku: str\n"
        "    qty: int = 1\n"
        "\n\n"
        "class Base(DeclarativeBase):\n"
        "    pass\n"
        "\n\n"
        "class User(Base):\n"
        '    __tablename__ = "users"\n'
        "    id: Mapped[int] = mapped_column(primary_key=True)\n"
        "    name: Mapped[str] = mapped_column()\n"
        "    legacy = Column(Integer)\n"
        "\n\n"
        'Dynamic = create_model("Dynamic", x=(int, ...))\n'
    ),
    # A second module whose entity references one in shop/models.py (the cross-file link).
    "src/shop/cart.py": (
        "from dataclasses import dataclass\n"
        "from shop.models import Order\n"
        "\n\n"
        "@dataclass\n"
        "class Cart:\n"
        "    orders: list[Order]\n"
    ),
}

# Hand-labeled oracle: (framework, fq class). `Base` and `Dynamic` are deliberately absent.
EXPECTED_ENTITIES = {
    ("pydantic", "shop.models.Order"),
    ("dataclass", "shop.models.LineItem"),
    ("sqlalchemy", "shop.models.User"),
    ("dataclass", "shop.cart.Cart"),
}
EXPECTED_FIELDS = {
    "shop.models.Order": {"id", "total", "note"},
    "shop.models.LineItem": {"sku", "qty"},
    "shop.models.User": {"id", "name", "legacy"},  # __tablename__ is metadata, not a field
    "shop.cart.Cart": {"orders"},
}
KNOWN_GAP = "shop.models.Dynamic"  # create_model(): dynamic, invisible to static analysis


def _index(engine: Engine, tmp_path: Path) -> str:
    sha = make_git_repo(tmp_path, [FILES])[0]
    index_commit(engine, str(tmp_path), sha, extractors=[EntityExtractor()], first_party_root="src")
    return sha


def _entity_payloads(engine: Engine, sha: str) -> list[dict]:
    join = m.snapshot_entry.join(
        m.artifact, m.artifact.c.artifact_id == m.snapshot_entry.c.artifact_id
    )
    with engine.connect() as conn:
        return list(
            conn.execute(
                select(m.artifact.c.payload)
                .select_from(join)
                .where(m.snapshot_entry.c.sha == sha, m.artifact.c.kind == "entity")
            ).scalars()
        )


def test_entities_match_oracle(engine: Engine, tmp_path: Path) -> None:
    sha = _index(engine, tmp_path)
    found = {(p["framework"], p["qualified_name"]) for p in _entity_payloads(engine, sha)}
    assert found == EXPECTED_ENTITIES


def test_fields_match_oracle(engine: Engine, tmp_path: Path) -> None:
    sha = _index(engine, tmp_path)
    by_key = {p["qualified_name"]: p for p in _entity_payloads(engine, sha)}
    for qualified_name, expected in EXPECTED_FIELDS.items():
        names = {f["name"] for f in by_key[qualified_name]["fields"]}
        assert names == expected, qualified_name


def test_bare_declarative_base_is_not_an_entity(engine: Engine, tmp_path: Path) -> None:
    sha = _index(engine, tmp_path)
    keys = {p["qualified_name"] for p in _entity_payloads(engine, sha)}
    assert "shop.models.Base" not in keys  # no __tablename__, no columns -> not a domain entity


def test_dynamic_model_is_a_known_gap(engine: Engine, tmp_path: Path) -> None:
    sha = _index(engine, tmp_path)
    keys = {p["qualified_name"] for p in _entity_payloads(engine, sha)}
    assert KNOWN_GAP not in keys  # documented blind spot, surfaced — not silently "found"


def test_entities_grounded_on_class_spans(engine: Engine, tmp_path: Path) -> None:
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
            select(m.artifact.c.payload, m.code_span.c.span_kind)
            .select_from(join)
            .where(m.snapshot_entry.c.sha == sha, m.artifact.c.kind == "entity")
        ).all()
    assert rows  # every entity is grounded (>=1 derived_from)
    for row in rows:
        assert row.span_kind == "class"
        assert row.payload["span_mapping"] == "exact"


def test_cross_file_entity_links_grounded(engine: Engine, tmp_path: Path) -> None:
    """`Cart` (cart.py) references `Order` (models.py) -> the artifact spans BOTH files."""
    sha = _index(engine, tmp_path)
    with engine.connect() as conn:
        prov = provenance_for_artifact(conn, sha, "entity:shop.cart.Cart")
    by_role = {(p.file_path, p.role) for p in prov}
    assert ("src/shop/cart.py", "class_definition") in by_role
    assert ("src/shop/models.py", "related_entity") in by_role  # cross-file grounding

    cart = next(p for p in _entity_payloads(engine, sha) if p["qualified_name"] == "shop.cart.Cart")
    related = {(r["name"], r["target_fq"], r["via"]) for r in cart["related_entities"]}
    assert ("Order", "shop.models.Order", "field_type") in related


# --- identity v2 regression: mutually referencing entities (same evidence span set) ------------

# `Order` and `Item` reference EACH OTHER across files, so both entity artifacts are grounded on
# the identical pair of class spans. Under identity rule v1 (no logical_key in the hash) they
# collided into ONE artifact_id and the second payload was silently lost; rule v2 must keep them
# distinct while preserving the cross-file grounding of both.
MUTUAL_FILES = {
    "src/mrefs/__init__.py": "",
    "src/mrefs/a.py": (
        "from pydantic import BaseModel\n\n\n"
        "class MOrder(BaseModel):\n"
        "    items: list['MItem']\n"
    ),
    "src/mrefs/b.py": (
        "from pydantic import BaseModel\n"
        "from mrefs.a import MOrder\n\n\n"
        "class MItem(BaseModel):\n"
        "    order: 'MOrder'\n"
    ),
}


def test_mutual_refs_yield_distinct_artifacts(engine: Engine, tmp_path: Path) -> None:
    sha = make_git_repo(tmp_path, [MUTUAL_FILES])[0]
    index_commit(engine, str(tmp_path), sha, extractors=[EntityExtractor()], first_party_root="src")

    join = m.snapshot_entry.join(
        m.artifact, m.artifact.c.artifact_id == m.snapshot_entry.c.artifact_id
    )
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                m.snapshot_entry.c.logical_key,
                m.snapshot_entry.c.artifact_id,
                m.artifact.c.payload,
            )
            .select_from(join)
            .where(m.snapshot_entry.c.sha == sha, m.artifact.c.kind == "entity")
        ).all()

    by_key = {r.logical_key: r for r in rows}
    assert set(by_key) == {"entity:mrefs.a.MOrder", "entity:mrefs.b.MItem"}  # both survive
    ids = {bytes(r.artifact_id) for r in rows}
    assert len(ids) == 2  # DISTINCT artifact ids despite the identical evidence span set
    # each logical key serves ITS OWN payload (the v1 bug served one payload for both keys)
    assert by_key["entity:mrefs.a.MOrder"].payload["class_name"] == "MOrder"
    assert by_key["entity:mrefs.b.MItem"].payload["class_name"] == "MItem"

    # the cross-file grounding is preserved on both sides
    with engine.connect() as conn:
        for key in by_key:
            files = {p.file_path for p in provenance_for_artifact(conn, sha, key)}
            assert files == {"src/mrefs/a.py", "src/mrefs/b.py"}
