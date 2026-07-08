"""Deterministic call-graph edge extractor (DESIGN.md §8; the §14-item-2 foundation).

Produces one ``call_edge`` artifact per RESOLVED (caller -> callee) pair — ``call:{caller_fq}->
{callee_fq}`` — with the call-site line numbers aggregated in ``payload.lines`` (the import_edge
precedent). Three deterministic resolution tiers, precision-first (only edges resolved to a
first-party def span are emitted; never a wrong guess):

* ``same_module`` — a bare ``f(...)`` naming a top-level function/class of the same module;
* ``imported`` — CROSS-FILE: ``from x import f [as g]; g(...)`` and module-attribute calls after
  ``import x[.y] [as z]`` (``x.y.f(...)``, ``z.f(...)``), resolved via a per-module import table;
* ``self`` — ``self.method(...)`` inside a method, resolved to a method of the SAME class.

Grounded on the caller def span (role ``caller``; a module-level call grounds on the module span)
plus the callee def span (role ``callee``, cross-file possible). Direct recursion is a single-span
artifact (one edge row, role ``caller`` — the ``(artifact_id, span_id)`` PK permits one row per
span). Mutual recursion yields two distinct artifacts (identity rule v2 folds ``logical_key`` into
``artifact_id``). ``framework_versions`` is deliberately empty: a call edge does not depend on any
framework version (the import-graph rationale).

Fully static — re-parses span text with tree-sitter; never imports or executes user code. Documented
gaps (recall is bounded, surfaced by the eval gate — DESIGN.md §12): arbitrary ``obj.method(...)``;
dynamic calls (``getattr``, computed callables, the outer link of ``super().m()`` / ``a().b()``);
inherited ``self.method()``; star imports; re-export indirection; calls to non-first-party targets
(skipped — not first-party knowledge); decorator expressions, parameter-default expressions and
class-body statements (def-time, not call-time; decorator wiring belongs to the fastapi/events
extractors); the import table is file-scoped (no scope modeling); ``cls.method()`` /
``ClassName.method(...)`` receivers.
"""

from __future__ import annotations

import textwrap
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import tree_sitter_python as tsp
from tree_sitter import Language, Node, Parser

from kb.extract.base import DerivedEdge, ExtractContext, ExtractedArtifact
from kb.structural.interface import ParsedSpan

EXTRACTOR_ID = "calls"
EXTRACTOR_VERSION = "1"

_LANGUAGE = Language(tsp.language())
_IMPORT_TYPES = frozenset({"import_statement", "import_from_statement"})
_SCOPE_BARRIERS = frozenset({"function_definition", "class_definition", "decorated_definition"})


@dataclass(frozen=True)
class _ImportTable:
    symbols: dict[str, tuple[str, str]]  # local name -> (target_module, original_symbol)
    modules: dict[str, str]  # local dotted name -> target module fqname


@dataclass
class _Edge:
    caller: ParsedSpan
    caller_kind: str  # "module" | "function" | "method"
    caller_module: str
    callee: ParsedSpan
    callee_module: str
    resolution: str  # "same_module" | "imported" | "self"
    lines: set[int] = field(default_factory=set)


