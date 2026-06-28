"""Deterministic library public-API-surface extractor (DESIGN.md §8, §14).

Produces one ``public_symbol`` artifact per name a package exposes from its ``__init__.py`` — what
``import pkg; pkg.X`` makes available. The public surface is determined statically (tree-sitter),
the same discipline as the FastAPI / entity extractors: it never imports or executes user code.
``griffe`` is used only as a dev-only independent oracle in the gate, never on the index path.

Rules (mirroring griffe's ``is_public``): when a package's ``__init__`` defines ``__all__`` it is
authoritative (a name is public iff it is listed); otherwise the surface is the module's top-level,
non-underscore functions/classes (imported names are NOT public without ``__all__``). Re-exports
(``from .sub import X``) are resolved cross-file to the defining function/class span (role
``definition``) and grounded additionally on the ``__init__`` import statement (role ``re_export``).
``framework_versions`` is empty: a public surface is framework-version-independent (like imports).

Documented gaps (recorded in ``payload.limitations``, never a silent loss): dynamic ``__all__``,
star re-exports, re-exports of third-party / non-first-party symbols, unresolved relative imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import tree_sitter_python as tsp
from tree_sitter import Language, Node, Parser

from kb.extract.base import DerivedEdge, ExtractContext, ExtractedArtifact
from kb.structural.interface import ParsedSpan

EXTRACTOR_ID = "library_surface"
EXTRACTOR_VERSION = "1"

_LANGUAGE = Language(tsp.language())


@dataclass(frozen=True)
class _ReExport:
    target_module: str | None  # None when a relative import cannot be resolved
    target_symbol: str
    import_line: int  # 1-based start line of the `from ... import ...` statement in __init__.py


class LibrarySurfaceExtractor:
    extractor_id = EXTRACTOR_ID
    extractor_version = EXTRACTOR_VERSION

    def __init__(self) -> None:
        self._parser = Parser(_LANGUAGE)

    def extract(self, ctx: ExtractContext) -> list[ExtractedArtifact]:
        def_index: dict[tuple[str, str], ParsedSpan] = {}
        for module, spans in ctx.spans_by_module.items():
            for span in spans:
                if span.span_kind in ("class", "function"):
                    parent, _, name = span.fq_symbol_path.rpartition(".")
                    if parent == module:  # top-level definitions only (no methods / nested defs)
                        def_index[(module, name)] = span
        module_set = set(ctx.spans_by_module)
        import_span_by_line = {
            module: {s.start_line: s for s in spans if s.span_kind == "import"}
            for module, spans in ctx.spans_by_module.items()
        }
        module_span = {
            module: next((s for s in spans if s.span_kind == "module"), None)
            for module, spans in ctx.spans_by_module.items()
        }

        artifacts: list[ExtractedArtifact] = []
        seen: set[str] = set()
        for package in self._package_modules(ctx):
            root = self._parse_file(ctx, package)
            if root is None:
                continue
            all_names, all_dynamic = _parse_all(root)
            reexports = _parse_reexports(root, package)
            names = self._surface_names(package, all_names, def_index)
            for name in sorted(names):
                art = self._build_artifact(
                    package, name, all_names is not None, all_dynamic, reexports,
                    def_index, module_set, import_span_by_line, module_span,
                )
                if art is not None and art.logical_key not in seen:
                    seen.add(art.logical_key)
                    artifacts.append(art)
        return artifacts

    # --- surface determination ---------------------------------------------

    def _surface_names(
        self,
        package: str,
        all_names: set[str] | None,
        def_index: dict[tuple[str, str], ParsedSpan],
    ) -> set[str]:
        if all_names is not None:
            return all_names  # __all__ is authoritative
        # No __all__: top-level non-underscore functions/classes (imported names are NOT public).
        return {
            name
            for (module, name) in def_index
            if module == package and not name.startswith("_")
        }

    def _build_artifact(
        self,
        package: str,
        name: str,
        has_all: bool,
        all_dynamic: bool,
        reexports: dict[str, _ReExport],
        def_index: dict[tuple[str, str], ParsedSpan],
        module_set: set[str],
        import_span_by_line: dict[str, dict[int, ParsedSpan]],
        module_span: dict[str, ParsedSpan | None],
    ) -> ExtractedArtifact | None:
        grounding: dict[bytes, DerivedEdge] = {}
        limitations: list[str] = []
        if all_dynamic:
            limitations.append("dynamic_all")

        in_place = def_index.get((package, name))
        reexport = reexports.get(name)
        defining_module: str | None = None
        defining_fq: str | None = None
        symbol_kind: str | None = None
        signature: str | None = None
        is_reexport = False
        span_mapping = "exact"

        if in_place is not None:
            symbol_kind = in_place.span_kind
            defining_module, defining_fq = package, in_place.fq_symbol_path
            signature = self._signature(in_place)
            grounding[in_place.span_id] = DerivedEdge(in_place.span_id, "definition")
        elif reexport is not None:
            is_reexport = True
            re_span = import_span_by_line.get(package, {}).get(reexport.import_line)
            if re_span is not None:
                grounding[re_span.span_id] = DerivedEdge(re_span.span_id, "re_export")
            target = (
                def_index.get((reexport.target_module, reexport.target_symbol))
                if reexport.target_module is not None
                else None
            )
            if target is not None:  # first-party re-export -> cross-file definition grounding
                symbol_kind = target.span_kind
                defining_module, defining_fq = reexport.target_module, target.fq_symbol_path
                signature = self._signature(target)
                grounding[target.span_id] = DerivedEdge(target.span_id, "definition")
            elif reexport.target_module is None:
                limitations.append("relative_import_unresolved")
                span_mapping = "approximate"
            elif _is_submodule(reexport, module_set):
                return None  # re-exported submodule (module kind) -> not a function/class symbol
            elif reexport.target_module in module_set:
                return None  # first-party but not a top-level class/function (variable/dynamic)
            else:  # third-party / non-first-party symbol -> grounded on the re-export span only
                defining_module = reexport.target_module
                limitations.append("definition_not_first_party")
                span_mapping = "approximate"
        else:
            return None  # listed in __all__ but neither defined here nor imported -> cannot ground

        if not grounding:  # last resort: never store ungrounded (writer would reject it anyway)
            fallback = module_span.get(package)
            if fallback is None:
                return None
            grounding[fallback.span_id] = DerivedEdge(fallback.span_id, "re_export")
            span_mapping = "approximate"

        payload = {
            "public_qualified_name": f"{package}.{name}",
            "name": name,
            "symbol_kind": symbol_kind,
            "exporting_module": package,
            "defining_module": defining_module,
            "defining_fq_symbol_path": defining_fq,
            "exported_via": "all" if has_all else "naming",
            "is_reexport": is_reexport,
            "signature": signature,
            "span_mapping": span_mapping,
            "limitations": limitations,
        }
        return ExtractedArtifact(
            kind="public_symbol",
            logical_key=f"surface:{package}.{name}",
            payload=payload,
            derived_from=list(grounding.values()),
            extractor_id=self.extractor_id,
            extractor_version=self.extractor_version,
        )

    # --- file access -------------------------------------------------------

    def _package_modules(self, ctx: ExtractContext) -> list[str]:
        return sorted(
            module
            for module, path in ctx.path_by_module.items()
            if path.split("/")[-1] == "__init__.py"
        )

    def _parse_file(self, ctx: ExtractContext, module: str) -> Node | None:
        path = ctx.path_by_module.get(module)
        if path is None:
            return None
        source = (Path(ctx.materialized_root) / path).read_bytes()
        return self._parser.parse(source).root_node

    def _signature(self, span: ParsedSpan) -> str | None:
        root = self._parser.parse(span.raw_text.encode("utf-8")).root_node
        deco = _first_child_of_type(root, "decorated_definition")
        host = deco if deco is not None else root
        fn = _first_child_of_type(host, "function_definition")
        if fn is not None:
            params = _text(fn.child_by_field_name("parameters")) or "()"
            ret = _text(fn.child_by_field_name("return_type"))
            return f"{params} -> {ret}" if ret else params
        cls = _first_child_of_type(host, "class_definition")
        if cls is not None:
            return _text(cls.child_by_field_name("superclasses"))
        return None


# --- module-level helpers (kept local; mirror the other deterministic extractors) ----


def _is_submodule(reexport: _ReExport, module_set: set[str]) -> bool:
    return f"{reexport.target_module}.{reexport.target_symbol}" in module_set


def _parse_all(root: Node) -> tuple[set[str] | None, bool]:
    """Return ``(__all__ names | None, dynamic?)`` from a module body.

    The last top-level ``__all__`` assignment wins (Python semantics). A non-literal RHS (a name, a
    concatenation, an augmented assignment, a comprehension) is a dynamic ``__all__`` -> ``(None,
    True)``; a literal list/tuple/set of string literals yields the name set.
    """
    names: set[str] | None = None
    dynamic = False
    for stmt in root.named_children:
        if stmt.type == "expression_statement":
            assign = _first_child_of_type(stmt, "assignment")
            if assign is None or _text(assign.child_by_field_name("left")) != "__all__":
                continue
            right = assign.child_by_field_name("right")
            if right is not None and right.type in ("list", "tuple", "set"):
                literal = _string_list(right)
                names, dynamic = (literal, False) if literal is not None else (None, True)
            else:
                names, dynamic = None, True
        elif stmt.type == "augmented_assignment":
            if _text(stmt.child_by_field_name("left")) == "__all__":
                dynamic = True
    return names, dynamic


def _string_list(node: Node) -> set[str] | None:
    """The set of string-literal elements of a list/tuple/set, or None if any element isn't one."""
    out: set[str] = set()
    for child in node.named_children:
        if child.type != "string":
            return None
        value = _string_value(child)
        if value is None:
            return None
        out.add(value)
    return out


