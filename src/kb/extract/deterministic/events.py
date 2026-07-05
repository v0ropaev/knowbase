"""Deterministic event-handler extractor — pydantic / FastAPI / SQLAlchemy hooks (DESIGN.md §8).

Produces one ``event_handler`` artifact per HANDLER function/method that carries decorator
registrations: pydantic ``@field_validator(...)`` / ``@model_validator(...)`` methods, FastAPI
``@app.on_event("...")`` handlers, and SQLAlchemy ``@event.listens_for(Target, "...")`` listeners.
All of a handler's registrations (stacked decorators included) live in ``payload.registrations`` —
one artifact per handler, not per decorator, because ``artifact_id`` is content-addressed over the
grounding spans + extractor identity ([LOCKED], ``kb.ids``): two registrations of one handler share
the same evidence spans and would collide as separate artifacts. The handler is the grounded unit;
its registrations are payload facts (the entity-fields precedent).

Grounded on the handler span (role ``handler`` — the span includes its decorators), plus the
enclosing model class (role ``owner_class``, pydantic) and every resolved listened-to class (role
``target_class``, SQLAlchemy — resolved cross-file like the FastAPI ``response_model``).
``framework_versions`` folds only the frameworks the handler's registrations belong to.

Fully static: re-parses each handler span's source with tree-sitter; it never imports or executes
user code. Known gaps (documented, surfaced by the eval gate, never a silent wrong guess): the
call-form ``event.listen(Target, "name", fn)``, FastAPI lifespan context managers, pydantic v1
``@validator``/``@root_validator``, and dynamic event names (``@app.on_event(EVENT)`` is skipped —
the event name is load-bearing; a dynamic ``listens_for`` event or ``field_validator`` field is
kept but flagged in ``payload.limitations``). Detection is by decorator shape only (no data-flow):
any ``*.on_event("literal")`` matches, and pydantic validators are not verified against a
``BaseModel`` base — ``detection_signals`` keeps that honest.
"""

from __future__ import annotations

import textwrap
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tree_sitter_python as tsp
from tree_sitter import Language, Node, Parser

from kb.extract.base import DerivedEdge, ExtractContext, ExtractedArtifact
from kb.structural.interface import ParsedSpan

EXTRACTOR_ID = "events"
EXTRACTOR_VERSION = "1"

_LANGUAGE = Language(tsp.language())
_VERSIONED = ("pydantic", "fastapi", "sqlalchemy")
_FAMILY_FRAMEWORK = {
    "pydantic_field_validator": "pydantic",
    "pydantic_model_validator": "pydantic",
    "fastapi_on_event": "fastapi",
    "sqlalchemy_listens_for": "sqlalchemy",
}


@dataclass(frozen=True)
class _Registration:
    family: str
    event: str | None = None
    fields: list[str] = field(default_factory=list)
    mode: str | None = None
    target: str | None = None  # listens_for first positional, raw text
    decorator_object: str | None = None
    limitations: list[str] = field(default_factory=list)


