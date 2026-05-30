"""Structural layer: parse source into content-addressed spans (DESIGN.md §5, §11).

Side-effect-free: never imports or executes the analyzed code. A ``StructuralIndex`` backend
(tree-sitter for the MVP) is hidden behind an interface so a SCIP/typed backend can replace it
later without touching the store.
"""
