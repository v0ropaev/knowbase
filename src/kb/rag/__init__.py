"""The frozen pgvector RAG-over-source baseline — the "other arm" of the knowledge-vs-RAG A/B.

Deliberately separate from ``kb.store``: raw source windows + embeddings, no provenance, no
grounding, no content-addressing. Changing the frozen params is a baseline change (bump version).
"""
