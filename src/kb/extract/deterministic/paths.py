"""Deterministic business-process path extractor (DESIGN.md §4, §9, §14 item 2 — step 2).

Materializes **named real paths**: an ENTRYPOINT (an already-extracted ``api_route`` /
``event_handler`` handler) → a shortest chain of resolved first-party ``call_edge``s → a TERMINAL
function containing a SINK call (a registry match). One ``process_path`` artifact per
``(entrypoint, sink_name, terminal)``, grounded on EVERY def span along the path (multi-file
provenance). This is a SECOND-ORDER extractor: it consumes ``ctx.prior_artifacts`` and must be
registered AFTER the fastapi/events/calls extractors (see ``kb.daemon.watch.default_extractors``).

The DESIGN §9 hard floor holds BY CONSTRUCTION: every sink claim IS a registry match on the
materialized path, and every endpoint IS an extracted entrypoint. ``confidence`` is 1.0 — a found
path is machine-checked exact (the ``call_edge`` precedent); incompleteness (paths missed by the
bounded-recall call graph or textual sink matching) is a payload/doc fact, priced into the FUTURE
LLM-labeled artifact, not this one. The effective sink registry's digest is folded into
``framework_versions`` so a registry edit can never silently serve a stale payload (identity-v2
discipline).

Sinks are typically third-party calls, which the precision-first ``call_edge`` extractor skips —
so sink detection is a separate textual scan of call sites against the registry, reusing the exact
call-attribution machinery of ``kb.extract.deterministic.calls`` (body-only scan roots, no-descend
traversal), so edges and sinks always agree about which function contains a call.

Registry: built-in defaults below, plus an optional ``.kb/sinks.yaml`` at the ANALYZED repo's root
(travels with the commit; materialized by the pipeline). Pattern grammar is deliberately minimal:
exact dotted ``A.B``, prefix ``P.*``, suffix ``*.NAME`` (attribute-name match — works even when the
receiver is dynamic). Documented gaps: alias-renamed receivers (``import requests as rq``),
mid-segment globs, argument inspection, class-instantiation edges (``__init__`` side effects are
invisible), sink-bearing intermediates shadow deeper terminals (paths terminate at the first sink).
A malformed override raises ``ValueError`` loudly — never silently ignored.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tree_sitter_python as tsp
import yaml
from tree_sitter import Language, Parser

from kb.extract.base import DerivedEdge, ExtractContext, ExtractedArtifact
from kb.extract.deterministic.calls import (
    _caller_scan_root,
    _dotted_object_text,
    _iter_calls,
    _text,
)
from kb.structural.interface import ParsedSpan

EXTRACTOR_ID = "process_paths"
EXTRACTOR_VERSION = "1"
SINKS_FILE = ".kb/sinks.yaml"
DEFAULT_MAX_DEPTH = 8
DEFAULT_MAX_PATHS_PER_ENTRYPOINT = 32

_LANGUAGE = Language(tsp.language())


@dataclass(frozen=True)
class SinkRule:
    name: str
    patterns: tuple[str, ...]  # exact "A.B" | prefix "P.*" | suffix "*.NAME"


# Curation rule: no collision-prone names (*.add, *.write, *.flush ...) — precision-first;
# repos opt into project-specific sinks via the .kb/sinks.yaml override.
_BUILTIN_SINKS: tuple[SinkRule, ...] = (
    SinkRule("db_write", ("*.commit", "*.execute", "*.executemany", "*.bulk_save_objects")),
    SinkRule("http_call", ("requests.*", "httpx.*", "urllib.request.*", "aiohttp.*")),
    SinkRule("email_send", ("smtplib.*", "*.send_message", "*.sendmail")),
    SinkRule("subprocess_exec", ("subprocess.*", "os.system", "os.popen")),
    SinkRule("file_write", ("*.write_text", "*.write_bytes", "shutil.*", "os.remove")),
    SinkRule("queue_publish", ("*.publish", "*.apply_async", "*.send_task", "*.basic_publish")),
)


@dataclass(frozen=True)
class _SinkMatch:
    text: str  # matched dotted callee text; "?.<attr>" when the receiver is dynamic
    pattern: str
    line: int  # absolute 1-based


@dataclass(frozen=True)
class _Entrypoint:
    fq: str
    kind: str  # "api_route" | "event_handler"
    reference: str
    references: tuple[tuple[str, str], ...]  # all (kind, reference) pairs for this fq


@dataclass(frozen=True)
class _FoundPath:
    entrypoint: _Entrypoint
    steps: tuple[str, ...]
    resolutions: tuple[str, ...]
    sink_name: str
    matches: tuple[_SinkMatch, ...]
    limitations: tuple[str, ...]


class PathEngine:
    """Deterministic shortest-path slicer over first-party call edges (the DESIGN §11 seam)."""

    def __init__(
        self,
        adjacency: Mapping[str, Sequence[tuple[str, str]]],
        sinks_by_fq: Mapping[str, Mapping[str, Sequence[_SinkMatch]]],
        *,
        max_depth: int,
        max_paths_per_entrypoint: int,
    ) -> None:
        self._adjacency = adjacency
        self._sinks_by_fq = sinks_by_fq
        self._max_depth = max_depth
        self._max_paths = max_paths_per_entrypoint

    def paths_from(self, entrypoint: _Entrypoint) -> list[_FoundPath]:
        """BFS with visited-on-enqueue: cycle-safe, O(V+E), first arrival = a shortest path;
        sorted adjacency + FIFO order make ties deterministic (lexicographically earliest)."""
        found: list[_FoundPath] = []
        limitations: set[str] = set()
        visited = {entrypoint.fq}
        queue: deque[tuple[str, tuple[str, ...], tuple[str, ...]]] = deque(
            [(entrypoint.fq, (entrypoint.fq,), ())]
        )
        while queue:
            fq, steps, resolutions = queue.popleft()
            sink_names = self._sinks_by_fq.get(fq)
            if sink_names:
                for sink_name in sorted(sink_names):
                    if len(found) >= self._max_paths:
                        limitations.add("paths_capped")
                        break
                    found.append(
                        _FoundPath(
                            entrypoint=entrypoint,
                            steps=steps,
                            resolutions=resolutions,
                            sink_name=sink_name,
                            matches=tuple(
                                sorted(sink_names[sink_name], key=lambda m: m.line)
                            ),
                            limitations=(),
                        )
                    )
                continue  # a path TERMINATES at its first sink-bearing node
            if len(steps) - 1 >= self._max_depth:
                limitations.add("depth_capped")
                continue
            for callee, resolution in self._adjacency.get(fq, ()):
                if callee not in visited:
                    visited.add(callee)
                    queue.append((callee, (*steps, callee), (*resolutions, resolution)))
        if limitations:  # retro-tag every emitted path — no silent caps
            found = [
                _FoundPath(
                    p.entrypoint, p.steps, p.resolutions, p.sink_name, p.matches,
                    tuple(sorted(limitations)),
                )
                for p in found
            ]
        return found


class ProcessPathExtractor:
    extractor_id = EXTRACTOR_ID
    extractor_version = EXTRACTOR_VERSION

    def __init__(
        self,
        *,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_paths_per_entrypoint: int = DEFAULT_MAX_PATHS_PER_ENTRYPOINT,
    ) -> None:
        self._parser = Parser(_LANGUAGE)
        self._max_depth = max_depth
        self._max_paths = max_paths_per_entrypoint

    def extract(self, ctx: ExtractContext) -> list[ExtractedArtifact]:
        rules = _load_registry(ctx.materialized_root)
        digest = _registry_digest(rules)
        entrypoints = _entrypoints(ctx.prior_artifacts)
        if not entrypoints:
            return []
        adjacency = _adjacency(ctx.prior_artifacts)
        span_index = _def_span_index(ctx)
        sinks_by_fq = self._scan_sinks(ctx, rules)
        engine = PathEngine(
            adjacency, sinks_by_fq,
            max_depth=self._max_depth, max_paths_per_entrypoint=self._max_paths,
        )
        artifacts: list[ExtractedArtifact] = []
        for entrypoint in entrypoints:
            for path in engine.paths_from(entrypoint):
                art = self._build_artifact(path, span_index, digest)
                if art is not None:
                    artifacts.append(art)
        return artifacts

    def _scan_sinks(
        self, ctx: ExtractContext, rules: Sequence[SinkRule]
    ) -> dict[str, dict[str, list[_SinkMatch]]]:
        out: dict[str, dict[str, list[_SinkMatch]]] = {}
        for spans in ctx.spans_by_module.values():
            for span in spans:
                if span.span_kind not in ("function", "method"):
                    continue
                root = _caller_scan_root(self._parser, span)
                if root is None:
                    continue
                for call in _iter_calls(root):
                    fn = call.child_by_field_name("function")
                    if fn is None:
                        continue
                    dotted: str | None = None
                    attr: str | None = None
                    if fn.type == "identifier":
                        dotted = _text(fn)
                    elif fn.type == "attribute":
                        attr = _text(fn.child_by_field_name("attribute"))
                        obj_node = fn.child_by_field_name("object")
                        obj = _dotted_object_text(obj_node) if obj_node is not None else None
                        dotted = f"{obj}.{attr}" if obj and attr else None
                    else:
                        continue  # call-of-call etc. -> documented gap
                    for sink_name, pattern, text in _match_call(rules, dotted, attr):
                        line = span.start_line + call.start_point[0]
                        out.setdefault(span.fq_symbol_path, {}).setdefault(
                            sink_name, []
                        ).append(_SinkMatch(text=text, pattern=pattern, line=line))
        return out

    def _build_artifact(
        self, path: _FoundPath, span_index: dict[str, ParsedSpan], digest: str
    ) -> ExtractedArtifact | None:
        spans = [span_index.get(fq) for fq in path.steps]
        if any(s is None for s in spans):  # cannot happen within one snapshot; guard DB drift
            return None
        grounding: dict[bytes, DerivedEdge] = {}
        for i, span in enumerate(spans):
            assert span is not None
            role = "entrypoint" if i == 0 else ("terminal" if i == len(spans) - 1 else "step")
            grounding.setdefault(span.span_id, DerivedEdge(span.span_id, role))
        ep = path.entrypoint
        payload: dict[str, Any] = {
            "entrypoint": ep.fq,
            "entrypoint_kind": ep.kind,
            "entrypoint_reference": ep.reference,
            "entrypoint_references": [list(r) for r in ep.references],
            "steps": list(path.steps),
            "edges": [
                {"caller": a, "callee": b, "resolution": r}
                for (a, b), r in zip(
                    zip(path.steps, path.steps[1:], strict=False),
                    path.resolutions, strict=True,
                )
            ],
            "terminal": path.steps[-1],
            "sink": {
                "name": path.sink_name,
                "matches": [
                    {"text": m.text, "pattern": m.pattern, "line": m.line}
                    for m in path.matches
                ],
            },
            "depth": len(path.steps) - 1,
            "span_mapping": "exact",
            "completeness": (
                "paths found are exact; missing paths possible "
                "(call-edge recall bounds, textual sink matching)"
            ),
            "limitations": list(path.limitations),
        }
        return ExtractedArtifact(
            kind="process_path",
            logical_key=f"process:{ep.fq}->{path.sink_name}@{path.steps[-1]}",
            payload=payload,
            derived_from=list(grounding.values()),
            extractor_id=self.extractor_id,
            extractor_version=self.extractor_version,
            framework_versions={"sink_registry": digest},
        )


# --- registry ----------------------------------------------------------------


def _load_registry(materialized_root: str) -> tuple[SinkRule, ...]:
    path = Path(materialized_root) / SINKS_FILE
    if not path.exists():
        return _BUILTIN_SINKS
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError(f"{SINKS_FILE}: expected a mapping with version: 1")
    extend = data.get("extend", True)
    if not isinstance(extend, bool):
        raise ValueError(f"{SINKS_FILE}: 'extend' must be a boolean")
    overrides: list[SinkRule] = []
    for item in data.get("sinks", []) or []:
        if not isinstance(item, dict):
            raise ValueError(f"{SINKS_FILE}: each sink must be a mapping, got {item!r}")
        name, patterns = item.get("name"), item.get("patterns")
        if not isinstance(name, str) or not name.replace("_", "a").isalnum():
            raise ValueError(f"{SINKS_FILE}: bad sink name {name!r}")
        if not isinstance(patterns, list) or not patterns:
            raise ValueError(f"{SINKS_FILE}: sink {name!r} needs a non-empty patterns list")
        for pat in patterns:
            if not isinstance(pat, str) or pat.count("*") > 1 or pat in ("*", ""):
                raise ValueError(f"{SINKS_FILE}: bad pattern {pat!r} in sink {name!r}")
        overrides.append(SinkRule(name, tuple(patterns)))
    by_name = {r.name: r for r in _BUILTIN_SINKS} if extend else {}
    for rule in overrides:
        by_name[rule.name] = rule  # same-name override REPLACES the built-in (predictable)
    return tuple(sorted(by_name.values(), key=lambda r: r.name))


def _registry_digest(rules: Sequence[SinkRule]) -> str:
    canonical = json.dumps(
        [{"name": r.name, "patterns": list(r.patterns)} for r in rules],
        separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def _match_call(
    rules: Sequence[SinkRule], dotted: str | None, attr: str | None
) -> list[tuple[str, str, str]]:
    hits: list[tuple[str, str, str]] = []
    for rule in rules:
        for pattern in rule.patterns:
            if pattern.startswith("*."):
                if attr is not None and attr == pattern[2:]:
                    hits.append((rule.name, pattern, dotted or f"?.{attr}"))
            elif pattern.endswith(".*"):
                prefix = pattern[:-2]
                if dotted is not None and (dotted == prefix or dotted.startswith(prefix + ".")):
                    hits.append((rule.name, pattern, dotted))
            elif dotted is not None and dotted == pattern:
                hits.append((rule.name, pattern, dotted))
    return hits


# --- prior-artifact readers ----------------------------------------------------


def _entrypoints(prior: Sequence[ExtractedArtifact]) -> list[_Entrypoint]:
    refs_by_fq: dict[str, list[tuple[str, str]]] = {}
    for art in prior:
        if art.kind == "api_route":
            fq = str(art.payload["handler"])
            ref = f"{art.payload.get('method', '?')} {art.payload.get('path', '?')}"
            refs_by_fq.setdefault(fq, []).append(("api_route", ref))
        elif art.kind == "event_handler":
            fq = str(art.payload["handler"])
            regs = art.payload.get("registrations", [])
            ref = ";".join(
                f"{r.get('family', '?')}:{r.get('event')}" if r.get("event")
                else str(r.get("family", "?"))
                for r in regs
            )
            refs_by_fq.setdefault(fq, []).append(("event_handler", ref))
    out: list[_Entrypoint] = []
    for fq in sorted(refs_by_fq):
        pairs = tuple(sorted(set(refs_by_fq[fq])))
        out.append(_Entrypoint(fq=fq, kind=pairs[0][0], reference=pairs[0][1], references=pairs))
    return out


def _adjacency(prior: Sequence[ExtractedArtifact]) -> dict[str, list[tuple[str, str]]]:
    adjacency: dict[str, list[tuple[str, str]]] = {}
    for art in prior:
        if art.kind != "call_edge":
            continue
        # class-instantiation edges dead-end (the __init__ body is invisible) -> excluded
        if art.payload.get("callee_kind") not in ("function", "method"):
            continue
        adjacency.setdefault(str(art.payload["caller"]), []).append(
            (str(art.payload["callee"]), str(art.payload.get("resolution", "?")))
        )
    for callees in adjacency.values():
        callees.sort()  # deterministic BFS tie-break
    return adjacency


def _def_span_index(ctx: ExtractContext) -> dict[str, ParsedSpan]:
    index: dict[str, ParsedSpan] = {}
    for spans in ctx.spans_by_module.values():
        for span in spans:
            if span.span_kind in ("function", "method"):
                index[span.fq_symbol_path] = span  # last-wins, the calls.py precedent
    return index
