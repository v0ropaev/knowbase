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
    return head