def _parse_reexports(root: Node, package: str) -> dict[str, _ReExport]:
    """Map each name brought into ``package``'s ``__init__`` by ``from ... import ...`` to its
    target ``(module, symbol)`` + the import statement's line (for cross-file grounding)."""
    out: dict[str, _ReExport] = {}
    for stmt in root.named_children:
        if stmt.type != "import_from_statement":
            continue
        module_ref = stmt.child_by_field_name("module_name")
        if module_ref is None:
            continue
        target_module = _resolve_module_ref(module_ref, package)
        line = stmt.start_point[0] + 1
        for name_node in stmt.children_by_field_name("name"):
            exposed, original = _import_alias(name_node)
            if exposed is None or original is None:  # wildcard / unexpected shape
                continue
            out[exposed] = _ReExport(target_module, original, line)
    return out


def _resolve_module_ref(node: Node, package: str) -> str | None:
    if node.type == "dotted_name":
        return _text(node)
    if node.type == "relative_import":
        prefix = node.child(0)
        dots = len((prefix.text or b"").decode("utf-8")) if prefix is not None else 0
        tail_node = node.child_by_field_name("module_name") or _first_child_of_type(
            node, "dotted_name"
        )
        return _resolve_relative(package, dots, _text(tail_node) if tail_node else None)
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
    """``(exposed, original)`` for an imported-name node; ``(None, None)`` for a wildcard import."""
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


def _string_value(node: Node) -> str | None:
    if node.type != "string":
        return None
    contents = [
        (child.text or b"").decode("utf-8", errors="replace")
        for child in node.named_children
        if child.type == "string_content"
    ]
    if contents:
        return "".join(contents)
    return (node.text or b"").decode("utf-8", errors="replace").strip("\"'")


def _text(node: Node | None) -> str | None:
    if node is None or node.text is None:
        return None
    return node.text.decode("utf-8")
