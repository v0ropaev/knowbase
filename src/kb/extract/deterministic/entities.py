"""Deterministic domain-entity extractor — pydantic / dataclass / SQLAlchemy (DESIGN.md §4, §14).

Produces one ``entity`` artifact per domain class, grounded on that class's span (role
``class_definition``). Fully static: re-parses each class span's source with tree-sitter (the same
discipline as the FastAPI contract extractor); it never imports or executes user code.

Detection is best-effort and the signals are recorded in the payload (never a silent guess):
  * **dataclass**   — a decorator whose dotted name ends in ``dataclass``.
  * **pydantic**    — a direct base named ``BaseModel`` / ``BaseSettings``.
  * **sqlalchemy**  — a ``__tablename__`` assignment, or a field via ``Mapped[...]`` /
                      ``mapped_column(...)`` / ``Column(...)`` (so a bare declarative ``Base`` with
                      neither is correctly NOT treated as an entity).
``framework_versions`` (pydantic / sqlalchemy) is read from the ANALYZED repo at the SHA and folded
into the artifact key, since field interpretation can shift across major versions (DESIGN.md §6).
"""

from __future__ import annotations

import textwrap
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tree_sitter_python as tsp
from tree_sitter import Language, Node, Parser

from kb.extract.base import DerivedEdge, ExtractContext, ExtractedArtifact
from kb.structural.interface import ParsedSpan

EXTRACTOR_ID = "entities"
EXTRACTOR_VERSION = "1"

_LANGUAGE = Language(tsp.language())
_PYDANTIC_BASES = frozenset({"BaseModel", "BaseSettings"})
_SA_COLUMN_CALLS = frozenset({"Column", "mapped_column"})
_OPTIONAL_MARKERS = ("Optional[", "| None", "None |")
_VERSIONED = ("pydantic", "sqlalchemy")


@dataclass(frozen=True)
class _RawField:
    name: str
    annotation: str | None
    has_default: bool
    value_callee: str | None  # innermost name of a call on the RHS, e.g. "mapped_column" | "Column"


class EntityExtractor:
    extractor_id = EXTRACTOR_ID
    extractor_version = EXTRACTOR_VERSION

    def __init__(self) -> None:
        self._parser = Parser(_LANGUAGE)

    def extract(self, ctx: ExtractContext) -> list[ExtractedArtifact]:
        versions = _framework_versions(ctx, _VERSIONED)
        artifacts: list[ExtractedArtifact] = []
        for module, spans in ctx.spans_by_module.items():
            for span in spans:
                if span.span_kind != "class":
                    continue
                art = self._build_artifact(module, span, versions)
                if art is not None:
                    artifacts.append(art)
        return artifacts

    def _build_artifact(
        self, module: str, span: ParsedSpan, versions: dict[str, str]
    ) -> ExtractedArtifact | None:
        root = self._parser.parse(textwrap.dedent(span.raw_text).encode("utf-8")).root_node
        deco = _first_child_of_type(root, "decorated_definition")
        cls = (
            _first_child_of_type(deco, "class_definition")
            if deco is not None
            else _first_child_of_type(root, "class_definition")
        )
        if cls is None:
            return None

        decorators = _decorator_names(deco) if deco is not None else []
        bases = _base_names(cls)
        body = cls.child_by_field_name("body")
        tablename, raw_fields, relationships = _parse_body(body)

        framework, signals, limitations = _classify(decorators, bases, tablename, raw_fields)
        if framework is None:
            return None

        fields = _select_fields(framework, raw_fields)
        payload: dict[str, Any] = {
            "framework": framework,
            "class_name": span.fq_symbol_path.rsplit(".", 1)[-1],
            "qualified_name": span.fq_symbol_path,
            "module": module,
            "bases": bases,
            "fields": [
                {
                    "name": f.name,
                    "annotation": f.annotation,
                    "has_default": f.has_default,
                    "required": f.required,
                    "source": f.source,
                }
                for f in fields
            ],
            "tablename": tablename,
            "relationships": relationships,
            "detection_signals": signals,
            "span_mapping": "exact",
            "limitations": limitations,
        }
        framework_versions = (
            {} if framework == "dataclass" else {framework: versions.get(framework, "unknown")}
        )
        return ExtractedArtifact(
            kind="entity",
            logical_key=f"entity:{span.fq_symbol_path}",
            payload=payload,
            derived_from=[DerivedEdge(span.span_id, "class_definition")],
            extractor_id=self.extractor_id,
            extractor_version=self.extractor_version,
            framework_versions=framework_versions,
        )


