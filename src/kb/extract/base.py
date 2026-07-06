"""Extractor contracts: a grounded ``ExtractedArtifact`` and the ``Extractor`` protocol.

Every artifact carries the spans it was derived from (>=1, enforced everywhere) and computes its
own content-addressed id from those spans + extractor/prompt/model/framework identity (DESIGN.md
§6).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from kb.ids import compute_artifact_id
from kb.structural.interface import ParsedSpan


@dataclass(frozen=True)
class DerivedEdge:
    """A grounding edge: the span an artifact was derived from, with an optional role label."""

    span_id: bytes
    role: str | None = None


@dataclass(frozen=True)
class ExtractedArtifact:
    """A knowledge artifact produced by an extractor, before it is written to the store."""

    kind: str
    logical_key: str
    payload: dict[str, Any]
    derived_from: list[DerivedEdge]
    extractor_id: str
    extractor_version: str
    framework_versions: dict[str, str] = field(default_factory=dict)
    prompt_version: str = ""
    model_id: str = ""
    is_deterministic: bool = True
    confidence: float = 1.0

    def artifact_id(self) -> bytes:
        """The content-addressed id (raises if there are no derived-from spans)."""
        return compute_artifact_id(
            derived_from_span_ids=[edge.span_id for edge in self.derived_from],
            logical_key=self.logical_key,
            extractor_id=self.extractor_id,
            extractor_version=self.extractor_version,
            prompt_version=self.prompt_version,
            model_id=self.model_id,
            framework_versions=self.framework_versions,
        )


class Extractor(Protocol):
    """Turns the spans of a file (plus context) into grounded artifacts."""

    extractor_id: str
    extractor_version: str

    def extract(self, ctx: ExtractContext) -> list[ExtractedArtifact]: ...


@dataclass(frozen=True)
class ExtractContext:
    """Inputs available to an extractor for one repository snapshot.

    ``spans_by_module`` maps a dotted module name to that module's parsed spans; ``path_by_module``
    maps the same names to their repo-relative POSIX file paths (for provenance/grounding).
    ``materialized_root`` is an on-disk copy of the tree at ``sha`` and ``first_party_root`` the
    importable subdir (e.g. "src" or "") — together they let resolver-based extractors like grimp
    run against the exact snapshot without depending on the working tree.
    """

    sha: str
    materialized_root: str
    first_party_root: str
    spans_by_module: Mapping[str, Sequence[ParsedSpan]]
    path_by_module: Mapping[str, str]