class EventExtractor:
    extractor_id = EXTRACTOR_ID
    extractor_version = EXTRACTOR_VERSION

    def __init__(self) -> None:
        self._parser = Parser(_LANGUAGE)

    def extract(self, ctx: ExtractContext) -> list[ExtractedArtifact]:
        versions = _framework_versions(ctx, _VERSIONED)
        class_index = _index_class_spans(ctx)
        class_by_fq = {
            span.fq_symbol_path: span
            for spans in ctx.spans_by_module.values()
            for span in spans
            if span.span_kind == "class"
        }
        artifacts: list[ExtractedArtifact] = []
        for module, spans in ctx.spans_by_module.items():
            for span in spans:
                if span.span_kind not in ("function", "method"):
                    continue
                registrations = self._parse_registrations(span)
                if registrations:
                    artifacts.append(
                        self._build_artifact(
                            module, span, registrations, class_index, class_by_fq, versions
                        )
                    )
        return artifacts

    # --- registration parsing (static, re-parse the span) -------------------

    def _parse_registrations(self, span: ParsedSpan) -> list[_Registration]:
        root = self._parser.parse(textwrap.dedent(span.raw_text).encode("utf-8")).root_node
        deco_def = _first_child_of_type(root, "decorated_definition")
        if deco_def is None:
            return []
        out: list[_Registration] = []
        for decorator in deco_def.named_children:
            if decorator.type != "decorator":
                continue
            call = _first_child_of_type(decorator, "call")
            if call is None:  # bare decorators (@classmethod etc.) register nothing here
                continue
            callee = call.child_by_field_name("function")
            if callee is None:
                continue
            name = (_text(callee) or "").rsplit(".", 1)[-1]
            args = call.child_by_field_name("arguments")
            deco_object = (
                _text(callee.child_by_field_name("object")) if callee.type == "attribute" else None
            )
            if name in ("field_validator", "model_validator"):
                if span.span_kind != "method":
                    continue  # only a method inside a class registers a pydantic validator
                out.extend(self._pydantic_registration(name, args))
            elif name == "on_event" and callee.type == "attribute":
                reg = self._on_event_registration(args, deco_object)
                if reg is not None:
                    out.append(reg)
            elif name == "listens_for":
                reg = self._listens_for_registration(args, deco_object)
                if reg is not None:
                    out.append(reg)
        return out

    def _pydantic_registration(self, name: str, args: Node | None) -> list[_Registration]:
        mode_node = _keyword_value(args, "mode")
        mode = _string_literal_value(mode_node) if mode_node is not None else None
        limitations: list[str] = []
        if mode_node is not None and mode_node.type != "string":
            limitations.append("dynamic_mode")
            mode = None
        if name == "model_validator":
            return [_Registration(family="pydantic_model_validator", mode=mode,
                                  limitations=limitations)]
        fields, saw_non_literal = _positional_string_literals(args)
        if saw_non_literal:
            limitations.append("dynamic_field_names")
        return [_Registration(family="pydantic_field_validator", fields=fields, mode=mode,
                              limitations=limitations)]

    def _on_event_registration(
        self, args: Node | None, deco_object: str | None
    ) -> _Registration | None:
        event = _first_string_literal(args)
        if event is None:  # dynamic event name -> known gap, never a wrong guess
            return None
        return _Registration(family="fastapi_on_event", event=event,
                             decorator_object=deco_object)

    def _listens_for_registration(
        self, args: Node | None, deco_object: str | None
    ) -> _Registration | None:
        positional = _positional_args(args)
        if not positional:
            return None  # malformed registration
        target = _text(positional[0])
        limitations: list[str] = []
        event: str | None = None
        if len(positional) > 1 and positional[1].type == "string":
            event = _string_literal_value(positional[1])
        else:
            limitations.append("dynamic_event_name")
        return _Registration(family="sqlalchemy_listens_for", event=event, target=target,
                             decorator_object=deco_object, limitations=limitations)

    # --- artifact assembly ---------------------------------------------------

    def _build_artifact(
        self,
        module: str,
        span: ParsedSpan,
        registrations: list[_Registration],
        class_index: dict[str, list[tuple[str, ParsedSpan]]],
        class_by_fq: dict[str, ParsedSpan],
        versions: dict[str, str],
    ) -> ExtractedArtifact:
        grounding: dict[bytes, DerivedEdge] = {span.span_id: DerivedEdge(span.span_id, "handler")}
        limitations: list[str] = []

        owner_class: str | None = None
        if any(reg.family.startswith("pydantic_") for reg in registrations):
            owner_fq = span.fq_symbol_path.rsplit(".", 1)[0]
            owner_span = class_by_fq.get(owner_fq)
            if owner_span is not None:
                owner_class = owner_fq
                grounding.setdefault(
                    owner_span.span_id, DerivedEdge(owner_span.span_id, "owner_class")
                )

        reg_payloads: list[dict[str, Any]] = []
        for reg in registrations:
            limitations.extend(reg.limitations)
            target_matches: list[tuple[str, ParsedSpan]] = []
            if reg.family == "sqlalchemy_listens_for" and reg.target:
                target_matches = class_index.get(reg.target.rsplit(".", 1)[-1], [])
                for _tgt_module, tgt_span in target_matches:
                    grounding.setdefault(
                        tgt_span.span_id, DerivedEdge(tgt_span.span_id, "target_class")
                    )
                if not target_matches:
                    limitations.append("target_not_first_party")
            reg_payloads.append(
                {
                    "family": reg.family,
                    "event": reg.event,
                    "fields": reg.fields,
                    "mode": reg.mode,
                    "target": reg.target,
                    "target_resolved": bool(target_matches),
                    "target_ambiguous": len({s.fq_symbol_path for _, s in target_matches}) > 1,
                    "target_targets": [
                        {"module": m, "fq_symbol_path": s.fq_symbol_path}
                        for (m, s) in target_matches
                    ],
                    "decorator_object": reg.decorator_object,
                }
            )

        families = sorted({reg.family for reg in registrations})
        payload: dict[str, Any] = {
            "handler": span.fq_symbol_path,
            "handler_module": module,
            "owner_class": owner_class,
            "families": families,
            "registrations": reg_payloads,
            "detection_signals": [f"{family}_decorator" for family in families],
            "span_mapping": "exact",
            "limitations": limitations,
        }
        framework_versions = {
            _FAMILY_FRAMEWORK[family]: versions.get(_FAMILY_FRAMEWORK[family], "unknown")
            for family in families
        }
        return ExtractedArtifact(
            kind="event_handler",
            logical_key=f"event:{span.fq_symbol_path}",
            payload=payload,
            derived_from=list(grounding.values()),
            extractor_id=self.extractor_id,
            extractor_version=self.extractor_version,
            framework_versions=framework_versions,
        )


# --- module-level helpers (kept local; mirror the fastapi / entities extractors) ----


def _index_class_spans(ctx: ExtractContext) -> dict[str, list[tuple[str, ParsedSpan]]]:
    index: dict[str, list[tuple[str, ParsedSpan]]] = {}
    for module, spans in ctx.spans_by_module.items():
        for span in spans:
            if span.span_kind == "class":
                base = span.fq_symbol_path.rsplit(".", 1)[-1]
                index.setdefault(base, []).append((module, span))
    return index


def _positional_args(args: Node | None) -> list[Node]:
    if args is None:
        return []
    return [child for child in args.named_children if child.type != "keyword_argument"]


def _positional_string_literals(args: Node | None) -> tuple[list[str], bool]:
    """All positional string-literal values, plus whether a non-literal positional was seen."""
    literals: list[str] = []
    saw_non_literal = False
    for node in _positional_args(args):
        if node.type == "string":
            value = _string_literal_value(node)
            if value is not None:
                literals.append(value)
        else:
            saw_non_literal = True
    return literals, saw_non_literal


def _first_child_of_type(node: Node, type_name: str) -> Node | None:
    for child in node.named_children:
        if child.type == type_name:
            return child
    return None


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


def _string_literal_value(node: Node | None) -> str | None:
    if node is None or node.type != "string":
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
