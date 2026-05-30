"""Canonicalize routes from an OpenAPI doc and from ``api_route`` artifacts to a comparable shape.

Both sides project to a ``frozenset`` of ``CanonRoute`` (order-insensitive); ``$ref``s are resolved
to schema names so ``response_model=List[OrderOut]`` (artifact) and the array-of-``$ref`` (oracle)
compare equal. Documented oracle blind spots (``include_in_schema=False``) are dropped on the
artifact side so the comparison is apples-to-apples.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "trace"})


@dataclass(frozen=True)
class CanonRoute:
    method: str
    path: str
    response_schema: str | None
    params: frozenset[tuple[str, str]]  # (name, location) for path/query only
    body_schema: str | None


def _schema_name(schema: Any) -> str | None:
    """Resolve a schema node to its model name (follows $ref, arrays, and Optional unions)."""
    if not isinstance(schema, Mapping):
        return None
    ref = schema.get("$ref")
    if isinstance(ref, str):
        return ref.rsplit("/", 1)[-1]
    if schema.get("type") == "array":
        return _schema_name(schema.get("items", {}))
    for combinator in ("anyOf", "allOf", "oneOf"):
        for sub in schema.get(combinator, []):
            name = _schema_name(sub)
            if name is not None:
                return name
    return None


def _success_response(operation: Mapping[str, Any]) -> Mapping[str, Any]:
    responses = operation.get("responses", {})
    for code in sorted(responses):
        if code.startswith("2"):
            value = responses[code]
            return value if isinstance(value, Mapping) else {}
    return {}


def _json_schema(container: Mapping[str, Any]) -> Any:
    return container.get("content", {}).get("application/json", {}).get("schema", {})


def canonical_routes_from_openapi(doc: Mapping[str, Any]) -> set[CanonRoute]:
    routes: set[CanonRoute] = set()
    for path, methods in doc.get("paths", {}).items():
        for method, operation in methods.items():
            if method.lower() not in _HTTP_METHODS or not isinstance(operation, Mapping):
                continue
            params = frozenset(
                (p["name"], p["in"])
                for p in operation.get("parameters", [])
                if p.get("in") in ("path", "query")
            )
            response_schema = _schema_name(_json_schema(_success_response(operation)))
            body_schema = _schema_name(_json_schema(operation.get("requestBody", {})))
            routes.add(CanonRoute(method.upper(), path, response_schema, params, body_schema))
    return routes


def canonical_routes_from_artifacts(payloads: Iterable[Mapping[str, Any]]) -> set[CanonRoute]:
    routes: set[CanonRoute] = set()
    for payload in payloads:
        if payload.get("include_in_schema") is False:
            continue  # documented oracle blind spot — present statically, absent from the oracle
        params = frozenset(
            (p["name"], p["in"])
            for p in payload.get("params", [])
            if p.get("in") in ("path", "query")
        )
        body_schema = next(
            (p["annotation"] for p in payload.get("params", []) if p.get("in") == "body"), None
        )
        routes.add(
            CanonRoute(
                payload["method"],
                payload["path"],
                payload.get("response_model_base"),
                params,
                body_schema,
            )
        )
    return routes
