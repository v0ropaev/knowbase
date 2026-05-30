"""Import/dependency extractor — the spine bring-up extractor (DESIGN.md §8).

Produces one ``import_edge`` artifact per first-party module->module dependency. Edge *resolution*
comes from grimp; the grounding *span* (the exact import statement) comes from tree-sitter, mapped
by line number — grimp only reports a line number, not a byte span. ``framework_versions`` is
deliberately empty: the import graph does not depend on any framework version, so it stays out of
the artifact key (DESIGN.md §6).

Known limits handled here (DESIGN.md §13): dynamic/conditional imports are invisible to static
analysis (a recorded gap, surfaced by the eval, never a silent loss); re-exports / relative /
``None``-line imports that cannot be mapped 1:1 to a statement fall back to grounding on the
module span (``role="import_unresolved_span"``, ``payload.span_mapping="approximate"``) — still
grounded, but flagged.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from typing import Any

import grimp

from kb.extract.base import DerivedEdge, ExtractContext, ExtractedArtifact
from kb.structural.interface import ParsedSpan

EXTRACTOR_ID = "imports"
EXTRACTOR_VERSION = "1"


class ImportExtractor:
    extractor_id = EXTRACTOR_ID
    extractor_version = EXTRACTOR_VERSION

    def extract(self, ctx: ExtractContext) -> list[ExtractedArtifact]:
        top_packages = sorted({m.split(".")[0] for m in ctx.spans_by_module if m})
        if not top_packages:
            return []

        import_span_by_line = {
            module: {s.start_line: s for s in spans if s.span_kind == "import"}
            for module, spans in ctx.spans_by_module.items()
        }
        module_span = {
            module: next((s for s in spans if s.span_kind == "module"), None)
            for module, spans in ctx.spans_by_module.items()
        }

        importable_root = (
            os.path.join(ctx.materialized_root, ctx.first_party_root)
            if ctx.first_party_root
            else ctx.materialized_root
        )
        artifacts: list[ExtractedArtifact] = []
        added = importable_root not in sys.path
        if added:
            sys.path.insert(0, importable_root)
        try:
            # grimp statically parses (never imports/executes) the named first-party packages.
            graph = grimp.build_graph(*top_packages, include_external_packages=False)
            for importer in graph.modules:
                if importer not in ctx.spans_by_module:
                    continue
                for imported in graph.find_modules_directly_imported_by(importer):
                    details = graph.get_import_details(importer=importer, imported=imported)
                    artifact = self._edge_artifact(
                        importer, imported, details, import_span_by_line, module_span
                    )
                    if artifact is not None:
                        artifacts.append(artifact)
        finally:
            if added and importable_root in sys.path:
                sys.path.remove(importable_root)
        return artifacts

    def _edge_artifact(
        self,
        importer: str,
        imported: str,
        details: Sequence[Mapping[str, Any]],
        import_span_by_line: dict[str, dict[int, ParsedSpan]],
        module_span: dict[str, ParsedSpan | None],
    ) -> ExtractedArtifact | None:
        lines = sorted({d["line_number"] for d in details if d.get("line_number")})
        by_line = import_span_by_line.get(importer, {})
        grounding: dict[bytes, DerivedEdge] = {}
        for line in lines:
            span = by_line.get(line)
            if span is not None:
                grounding[span.span_id] = DerivedEdge(span.span_id, "import_statement")

        approximate = not grounding
        if approximate:
            fallback = module_span.get(importer)
            if fallback is None:
                return None  # cannot ground -> never store an ungrounded artifact
            grounding[fallback.span_id] = DerivedEdge(fallback.span_id, "import_unresolved_span")

        return ExtractedArtifact(
            kind="import_edge",
            logical_key=f"import:{importer}->{imported}",
            payload={
                "importer": importer,
                "imported": imported,
                "lines": lines,
                "span_mapping": "approximate" if approximate else "exact",
            },
            derived_from=list(grounding.values()),
            extractor_id=self.extractor_id,
            extractor_version=self.extractor_version,
        )
