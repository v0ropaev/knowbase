"""The structural-layer interface: ``ParsedSpan`` (identity + per-SHA location) and the
``StructuralIndex`` protocol (the SCIP seam, DESIGN.md §11)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ParsedSpan:
    """A code span: its content-addressed identity PLUS its location at one parse.

    Identity (``span_kind``, ``fq_symbol_path``, ``structural_fingerprint``, ``span_id``) is
    location-free and path-free (DESIGN.md §5). The location fields below are per-SHA — they are
    written to ``span_occurrence`` and re-resolved on every commit, never to ``code_span``.
    Lines are 1-based (human/provenance-facing); bytes are 0-based offsets into the source.
    """

    span_kind: str  # module | class | function | method | import
    fq_symbol_path: str
    structural_fingerprint: bytes
    span_id: bytes
    lang: str
    # --- location (per-SHA; not part of identity) ---
    start_byte: int
    end_byte: int
    start_line: int
    end_line: int
    raw_text: str


@dataclass(frozen=True)
class ParseResult:
    """Spans for one file plus a gap signal. ``has_error`` => the parse hit a syntax error; the
    caller records an explicit gap rather than silently dropping the file (DESIGN.md §13)."""

    spans: list[ParsedSpan]
    has_error: bool


class StructuralIndex(Protocol):
    """Produces content-addressed spans for a single source file. ``module_fqname`` is supplied by
    the caller (derived from the path) so identity never depends on the raw file path."""

    def parse_file(self, module_fqname: str, source: bytes) -> ParseResult: ...
