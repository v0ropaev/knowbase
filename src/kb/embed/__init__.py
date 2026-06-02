"""Embedding adapters + population (DESIGN.md §10) — torch isolated here, off the index path.

Embeddings are a replaceable acceleration layer over the content-addressed knowledge, not part of an
artifact's identity. ``kb embed`` populates them in a separate pass.
"""
