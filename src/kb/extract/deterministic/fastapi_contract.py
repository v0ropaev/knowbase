"""FastAPI API-contract extractor — the honest thesis-test extractor (DESIGN.md §8).

Produces one ``api_route`` artifact per ``@router.<verb>("/path", response_model=...)`` handler,
grounded on the handler function span (role ``handler``) AND — across files — on the response
model's class span (role ``response_model``). That cross-file link is what RAG-over-chunks misses.

Fully static: re-parses each handler span's source with tree-sitter; it never imports or executes
user code (the runtime ``app.openapi()`` oracle lives in ``kb.introspect`` and is eval-only).
``framework_versions`` is read from the ANALYZED repo at the SHA (not knowbase's own env) and folded
into the artifact key, since the contract shape can depend on the fastapi/pydantic version
(DESIGN.md §6).
"""

from __future__ import annotations

import textwrap
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import tree_sitter_python as tsp
from tree_sitter import Language, Node, Parser

from kb.extract.base import DerivedEdge, ExtractContext, ExtractedArtifact
from kb.structural.interface import ParsedSpan

EXTRACTOR_ID = "fastapi_contract"
EXTRACTOR_VERSION = "1"

_LANGUAGE = Language(tsp.language())
_HTTP_VERBS = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "trace"})
_SCALAR_ANNOTATIONS = frozenset({"int", "str", "float", "bool", "bytes", "UUID"})


@dataclass(frozen=True)
class _Param:
    name: str
    annotation: str | None
    has_default: bool
    location: str  # path | query | body | unknown


@dataclass(frozen=True)
class _Route:
    method: str  # upper-cased HTTP verb
    raw_path: str
    decorator_object: str | None
    response_model: str | None  # raw text, e.g. "List[OrderOut]"
    response_model_base: str | None  # innermost identifier, e.g. "OrderOut"
    status_code: int | None
    include_in_schema: bool
    params: list[_Param] = field(default_factory=list)
    return_type: str | None = None


