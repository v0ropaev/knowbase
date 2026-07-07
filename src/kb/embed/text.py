"""The text fed to the embedder for each artifact (frozen — changing it changes rankings).

Reuses the MCP ``summarize`` headline and appends key payload fields so a natural-language query has
discriminative tokens to match.
"""

from __future__ import annotations

from typing import Any

from kb.mcp.records import summarize


def embed_text(kind: str, payload: dict[str, Any]) -> str:
    head = summarize(kind, payload)
    if kind == "api_route":
        parts = [
            head,
            f"path {payload.get('path', '')}",
            f"method {payload.get('method', '')}",
            f"response model {payload.get('response_model_base') or ''}",
            f"handler {payload.get('handler', '')}",
            " ".join(str(p.get("name", "")) for p in payload.get("params", [])),
        ]
        return " ".join(p for p in parts if p.strip())
    if kind == "import_edge":
        return f"{head} import {payload.get('importer', '')} {payload.get('imported', '')}"
    if kind == "entity":
        parts = [
            head,
            f"entity {payload.get('qualified_name', '')}",
            f"framework {payload.get('framework', '')}",
            "fields " + " ".join(str(f.get("name", "")) for f in payload.get("fields", [])),
            "related "
            + " ".join(str(r.get("name", "")) for r in payload.get("related_entities", [])),
        ]
        return " ".join(p for p in parts if p.strip())
    if kind == "public_symbol":
        parts = [
            head,
            f"public {payload.get('public_qualified_name', '')}",
            f"name {payload.get('name', '')}",
            f"kind {payload.get('symbol_kind') or ''}",
            f"defined in {payload.get('defining_module') or ''}",
            f"signature {payload.get('signature') or ''}",
        ]
        return " ".join(p for p in parts if p.strip())
    if kind == "event_handler":
        regs = payload.get("registrations", [])
        parts = [
            head,
            "families " + " ".join(str(f) for f in payload.get("families", [])),
            "events " + " ".join(str(r.get("event") or "") for r in regs),
            "targets " + " ".join(str(r.get("target") or "") for r in regs),
            f"owner {payload.get('owner_class') or ''}",
            f"handler {payload.get('handler', '')}",
            "fields " + " ".join(str(f) for r in regs for f in r.get("fields", [])),
        ]
        return " ".join(p for p in parts if p.strip())
    if kind == "process_path":
        sink = payload.get("sink", {})
        parts = [
            head,
            f"process {payload.get('entrypoint', '')}",
            f"route {payload.get('entrypoint_reference', '')}",
            "steps " + " ".join(str(s) for s in payload.get("steps", [])),
            f"sink {sink.get('name', '')} "
            + " ".join(str(m.get("text", "")) for m in sink.get("matches", [])),
            f"terminal {payload.get('terminal', '')}",
        ]
        return " ".join(p for p in parts if p.strip())
    if kind == "call_edge":
        parts = [
            head,
            f"call {payload.get('caller', '')} {payload.get('callee', '')}",
            f"caller module {payload.get('caller_module', '')}",
            f"callee module {payload.get('callee_module', '')}",
            f"resolution {payload.get('resolution', '')}",
            f"callee kind {payload.get('callee_kind', '')}",
        ]
        return " ".join(p for p in parts if p.strip())
    if kind == "description":
        claims = " ".join(str(c.get("text", "")) for c in payload.get("claims", []))
        return f"{payload.get('summary', '')} {claims}".strip() or head
    return head