# --- selected field (post-classification) ----------------------------------


@dataclass(frozen=True)
class _Field:
    name: str
    annotation: str | None
    has_default: bool
    required: bool
    source: str  # "annotated" | "column"


def _select_fields(framework: str, raw: Sequence[_RawField]) -> list[_Field]:
    out: list[_Field] = []
    for rf in raw:
        if _is_dunder(rf.name):
            continue
        annotated = rf.annotation is not None
        is_column = rf.value_callee in _SA_COLUMN_CALLS
        if framework == "sqlalchemy":
            is_mapped = rf.annotation is not None and rf.annotation.startswith("Mapped[")
            if not (is_mapped or is_column):
                continue
            source = "annotated" if annotated else "column"
        else:  # pydantic / dataclass fields are always annotated
            if not annotated:
                continue
            source = "annotated"
        out.append(
            _Field(
                name=rf.name,
                annotation=rf.annotation,
                has_default=rf.has_default,
                required=not rf.has_default and not _is_optional(rf.annotation),
                source=source,
            )
        )
    return out


def _classify(
    decorators: Sequence[str],
    bases: Sequence[str],
    tablename: str | None,
    raw: Sequence[_RawField],
) -> tuple[str | None, list[str], list[str]]:
    is_dataclass = any(d.rsplit(".", 1)[-1] == "dataclass" for d in decorators)
    has_column_field = any(
        rf.value_callee in _SA_COLUMN_CALLS
        or (rf.annotation is not None and rf.annotation.startswith("Mapped["))
        for rf in raw
    )
    is_sqlalchemy = tablename is not None or has_column_field
    is_pydantic = any(b in _PYDANTIC_BASES for b in bases)

    signals: list[str] = []
    if is_dataclass:
        signals.append("dataclass_decorator")
    if tablename is not None:
        signals.append("sqlalchemy_tablename")
    if has_column_field:
        signals.append("sqlalchemy_column_field")
    if is_pydantic:
        signals.append("pydantic_base")

    limitations: list[str] = []
    if sum((is_dataclass, is_sqlalchemy, is_pydantic)) > 1:
        limitations.append("multiple_framework_signals")

    # precedence: a dataclass decorator wins; then SQLAlchemy table/columns; then a pydantic base.
    if is_dataclass:
        return "dataclass", signals, limitations
    if is_sqlalchemy:
        return "sqlalchemy", signals, limitations
    if is_pydantic:
        return "pydantic", signals, limitations
    return None, signals, limitations


# --- tree-sitter parsing of the class body ----------------------------------


def _parse_body(
    body: Node | None,
) -> tuple[str | None, list[_RawField], list[dict[str, str | None]]]:
    """Return ``(__tablename__ literal, raw fields, relationships)`` from a class ``block``.

    Only DIRECT body statements are inspected, so assignments inside method bodies are not mistaken
    for fields.
    """
    if body is None:
        return None, [], []
    tablename: str | None = None
    fields: list[_RawField] = []
    relationships: list[dict[str, str | None]] = []
    for stmt in body.named_children:
        assign = _unwrap_assignment(stmt)
        if assign is None:
            continue
        left = assign.child_by_field_name("left")
        if left is None or left.type != "identifier":
            continue
        name = _text(left)
        if name is None:
            continue
        right = assign.child_by_field_name("right")
        callee = _innermost_call_name(right) if right is not None else None
        if name == "__tablename__":
            tablename = _string_value(right) if right is not None else None
            continue
        if callee == "relationship":
            relationships.append({"name": name, "target": _first_argument_text(right)})
        fields.append(
            _RawField(
                name=name,
                annotation=_text(assign.child_by_field_name("type")),
                has_default=right is not None,
                value_callee=callee,
            )
        )
    return tablename, fields, relationships


