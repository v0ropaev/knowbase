"""HARD GATE: reproducibility & invariance of the LOCKED span identity (DESIGN.md §5, §9).

These tests pin the behaviour that is expensive to change after the serializer ships:
formatting/comment/docstring/location changes must NOT change ``span_id``; renames MUST.
No database is involved — this is the pure identity core.
"""

from __future__ import annotations

from kb.structural.treesitter_index import TreeSitterIndex

INDEX = TreeSitterIndex()


def _ids_by_fq(module: str, source: str) -> dict[tuple[str, str], bytes]:
    """Map (span_kind, fq_symbol_path) -> span_id for a parsed source string."""
    result = INDEX.parse_file(module, source.encode("utf-8"))
    return {(s.span_kind, s.fq_symbol_path): s.span_id for s in result.spans}


BASE = '''\
import os
from a.b import c


class Order:
    """An order."""

    def total(self, items):
        # sum the items
        return sum(items)


def make(x):
    return Order()
'''


def test_parses_expected_symbols() -> None:
    ids = _ids_by_fq("shop.orders", BASE)
    kinds = {(k, fq) for (k, fq) in ids}
    assert ("module", "shop.orders") in kinds
    assert ("class", "shop.orders.Order") in kinds
    assert ("method", "shop.orders.Order.total") in kinds
    assert ("function", "shop.orders.make") in kinds
    # two import statements -> two import spans with distinct ordinals
    assert ("import", "shop.orders::import@0") in kinds
    assert ("import", "shop.orders::import@1") in kinds


def test_reproducible() -> None:
    assert _ids_by_fq("shop.orders", BASE) == _ids_by_fq("shop.orders", BASE)


def test_whitespace_and_blank_lines_invariant() -> None:
    reformatted = BASE.replace("return sum(items)", "return sum( items )").replace(
        "\n\n\n", "\n\n\n\n\n"
    )
    before = _ids_by_fq("shop.orders", BASE)
    after = _ids_by_fq("shop.orders", reformatted)
    key = ("method", "shop.orders.Order.total")
    assert before[key] == after[key]


def test_comment_change_invariant() -> None:
    changed = BASE.replace("# sum the items", "# completely different comment text")
    before = _ids_by_fq("shop.orders", BASE)
    after = _ids_by_fq("shop.orders", changed)
    key = ("method", "shop.orders.Order.total")
    assert before[key] == after[key]


def test_docstring_change_invariant() -> None:
    changed = BASE.replace('"""An order."""', '"""A purchase order, rewritten docs."""')
    before = _ids_by_fq("shop.orders", BASE)
    after = _ids_by_fq("shop.orders", changed)
    # the class' own docstring change must not alter the class span id ...
    assert before[("class", "shop.orders.Order")] == after[("class", "shop.orders.Order")]
    # ... nor the module span (a nested docstring is dropped from the module fingerprint too)
    assert before[("module", "shop.orders")] == after[("module", "shop.orders")]


def test_rename_changes_only_that_symbol() -> None:
    renamed = BASE.replace("def total(self, items):", "def grand_total(self, items):")
    before = _ids_by_fq("shop.orders", BASE)
    after = _ids_by_fq("shop.orders", renamed)
    assert ("method", "shop.orders.Order.total") in before
    assert ("method", "shop.orders.Order.total") not in after
    assert ("method", "shop.orders.Order.grand_total") in after
    # a sibling untouched symbol keeps its id
    assert before[("function", "shop.orders.make")] == after[("function", "shop.orders.make")]


def test_location_shift_invariant() -> None:
    """Inserting a new symbol above shifts byte/line offsets but identity must not change."""
    shifted = "def prelude():\n    return 0\n\n\n" + BASE
    before = _ids_by_fq("shop.orders", BASE)
    after = _ids_by_fq("shop.orders", shifted)
    for key in (
        ("class", "shop.orders.Order"),
        ("method", "shop.orders.Order.total"),
        ("function", "shop.orders.make"),
    ):
        assert before[key] == after[key], key


def test_body_change_changes_id() -> None:
    changed = BASE.replace("return sum(items)", "return sum(items) + 1")
    before = _ids_by_fq("shop.orders", BASE)
    after = _ids_by_fq("shop.orders", changed)
    key = ("method", "shop.orders.Order.total")
    assert before[key] != after[key]


def test_module_in_identity() -> None:
    """The same code under a different module name yields a different identity (fq path differs)."""
    a = _ids_by_fq("shop.orders", BASE)
    b = _ids_by_fq("shop.billing", BASE)
    assert a[("function", "shop.orders.make")] != b[("function", "shop.billing.make")]


def test_duplicate_imports_distinct_but_same_fingerprint() -> None:
    src = "import os\nimport os\n"
    spans = INDEX.parse_file("m", src.encode()).spans
    imports = sorted((s for s in spans if s.span_kind == "import"), key=lambda s: s.fq_symbol_path)
    assert len(imports) == 2
    assert imports[0].span_id != imports[1].span_id  # distinct ordinals
    assert imports[0].structural_fingerprint == imports[1].structural_fingerprint  # identical code


def test_unparseable_file_flagged_not_dropped() -> None:
    result = INDEX.parse_file("broken", b"def f(:\n    pass\n")
    assert result.has_error is True
    # still produces at least the module span (a recorded gap, not a silent loss)
    assert any(s.span_kind == "module" for s in result.spans)