class CallGraphExtractor:
    extractor_id = EXTRACTOR_ID
    extractor_version = EXTRACTOR_VERSION

    def __init__(self) -> None:
        self._parser = Parser(_LANGUAGE)

    def extract(self, ctx: ExtractContext) -> list[ExtractedArtifact]:
        def_index: dict[tuple[str, str], ParsedSpan] = {}
        method_index: dict[tuple[str, str], ParsedSpan] = {}
        for module, spans in ctx.spans_by_module.items():
            for span in spans:
                parent, _, name = span.fq_symbol_path.rpartition(".")
                if span.span_kind in ("class", "function") and parent == module:
                    def_index[(module, name)] = span
                elif span.span_kind == "method":
                    method_index[(parent, name)] = span  # parent segment IS the class fq
        module_set = set(ctx.spans_by_module)

        edges: dict[str, _Edge] = {}
        for module, spans in ctx.spans_by_module.items():
            table = self._import_table(ctx, module, module_set)
            for span in spans:
                if span.span_kind not in ("module", "function", "method"):
                    continue
                scan_root = self._caller_scan_root(span)
                if scan_root is None:
                    continue
                for call in _iter_calls(scan_root):
                    resolved = self._resolve(call, span, module, table, def_index,
                                             method_index, module_set)
                    if resolved is None:
                        continue
                    callee, callee_module, resolution = resolved
                    key = f"call:{span.fq_symbol_path}->{callee.fq_symbol_path}"
                    edge = edges.get(key)
                    if edge is None:
                        edge = _Edge(
                            caller=span, caller_kind=span.span_kind, caller_module=module,
                            callee=callee, callee_module=callee_module, resolution=resolution,
                        )
                        edges[key] = edge
                    edge.lines.add(span.start_line + call.start_point[0])

        return [self._build_artifact(key, edge) for key, edge in edges.items()]

    # --- resolution ----------------------------------------------------------

    def _resolve(
        self,
        call: Node,
        span: ParsedSpan,
        module: str,
        table: _ImportTable,
        def_index: dict[tuple[str, str], ParsedSpan],
        method_index: dict[tuple[str, str], ParsedSpan],
        module_set: set[str],
    ) -> tuple[ParsedSpan, str, str] | None:
        fn = call.child_by_field_name("function")
        if fn is None:
            return None
        if fn.type == "identifier":
            name = _text(fn) or ""
            local = def_index.get((module, name))
            if local is not None:
                return local, module, "same_module"
            bound = table.symbols.get(name)
            if bound is not None:
                target = def_index.get(bound)
                if target is not None:
                    return target, bound[0], "imported"
            return None
        if fn.type == "attribute":
            attr = _text(fn.child_by_field_name("attribute"))
            obj = fn.child_by_field_name("object")
            if attr is None or obj is None:
                return None
            if obj.type == "identifier" and _text(obj) == "self" and span.span_kind == "method":
                class_fq = span.fq_symbol_path.rsplit(".", 1)[0]
                target = method_index.get((class_fq, attr))
                if target is not None:
                    return target, module, "self"
                return None  # inherited / dynamic -> documented gap
            obj_text = _dotted_object_text(obj)
            if obj_text is None:  # call()/subscript/... receivers -> documented gap
                return None
            parts = obj_text.split(".")
            for k in range(len(parts), 0, -1):
                bound_module = table.modules.get(".".join(parts[:k]))
                if bound_module is None:
                    continue
                candidate = ".".join([bound_module, *parts[k:]])
                if candidate in module_set:
                    target = def_index.get((candidate, attr))
                    if target is not None:
                        return target, candidate, "imported"
                break
            return None
        return None  # call-of-call (getattr/super/chained outer), lambda, subscript, ...

    # --- import table ---------------------------------------------------------

    def _import_table(self, ctx: ExtractContext, module: str, module_set: set[str]) -> _ImportTable:
        return _import_table(self._parser, ctx, module, module_set)

    def _parse_file(self, ctx: ExtractContext, module: str) -> Node | None:
        return _parse_file(self._parser, ctx, module)

    def _caller_scan_root(self, span: ParsedSpan) -> Node | None:
        return _caller_scan_root(self._parser, span)

    # --- artifact assembly -----------------------------------------------------

    def _build_artifact(self, key: str, edge: _Edge) -> ExtractedArtifact:
        grounding: dict[bytes, DerivedEdge] = {
            edge.caller.span_id: DerivedEdge(edge.caller.span_id, "caller")
        }
        # direct recursion: caller == callee span -> the single row keeps role "caller"
        grounding.setdefault(edge.callee.span_id, DerivedEdge(edge.callee.span_id, "callee"))
        return ExtractedArtifact(
            kind="call_edge",
            logical_key=key,
            payload={
                "caller": edge.caller.fq_symbol_path,
                "caller_kind": edge.caller_kind,
                "caller_module": edge.caller_module,
                "callee": edge.callee.fq_symbol_path,
                "callee_module": edge.callee_module,
                "callee_kind": edge.callee.span_kind,
                "resolution": edge.resolution,
                "lines": sorted(edge.lines),
                "span_mapping": "exact",
                "limitations": [],
            },
            derived_from=list(grounding.values()),
            extractor_id=self.extractor_id,
            extractor_version=self.extractor_version,
        )


# --- module-level helpers (kept local; mirror the other deterministic extractors) ----


def _import_table(
    parser: Parser, ctx: ExtractContext, module: str, module_set: set[str]
) -> _ImportTable:
    """Per-module import bindings (shared with the events call-form scan)."""
    symbols: dict[str, tuple[str, str]] = {}
    modules: dict[str, str] = {}
    root = _parse_file(parser, ctx, module)
    if root is None:
        return _ImportTable(symbols, modules)
    base = _relative_base(module, ctx.path_by_module.get(module, ""))
    for stmt in _iter_import_statements(root):
        if stmt.type == "import_from_statement":
            module_ref = stmt.child_by_field_name("module_name")
            if module_ref is None:
                continue
            target = _resolve_module_ref(module_ref, base)
            if target is None:
                continue
            for name_node in stmt.children_by_field_name("name"):
                exposed, original = _import_alias(name_node)
                if exposed is None or original is None:  # wildcard -> nothing
                    continue
                symbols[exposed] = (target, original)
                if f"{target}.{original}" in module_set:  # `from pkg import sub` (a module)
                    modules[exposed] = f"{target}.{original}"
        else:  # plain import_statement
            for name_node in stmt.children_by_field_name("name"):
                if name_node.type == "dotted_name":  # import x.y
                    dotted = _text(name_node) or ""
                    if dotted:
                        modules[dotted] = dotted
                        modules.setdefault(dotted.split(".")[0], dotted.split(".")[0])
                elif name_node.type == "aliased_import":  # import x.y as z
                    original = _text(name_node.child_by_field_name("name"))
                    alias = _text(name_node.child_by_field_name("alias"))
                    if original and alias:
                        modules[alias] = original
    return _ImportTable(symbols, modules)


