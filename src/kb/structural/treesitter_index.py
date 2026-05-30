"""tree-sitter backend for the structural layer (DESIGN.md §11).

Parses Python source into content-addressed spans: one ``module`` span per file, one span per
class/function/method (covering decorators), and one span per import statement. Never imports or
executes the analyzed code.
"""

from __future__ import annotations

import tree_sitter_python as tsp
from tree_sitter import Language, Node, Parser

from kb.ids import compute_span_id
from kb.structural import symbol_path as sp
from kb.structural.fingerprint import structural_fingerprint
from kb.structural.interface import ParsedSpan, ParseResult

_LANGUAGE = Language(tsp.language())
_LANG_NAME = "python"
_DEF_TYPES = frozenset({"function_definition", "class_definition"})
_IMPORT_TYPES = frozenset({"import_statement", "import_from_statement"})


class TreeSitterIndex:
    """A ``StructuralIndex`` over Python source via tree-sitter."""

    def __init__(self) -> None:
        self._parser = Parser(_LANGUAGE)

    def parse_file(self, module_fqname: str, source: bytes) -> ParseResult:
        root = self._parser.parse(source).root_node
        spans: list[ParsedSpan] = [self._make_span("module", module_fqname, root)]
        spans.extend(self._walk_defs(root, module_fqname, [], in_class=False))
        spans.extend(self._collect_imports(root, module_fqname))
        return ParseResult(spans=spans, has_error=root.has_error)

    # --- span construction -------------------------------------------------

    def _make_span(self, span_kind: str, fq_symbol_path: str, node: Node) -> ParsedSpan:
        fingerprint = structural_fingerprint(node)
        span_id = compute_span_id(
            lang=_LANG_NAME,
            span_kind=span_kind,
            fq_symbol_path=fq_symbol_path,
            structural_fingerprint=fingerprint,
        )
        return ParsedSpan(
            span_kind=span_kind,
            fq_symbol_path=fq_symbol_path,
            structural_fingerprint=fingerprint,
            span_id=span_id,
            lang=_LANG_NAME,
            start_byte=node.start_byte,
            end_byte=node.end_byte,
            start_line=node.start_point[0] + 1,  # tree-sitter rows are 0-based; store 1-based
            end_line=node.end_point[0] + 1,
            raw_text=(node.text or b"").decode("utf-8", errors="replace"),
        )

    # --- definitions (classes / functions / methods) -----------------------

    def _walk_defs(
        self, node: Node, module: str, name_stack: list[str], in_class: bool
    ) -> list[ParsedSpan]:
        out: list[ParsedSpan] = []
        for child in node.named_children:
            inner = self._inner_def(child) if child.type == "decorated_definition" else child
            if inner is not None and inner.type in _DEF_TYPES:
                name = self._name_of(inner)
                if name is None:
                    continue
                is_class = inner.type == "class_definition"
                kind = "class" if is_class else ("method" if in_class else "function")
                new_stack = [*name_stack, name]
                # span node is `child` so decorators are included (needed by the API extractor)
                out.append(self._make_span(kind, sp.symbol_fqname(module, new_stack), child))
                body = inner.child_by_field_name("body")
                if body is not None:
                    out.extend(self._walk_defs(body, module, new_stack, in_class=is_class))
            else:
                # descend into compound statements (if/for/while/with/try) that may nest defs
                out.extend(self._walk_defs(child, module, name_stack, in_class))
        return out

    @staticmethod
    def _inner_def(decorated: Node) -> Node | None:
        for child in decorated.named_children:
            if child.type in _DEF_TYPES:
                return child
        return None

    @staticmethod
    def _name_of(definition: Node) -> str | None:
        name = definition.child_by_field_name("name")
        if name is None or name.text is None:
            return None
        return name.text.decode("utf-8")

    # --- imports -----------------------------------------------------------

    def _collect_imports(self, root: Node, module: str) -> list[ParsedSpan]:
        nodes: list[Node] = []
        self._find_imports(root, nodes)
        nodes.sort(key=lambda n: n.start_byte)  # deterministic source order -> stable ordinals
        return [
            self._make_span("import", sp.import_fqname(module, ordinal), node)
            for ordinal, node in enumerate(nodes)
        ]

    def _find_imports(self, node: Node, acc: list[Node]) -> None:
        for child in node.named_children:
            if child.type in _IMPORT_TYPES:
                acc.append(child)
            else:
                self._find_imports(child, acc)
