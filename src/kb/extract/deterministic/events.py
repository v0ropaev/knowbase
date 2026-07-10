"""Deterministic event-handler extractor — pydantic / FastAPI / SQLAlchemy hooks (DESIGN.md §8).

Produces one ``event_handler`` artifact per HANDLER function/method that carries registrations —
decorator-form: pydantic ``@field_validator(...)`` / ``@model_validator(...)`` methods, FastAPI
``@app.on_event("...")`` handlers, SQLAlchemy ``@event.listens_for(Target, "...")`` listeners —
AND call-form: module-level SQLAlchemy ``event.listen(Target, "name", fn)`` statements (family
``sqlalchemy_listen``), where ``fn`` resolves to a first-party top-level function of the same
module or an imported one (the calls.py import-table machinery). ALL of a handler's registrations
(stacked decorators and call sites included, possibly from OTHER modules) live in
``payload.registrations`` — one artifact per handler: the handler is the grounded unit and its
registrations are payload facts (the entity-fields precedent).

Grounded on the handler span (role ``handler``), plus the enclosing model class (role
``owner_class``, pydantic), every resolved listened-to class (role ``target_class``, cross-file),
and — for call-form registrations — the registering file's module span (role ``registration_site``:
there is no statement-level span, and any edit to that file must re-extract the handler, which the
module span's identity guarantees). Only MODULE-LEVEL ``listen(...)`` calls are extracted: they run
deterministically at import time, so "this handler is registered" holds at confidence 1.0; a call
inside a function/class body is conditional and stays a documented gap. A top-level call under an
``if`` is extracted as unconditional (the calls.py precedent).

Fully static: re-parses span sources with tree-sitter; it never imports or executes user code.
The bare ``from sqlalchemy.event import listen [as alias]`` form IS accepted when the import
table proves the called name is ``sqlalchemy.event.listen``. Known gaps (documented, surfaced by
the eval gate, never a silent wrong guess): ``listen(...)`` inside a function/class body, a
lambda/attribute/non-first-party ``fn`` (skipped — handler identity is load-bearing), FastAPI
lifespan context managers, pydantic v1 ``@validator``/``@root_validator``, and dynamic event names
(``@app.on_event(EVENT)`` is skipped; a dynamic ``listens_for``/``listen`` event or
``field_validator`` field is kept but flagged in ``payload.limitations``). Detection is by shape
only (no data-flow); ``detection_signals`` keeps that honest.
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
from kb.extract.deterministic.calls import _caller_scan_root, _import_table, _iter_calls
from kb.structural.interface import ParsedSpan

EXTRACTOR_ID = "events"
EXTRACTOR_VERSION = "3"  # v3: bare `from sqlalchemy.event import listen` call-form accepted

_LANGUAGE = Language(tsp.language())
_VERSIONED = ("pydantic", "fastapi", "sqlalchemy")
_FAMILY_FRAMEWORK = {
    "pydantic_field_validator": "pydantic",
    "pydantic_model_validator": "pydantic",
    "fastapi_on_event": "fastapi",
    "sqlalchemy_listens_for": "sqlalchemy",
    "sqlalchemy_listen": "sqlalchemy",
}
_SQLALCHEMY_FAMILIES = ("sqlalchemy_listens_for", "sqlalchemy_listen")


@dataclass(frozen=True)
class _Registration:
    family: str
    event: str | None = None
    fields: list[str] = field(default_factory=list)
    mode: str | None = None
    target: str | None = None  # listens_for/listen first positional, raw text
    decorator_object: str | None = None
    limitations: list[str] = field(default_factory=list)
    form: str = "decorator"  # "decorator" | "call"
    registration_module: str | None = None  # call-form: the module carrying the listen() statement
    registration_line: int | None = None  # call-form: 1-based line of the listen() call
    site_span_id: bytes | None = None  # call-form: the registering file's module span


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
        fn_index: dict[tuple[str, str], ParsedSpan] = {}  # top-level functions, per module
        for module, spans in ctx.spans_by_module.items():
            for span in spans:
                parent, _, name = span.fq_symbol_path.rpartition(".")
                if span.span_kind == "function" and parent == module:
                    fn_index[(module, name)] = span

        # Pass A — decorator registrations, attached to the decorated handler itself.
        entries: dict[str, tuple[str, ParsedSpan, list[_Registration]]] = {}
        for module, spans in ctx.spans_by_module.items():
            for span in spans:
                if span.span_kind not in ("function", "method"):
                    continue
                registrations = self._parse_registrations(span)
                if registrations:
                    entries[span.fq_symbol_path] = (module, span, registrations)

        # Pass B — module-level call-form registrations, attached to the RESOLVED handler
        # (which may live in another module). Sorted before merging: dict order differs between
        # full and incremental indexing, and the payload must be byte-identical across both.
        call_regs = self._call_form_registrations(ctx, fn_index)
        call_regs.sort(
            key=lambda item: (
                item[2].registration_module or "",
                item[2].registration_line or 0,
                item[2].event or "",
                item[1].fq_symbol_path,
            )
        )
        for handler_module, handler_span, registration in call_regs:
            entry = entries.get(handler_span.fq_symbol_path)
            if entry is None:
                entries[handler_span.fq_symbol_path] = (
                    handler_module, handler_span, [registration]
                )
            else:
                entry[2].append(registration)

        return [
            self._build_artifact(module, span, registrations, class_index, class_by_fq, versions)
            for _fq, (module, span, registrations) in sorted(entries.items())
        ]

    # --- call-form scan (module-level event.listen(Target, "event", fn)) -----

    def _call_form_registrations(
        self, ctx: ExtractContext, fn_index: dict[tuple[str, str], ParsedSpan]
    ) -> list[tuple[str, ParsedSpan, _Registration]]:
        """``(handler_module, handler_span, registration)`` per resolved module-level listen()."""
        module_set = set(ctx.spans_by_module)
        out: list[tuple[str, ParsedSpan, _Registration]] = []
        for module, spans in ctx.spans_by_module.items():
            module_span = next((s for s in spans if s.span_kind == "module"), None)
            if module_span is None:
                continue
            scan_root = _caller_scan_root(self._parser, module_span)
            if scan_root is None:
                continue
            table = None  # import table only if the module actually has a listen() candidate
            for call in _iter_calls(scan_root):
                callee = call.child_by_field_name("function")
                if callee is None:
                    continue
                if callee.type == "attribute":
                    if _text(callee.child_by_field_name("attribute")) != "listen":
                        continue
                elif callee.type == "identifier":
                    # bare `from sqlalchemy.event import listen [as l]; l(...)`: accept ONLY a
                    # name the import table proves to BE sqlalchemy.event.listen — precision
                    # first, any other bare `listen` (a local def, another library) is skipped
                    if table is None:
                        table = _import_table(self._parser, ctx, module, module_set)
                    if table.symbols.get(_text(callee) or "") != ("sqlalchemy.event", "listen"):
                        continue
                else:
                    continue
                positional = _positional_args(call.child_by_field_name("arguments"))
                if len(positional) < 3:
                    continue  # not a registration shape (e.g. sock.listen(5))
                handler_node = positional[2]
                if handler_node.type != "identifier":
                    continue  # lambda / attribute fn -> documented gap (identity load-bearing)
                handler_name = _text(handler_node) or ""
                handler_module, handler_span = module, fn_index.get((module, handler_name))
                if handler_span is None:
                    if table is None:
                        table = _import_table(self._parser, ctx, module, module_set)
                    bound = table.symbols.get(handler_name)
                    if bound is None:
                        continue  # unresolvable fn -> documented gap, never a wrong guess
                    handler_span = fn_index.get(bound)
                    handler_module = bound[0]
                if handler_span is None:
                    continue
                limitations: list[str] = []
                event: str | None = None
                if positional[1].type == "string":
                    event = _string_literal_value(positional[1])
                else:
                    limitations.append("dynamic_event_name")
                out.append(
                    (
                        handler_module,
                        handler_span,
                        _Registration(
                            family="sqlalchemy_listen",
                            event=event,
                            target=_text(positional[0]),
                            limitations=limitations,
                            form="call",
                            registration_module=module,
                            registration_line=module_span.start_line + call.start_point[0],
                            site_span_id=module_span.span_id,
                        ),
                    )
                )
        return out

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
            if reg.family in _SQLALCHEMY_FAMILIES and reg.target:
                target_matches = class_index.get(reg.target.rsplit(".", 1)[-1], [])
                for _tgt_module, tgt_span in target_matches:
                    grounding.setdefault(
                        tgt_span.span_id, DerivedEdge(tgt_span.span_id, "target_class")
                    )
                if not target_matches:
                    limitations.append("target_not_first_party")
            if reg.site_span_id is not None:
                grounding.setdefault(
                    reg.site_span_id, DerivedEdge(reg.site_span_id, "registration_site")
                )
            reg_payloads.append(
                {
                    "family": reg.family,
                    "form": reg.form,
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
                    "registration_module": reg.registration_module,
                    "registration_line": reg.registration_line,
                }
            )

        families = sorted({reg.family for reg in registrations})
        payload: dict[str, Any] = {
            "handler": span.fq_symbol_path,
            "handler_module": module,
            "owner_class": owner_class,
            "families": families,
            "registrations": reg_payloads,
            "detection_signals": sorted(
                {f"{reg.family}_{'call' if reg.form == 'call' else 'decorator'}"
                 for reg in registrations}
            ),
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
