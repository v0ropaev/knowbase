"""[LOCKED] ``structural_fingerprint`` — the canonical span serializer (DESIGN.md §5).

A span's fingerprint is the sha256 of a normalized S-expression of its tree-sitter subtree:

1. Only **named** nodes are serialized — anonymous punctuation/keyword nodes are dropped, so
   whitespace, formatting and layout are invisible to identity.
2. Semantic leaf nodes (identifier, dotted_name, string, integer, float) contribute their
   normalized token, so renames and literal-value changes DO change identity.
3. **Comments and docstrings are excluded** (resolved decision): a comment node is skipped, and a
   leading bare string-expression statement in a module/class/function body is dropped. A
   docstring-only edit therefore does not change ``span_id`` (no invalidation, dedup preserved).

Any change to these rules MUST bump ``kb.ids.NORMALIZATION_VERSION``.
"""

from __future__ import annotations

import hashlib

from tree_sitter import Node

_SEMANTIC_LEAVES = frozenset({"identifier", "dotted_name", "string", "integer", "float"})
_BODY_TYPES = frozenset({"module", "block"})


def structural_fingerprint(node: Node) -> bytes:
    """32-byte sha256 digest of the node's normalized S-expression."""
    return hashlib.sha256(_sexpr(node).encode("utf-8")).digest()


def _sexpr(node: Node) -> str:
    node_type = node.type
    if node_type in _SEMANTIC_LEAVES:
        return f"({node_type} {_normalize_token(node)})"

    is_body = node_type in _BODY_TYPES
    first_real = True
    parts: list[str] = []
    for child in node.named_children:
        if child.type == "comment":
            continue
        if is_body and first_real:
            first_real = False
            if _is_docstring_stmt(child):
                continue
        parts.append(_sexpr(child))

    if parts:
        return f"({node_type} {' '.join(parts)})"
    return f"({node_type})"


def _is_docstring_stmt(node: Node) -> bool:
    """A bare string expression statement, e.g. the first statement of a module/class/def body."""
    if node.type != "expression_statement":
        return False
    named = node.named_children
    return len(named) == 1 and named[0].type == "string"


def _normalize_token(node: Node) -> str:
    node_type = node.type
    text = (node.text or b"").decode("utf-8", errors="replace")
    if node_type in ("identifier", "dotted_name"):
        return text
    if node_type == "string":
        return _string_value(node)
    if node_type == "integer":
        try:
            return str(int(text, 0))  # canonicalize bases/underscores: 0x10, 16, 1_6 -> "16"
        except ValueError:
            return text
    if node_type == "float":
        try:
            return repr(float(text))
        except ValueError:
            return text
    return text


def _string_value(node: Node) -> str:
    """The decoded content of a string literal with quote style/prefix dropped. Concatenates
    ``string_content`` children (ignoring quotes/prefixes); falls back to raw text when empty."""
    contents = [
        (child.text or b"").decode("utf-8", errors="replace")
        for child in node.named_children
        if child.type == "string_content"
    ]
    if contents:
        return "".join(contents)
    return (node.text or b"").decode("utf-8", errors="replace")