class FastAPIExtractor:
    extractor_id = EXTRACTOR_ID
    extractor_version = EXTRACTOR_VERSION

    def __init__(self) -> None:
        self._parser = Parser(_LANGUAGE)

    def extract(self, ctx: ExtractContext) -> list[ExtractedArtifact]:
        framework_versions = _framework_versions(ctx)
        class_index = _index_class_spans(ctx)
        prefixes, unresolved_prefix_routers = self._router_prefixes(ctx)

        artifacts: list[ExtractedArtifact] = []
        seen: set[str] = set()
        for module, spans in ctx.spans_by_module.items():
            for span in spans:
                if span.span_kind not in ("function", "method"):
                    continue
                for route in self._parse_routes(span):
                    art = self._build_artifact(
                        module,
                        span,
                        route,
                        class_index,
                        prefixes,
                        unresolved_prefix_routers,
                        framework_versions,
                    )
                    if art.logical_key not in seen:
                        seen.add(art.logical_key)
                        artifacts.append(art)
        return artifacts

    # --- route parsing (static, re-parse the span) -------------------------

    def _parse_routes(self, span: ParsedSpan) -> list[_Route]:
        root = self._parser.parse(textwrap.dedent(span.raw_text).encode("utf-8")).root_node
        deco_def = _first_child_of_type(root, "decorated_definition")
        if deco_def is None:
            return []
        fn = _first_child_of_type(deco_def, "function_definition")
        raw_params = _parse_params(fn) if fn is not None else []
        return_type = _text(fn.child_by_field_name("return_type")) if fn is not None else None

        routes: list[_Route] = []
        for decorator in deco_def.named_children:
            if decorator.type != "decorator":
                continue
            call = _first_child_of_type(decorator, "call")
            if call is None:
                continue
            callee = call.child_by_field_name("function")
            if callee is None or callee.type != "attribute":
                continue
            verb = _text(callee.child_by_field_name("attribute"))
            if verb not in _HTTP_VERBS:
                continue
            args = call.child_by_field_name("arguments")
            path = _first_string_literal(args)
            if path is None:  # dynamic / computed path -> known gap, never a wrong guess
                continue
            rm_value = _keyword_value(args, "response_model")
            path_names = _path_param_names(path)
            params = [
                _Param(name, annotation, has_default,
                       _classify_param(name, annotation, has_default, path_names))
                for (name, annotation, has_default) in raw_params
            ]
            routes.append(
                _Route(
                    method=verb.upper(),
                    raw_path=path,
                    decorator_object=_text(callee.child_by_field_name("object")),
                    response_model=_text(rm_value),
                    response_model_base=_innermost_identifier(rm_value) if rm_value else None,
                    status_code=_keyword_int(args, "status_code"),
                    include_in_schema=_keyword_bool(args, "include_in_schema", default=True),
                    params=params,
                    return_type=return_type,
                )
            )
        return routes

    # --- router prefixes (best-effort, flagged) ----------------------------

    def _router_prefixes(self, ctx: ExtractContext) -> tuple[dict[str, str], set[str]]:
        """var_name -> literal prefix, and the set of router var names with a non-literal prefix."""
        prefixes: dict[str, str] = {}
        unresolved: set[str] = set()
        root_dir = Path(ctx.materialized_root)
        for path in ctx.path_by_module.values():
            source = (root_dir / path).read_bytes()
            tree = self._parser.parse(source).root_node
            for call in _iter_descendants(tree, "call"):
                fn = call.child_by_field_name("function")
                if fn is None or fn.type != "attribute":
                    continue
                if _text(fn.child_by_field_name("attribute")) != "include_router":
                    continue
                args = call.child_by_field_name("arguments")
                if args is None:
                    continue
                target = next((c for c in args.named_children if c.type == "identifier"), None)
                if target is None:
                    continue
                self._apply_prefix(
                    _text(target), _keyword_value(args, "prefix"), prefixes, unresolved
                )
            for assign in _iter_descendants(tree, "assignment"):
                left = assign.child_by_field_name("left")
                right = assign.child_by_field_name("right")
                if (
                    left is None
                    or left.type != "identifier"
                    or right is None
                    or right.type != "call"
                ):
                    continue
                callee = right.child_by_field_name("function")
                if callee is None or _text(callee) != "APIRouter":
                    continue
                self._apply_prefix(
                    _text(left), _keyword_value(right.child_by_field_name("arguments"), "prefix"),
                    prefixes, unresolved,
                )
        return prefixes, unresolved

    @staticmethod
    def _apply_prefix(
        name: str | None, value: Node | None, prefixes: dict[str, str], unresolved: set[str]
    ) -> None:
        if name is None or value is None:
            return
        if value.type == "string":
            prefixes[name] = prefixes.get(name, "") + _string_literal_value(value)
        else:
            unresolved.add(name)  # non-literal prefix -> cannot resolve confidently

    # --- artifact assembly -------------------------------------------------

    def _build_artifact(
        self,
        module: str,
        span: ParsedSpan,
        route: _Route,
        class_index: dict[str, list[tuple[str, ParsedSpan]]],
        prefixes: dict[str, str],
        unresolved_prefix_routers: set[str],
        framework_versions: dict[str, str],
    ) -> ExtractedArtifact:
        grounding: dict[bytes, DerivedEdge] = {span.span_id: DerivedEdge(span.span_id, "handler")}
        targets = class_index.get(route.response_model_base or "", [])
        for _tgt_module, tgt_span in targets:
            grounding[tgt_span.span_id] = DerivedEdge(tgt_span.span_id, "response_model")
        resolved = bool(targets)

        receiver = route.decorator_object
        prefix_unresolved = receiver in unresolved_prefix_routers
        prefix = "" if prefix_unresolved else prefixes.get(receiver or "", "")
        full_path = f"{prefix}{route.raw_path}"

        limitations: list[str] = []
        if route.response_model and "[" in route.response_model:
            limitations.append("generic_response_model")
        if route.response_model_base and not resolved:
            limitations.append("response_model_not_first_party")
        if prefix_unresolved:
            limitations.append("dynamic_prefix")

        payload = {
            "method": route.method,
            "path": full_path,
            "raw_path": route.raw_path,
            "decorator_object": receiver,
            "handler": span.fq_symbol_path,
            "handler_module": module,
            "status_code": route.status_code,
            "include_in_schema": route.include_in_schema,
            "response_model": route.response_model,
            "response_model_base": route.response_model_base,
            "response_model_resolved": resolved,
            "response_model_ambiguous": len(targets) > 1,
            "response_model_targets": [
                {"module": m, "fq_symbol_path": s.fq_symbol_path} for (m, s) in targets
            ],
            "return_type": route.return_type,
            "params": [
                {"name": p.name, "annotation": p.annotation, "has_default": p.has_default,
                 "in": p.location}
                for p in route.params
            ],
            "prefix_resolved": not prefix_unresolved,
            "span_mapping": "exact",
            "limitations": limitations,
        }
        return ExtractedArtifact(
            kind="api_route",
            logical_key=f"api:{route.method} {full_path}",
            payload=payload,
            derived_from=list(grounding.values()),
            extractor_id=self.extractor_id,
            extractor_version=self.extractor_version,
            framework_versions=framework_versions,
        )


# --- module-level helpers --------------------------------------------------


def _index_class_spans(ctx: ExtractContext) -> dict[str, list[tuple[str, ParsedSpan]]]:
    index: dict[str, list[tuple[str, ParsedSpan]]] = {}
    for module, spans in ctx.spans_by_module.items():
        for span in spans:
            if span.span_kind == "class":
                base = span.fq_symbol_path.rsplit(".", 1)[-1]
                index.setdefault(base, []).append((module, span))
    return index