def _parse_file(parser: Parser, ctx: ExtractContext, module: str) -> Node | None:
    path = ctx.path_by_module.get(module)
    if path is None:
        return None
    source = (Path(ctx.materialized_root) / path).read_bytes()
    return parser.parse(source).root_node


def _caller_scan_root(parser: Parser, span: ParsedSpan) -> Node | None:
    """The subtree whose calls belong to one caller: a def's BODY (its own decorators, parameters
    and annotations are excluded by construction), or the whole file for a module. Shared with the
    process-path sink scan AND the events call-form scan so call attribution stays bit-identical
    across all three."""
    root = parser.parse(textwrap.dedent(span.raw_text).encode("utf-8")).root_node
    if span.span_kind == "module":
        return root
    deco = _first_child_of_type(root, "decorated_definition")
    host = deco if deco is not None else root
    fn = _first_child_of_type(host, "function_definition")
    return fn.child_by_field_name("body") if fn is not None else None


def _iter_calls(node: Node) -> Iterator[Node]:
    """``call`` nodes lexically owned by ONE caller: stops at nested def/class subtrees (each
    nested def is its own caller span; descending would double-attribute). Recurses INTO call
    nodes too, so the inner call of a chained ``a().b()`` is collected."""
    for child in node.named_children:
        if child.type in _SCOPE_BARRIERS:
            continue
        if child.type == "call":
            yield child
        yield from _iter_calls(child)


def _iter_import_statements(node: Node) -> Iterator[Node]:
    for child in node.named_children:
        if child.type in _IMPORT_TYPES:
            yield child
        else:
            yield from _iter_import_statements(child)


def _dotted_object_text(node: Node) -> str | None:
    """Dotted text of a pure identifier/attribute chain (``a`` / ``a.b.c``), else None."""
    if node.type == "identifier":
        return _text(node)
    if node.type == "attribute":
        obj = node.child_by_field_name("object")
        attr = node.child_by_field_name("attribute")
        if obj is None or attr is None:
            return None
        left = _dotted_object_text(obj)
        right = _text(attr)
        if left is None or right is None:
            return None
        return f"{left}.{right}"
    return None


def _relative_base(module: str, path: str) -> str | None:
    """The package a relative import resolves against: the module itself for an ``__init__``,
    its parent package otherwise; None for a top-level non-package module (invalid Python)."""
    if path.split("/")[-1] == "__init__.py":
        return module
    return module.rsplit(".", 1)[0] if "." in module else None


def _resolve_module_ref(node: Node, base: str | None) -> str | None:
    if node.type == "dotted_name":
        return _text(node)
    if node.type == "relative_import":
        if base is None:
            return None
        prefix = node.child(0)
        dots = len((prefix.text or b"").decode("utf-8")) if prefix is not None else 0
        tail_node = node.child_by_field_name("module_name") or _first_child_of_type(
            node, "dotted_name"
        )
        return _resolve_relative(base, dots, _text(tail_node) if tail_node else None)
    return None


def _resolve_relative(package: str, dots: int, tail: str | None) -> str | None:
    parts = package.split(".") if package else []
    drop = dots - 1
    if drop > len(parts):
        return None
    base = parts[: len(parts) - drop] if drop else parts
    full = base + (tail.split(".") if tail else [])
    return ".".join(full) if full else None


def _import_alias(node: Node) -> tuple[str | None, str | None]:
    """``(exposed, original)`` for an imported-name node; ``(None, None)`` for a wildcard."""
    if node.type == "dotted_name":
        text = _text(node)
        return (text, text)
    if node.type == "aliased_import":
        original = _text(node.child_by_field_name("name"))
        alias = _text(node.child_by_field_name("alias"))
        return (alias, original)
    return (None, None)


def _first_child_of_type(node: Node, type_name: str) -> Node | None:
    for child in node.named_children:
        if child.type == type_name:
            return child
    return None


def _text(node: Node | None) -> str | None:
    if node is None or node.text is None:
        return None
    return node.text.decode("utf-8")
