"""[LOCKED] Content-addressed identity hashing — the spine of knowbase (DESIGN.md §5, §6).

These functions are pure and MUST be reproducible: the same logical input always yields the same
digest, independent of file path, byte offsets, formatting, comments, or commit. Changing any rule
here is a breaking change — bump ``NORMALIZATION_VERSION`` (for span identity) or the relevant
``extractor_version`` (for artifacts) so existing digests are invalidated safely rather than
silently colliding.

Identity excludes location by construction: neither function takes a byte offset or a file path.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping

# Bump when any span-identity rule (here or in kb.structural.fingerprint / symbol_path) changes.
NORMALIZATION_VERSION = 1

# ASCII Unit Separator — an unambiguous field boundary inside hashed input. Binary components are
# hex-encoded before hashing so a raw 0x1f byte in a digest can never be mistaken for a separator.
SEP = b"\x1f"


def compute_span_id(
    *,
    lang: str,
    span_kind: str,
    fq_symbol_path: str,
    structural_fingerprint: bytes,
    normalization_version: int = NORMALIZATION_VERSION,
) -> bytes:
    """The content-addressed identity of a code span (DESIGN.md §5). 32-byte sha256 digest.

    Note the absence of ``file_path`` and any offset: identity is structural + symbol-path only.
    """
    h = hashlib.sha256()
    for field in (
        str(normalization_version),
        lang,
        span_kind,
        fq_symbol_path,
        structural_fingerprint.hex(),
    ):
        h.update(field.encode("utf-8"))
        h.update(SEP)
    return h.digest()


def canonical_framework_versions(framework_versions: Mapping[str, str] | None) -> bytes:
    """Canonical JSON of the framework-version subset for an artifact key (b'{}' when empty)."""
    if not framework_versions:
        return b"{}"
    ordered = dict(sorted(framework_versions.items()))
    return json.dumps(ordered, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def compute_artifact_id(
    *,
    derived_from_span_ids: Iterable[bytes],
    extractor_id: str,
    extractor_version: str,
    prompt_version: str = "",
    model_id: str = "",
    framework_versions: Mapping[str, str] | None = None,
) -> bytes:
    """The content-addressed identity of a knowledge artifact (DESIGN.md §6). 32-byte sha256 digest.

    ``derived_from_span_ids`` are byte-sorted and de-duplicated so order of derivation is
    irrelevant. ``prompt_version``/``model_id`` are '' for deterministic extractors;
    ``framework_versions`` is included only where output depends on it (NOT the import graph).
    Raises ``ValueError`` if there are no derived-from spans — the anti-hallucination invariant
    enforced at the identity layer, mirroring the DB constraint trigger.
    """
    sorted_ids = sorted(set(derived_from_span_ids))
    if not sorted_ids:
        raise ValueError("artifact must derive from >= 1 span (anti-hallucination invariant)")
    h = hashlib.sha256()
    h.update(str(len(sorted_ids)).encode("utf-8"))
    h.update(SEP)
    for span_id in sorted_ids:
        h.update(span_id.hex().encode("utf-8"))
        h.update(SEP)
    for field in (extractor_id, extractor_version, prompt_version, model_id):
        h.update(field.encode("utf-8"))
        h.update(SEP)
    h.update(canonical_framework_versions(framework_versions))
    h.update(SEP)
    return h.digest()