def _parse_params(fn: Node) -> list[tuple[str, str | None, bool]]:
    """Raw (name, annotation, has_default) per parameter; classification is per-route."""
    params_node = fn.child_by_field_name("parameters")
    if params_node is None:
        return []
    out: list[tuple[str, str | None, bool]] = []
    for node in params_node.named_children:
        if node.type in ("list_splat_pattern", "dictionary_splat_pattern"):
            continue
        name, annotation, has_default = _decode_param(node)
        if name is None or name in ("self", "cls"):
            continue
        out.append((name, annotation, has_default))
    return out


def _path_param_names(path: str) -> frozenset[str]:
    names: set[str] = set()
    depth = 0
    current = ""
    for char in path:
        if char == "{":
            depth, current = depth + 1, ""
        elif char == "}" and depth:
            depth -= 1
            names.add(current.split(":", 1)[0])  # FastAPI allows "{id:int}" path converters
        elif depth:
            current += char
    return frozenset(names)


def _decode_param(node: Node) -> tuple[str | None, str | None, bool]:
    if node.type == "identifier":
        return _text(node), None, False
    name_node = node.child_by_field_name("name")
    name = _text(name_node) if name_node is not None else _text(
        next((c for c in node.named_children if c.type == "identifier"), None)
    )
    annotation = _text(node.child_by_field_name("type"))
    has_default = node.child_by_field_name("value") is not None or node.type in (
        "default_parameter",
        "typed_default_parameter",
    )
    return name, annotation, has_default


def _classify_param(
    name: str, annotation: str | None, has_default: bool, path_names: frozenset[str]
) -> str:
    # Path is exact (driven by {…} in the literal path); query/body is a recorded heuristic. The
    # gate only asserts on path+query, never on "unknown".
    if name in path_names:
        return "path"
    model_like = (
        annotation is not None
        and annotation[:1].isupper()
        and annotation not in _SCALAR_ANNOTATIONS
    )
    if model_like:
        return "body"  # a model-like (PascalCase) annotation -> likely a request body
    if has_default or annotation in _SCALAR_ANNOTATIONS:
        return "query"
    return "unknown"


def _framework_versions(ctx: ExtractContext) -> dict[str, str]:
    root = Path(ctx.materialized_root)
    found: dict[str, str] = {}
    for lock in ("uv.lock", "poetry.lock"):
        path = root / lock
        if path.exists():
            data = tomllib.loads(path.read_text())
            for pkg in data.get("package", []):
                if pkg.get("name") in ("fastapi", "pydantic") and "version" in pkg:
                    found.setdefault(pkg["name"], str(pkg["version"]))
    pyproject = root / "pyproject.toml"
    if pyproject.exists() and ("fastapi" not in found or "pydantic" not in found):
        data = tomllib.loads(pyproject.read_text())
        deps = data.get("project", {}).get("dependencies", []) or []
        for spec in deps:
            for name in ("fastapi", "pydantic"):
                if name not in found and spec.replace("-", "_").startswith(name):
                    found.setdefault(name, f"spec:{spec}")
    return {name: found.get(name, "unknown") for name in ("fastapi", "pydantic")}


def _first_child_of_type(node: Node, type_name: str) -> Node | None:
    for child in node.named_children:
        if child.type == type_name:
            return child
    return None


def _iter_descendants(node: Node, type_name: str) -> Iterator[Node]:
    for child in node.named_children:
        if child.type == type_name:
            yield child
        yield from _iter_descendants(child, type_name)


def _first_string_literal(args: Node | None) -> str | None:
    if args is None:
        return None
    node = _first_child_of_type(args, "string")
    return _string_literal_value(node) if node is not None else None


def _keyword_value(args: Node | None, name: str) -> Node | None:
    if args is None:
        return None
    for child in args.named_children:
        if child.type == "keyword_argument" and _text(child.child_by_field_name("name")) == name:
            return child.child_by_field_name("value")
    return None


def _keyword_int(args: Node | None, name: str) -> int | None:
    value = _keyword_value(args, name)
    if value is not None and value.type == "integer":
        try:
            return int(_text(value) or "", 0)
        except ValueError:
            return None
    return None


def _keyword_bool(args: Node | None, name: str, *, default: bool) -> bool:
    value = _keyword_value(args, name)
    if value is None:
        return default
    return value.type == "true"


def _innermost_identifier(node: Node) -> str | None:
    found: list[str] = []

    def _walk(current: Node) -> None:
        if current.type == "identifier":
            text = _text(current)
            if text is not None:
                found.append(text)
        for child in current.named_children:
            _walk(child)

    _walk(node)
    return found[-1] if found else None


def _string_literal_value(node: Node) -> str:
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