def _unwrap_assignment(stmt: Node) -> Node | None:
    """A class-body field is an ``assignment`` (possibly wrapped in an ``expression_statement``)."""
    if stmt.type == "assignment":
        return stmt
    if stmt.type == "expression_statement":
        inner = _first_child_of_type(stmt, "assignment")
        if inner is not None:
            return inner
    return None


def _decorator_names(deco: Node) -> list[str]:
    names: list[str] = []
    for child in deco.named_children:
        if child.type != "decorator":
            continue
        target = child.named_children[0] if child.named_children else None
        if target is None:
            continue
        if target.type == "call":
            target = target.child_by_field_name("function")
        text = _text(target)
        if text is not None:
            names.append(text)
    return names


def _base_names(cls: Node) -> list[str]:
    supers = cls.child_by_field_name("superclasses")
    if supers is None:
        return []
    names: list[str] = []
    for arg in supers.named_children:
        if arg.type == "keyword_argument":  # e.g. metaclass=...
            continue
        text = _text(arg)
        if text is not None:
            names.append(text.rsplit(".", 1)[-1])
    return names


# --- small tree-sitter helpers (kept local; mirror the fastapi extractor) ----


def _first_child_of_type(node: Node, type_name: str) -> Node | None:
    for child in node.named_children:
        if child.type == type_name:
            return child
    return None


def _innermost_call_name(node: Node) -> str | None:
    """If ``node`` is (or wraps) a call, return the innermost identifier of its callee."""
    if node.type != "call":
        return None
    fn = node.child_by_field_name("function")
    if fn is None:
        return None
    text = _text(fn)
    return text.rsplit(".", 1)[-1] if text is not None else None


def _first_argument_text(node: Node | None) -> str | None:
    if node is None or node.type != "call":
        return None
    args = node.child_by_field_name("arguments")
    if args is None:
        return None
    first = next((c for c in args.named_children), None)
    return _text(first) if first is not None else None


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


def _is_optional(annotation: str | None) -> bool:
    return annotation is not None and any(marker in annotation for marker in _OPTIONAL_MARKERS)


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def _text(node: Node | None) -> str | None:
    if node is None or node.text is None:
        return None
    return node.text.decode("utf-8")


def _framework_versions(ctx: ExtractContext, names: tuple[str, ...]) -> dict[str, str]:
    """Best-effort versions of ``names`` from the repo's lockfiles / pyproject at the SHA."""
    root = Path(ctx.materialized_root)
    targets = {name.lower(): name for name in names}
    found: dict[str, str] = {}
    for lock in ("uv.lock", "poetry.lock"):
        path = root / lock
        if path.exists():
            data = tomllib.loads(path.read_text())
            for pkg in data.get("package", []):
                key = str(pkg.get("name", "")).lower()
                if key in targets and "version" in pkg and targets[key] not in found:
                    found[targets[key]] = str(pkg["version"])
    pyproject = root / "pyproject.toml"
    if pyproject.exists() and any(name not in found for name in names):
        data = tomllib.loads(pyproject.read_text())
        deps = data.get("project", {}).get("dependencies", []) or []
        for spec in deps:
            normalized = spec.replace("-", "_").lower()
            for key, canonical in targets.items():
                if canonical not in found and normalized.startswith(key):
                    found[canonical] = f"spec:{spec}"
    return {name: found.get(name, "unknown") for name in names}
