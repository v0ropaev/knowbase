"""The ``kb`` command-line interface (DESIGN.md §11).

``kb index`` runs the spine for one commit. ``serve`` (MCP) and ``introspect`` (the eval-only
FastAPI oracle) are stubs in this push — they belong to the next push (DESIGN.md §8 "Next push").
"""

from __future__ import annotations

import typer

from kb.daemon.pipeline import index_commit
from kb.extract.deterministic.imports import ImportExtractor
from kb.store.engine import make_engine

app = typer.Typer(no_args_is_help=True, help="knowbase — a provenance-grounded knowledge layer.")


@app.command()
def index(
    repo: str = typer.Argument(..., help="Path to the git repository to index."),
    sha: str = typer.Option("HEAD", "--sha", help="Commit-ish to index (sha, branch, tag, HEAD)."),
    db_url: str | None = typer.Option(None, "--db-url", help="Postgres URL (else KB_DB_URL env)."),
) -> None:
    """Index one commit: ingest, parse spans, run deterministic extractors, write the snapshot."""
    engine = make_engine(db_url)
    result = index_commit(engine, repo, sha, extractors=[ImportExtractor()])
    engine.dispose()
    typer.echo(
        f"indexed {result.sha[:12]}: {result.files_indexed} files, {result.spans} spans, "
        f"{result.artifacts} artifacts, {len(result.gaps)} gaps"
    )
    if result.gaps:
        typer.echo(f"  gaps (unparseable, recorded): {', '.join(result.gaps)}")


@app.command()
def serve() -> None:
    """Run the read-only MCP server (next push)."""
    typer.echo("kb serve: MCP server is part of the next push (DESIGN.md §8).")
    raise typer.Exit(code=1)


@app.command()
def introspect(app_import: str) -> None:
    """Emit a FastAPI app's openapi() as an eval oracle artifact (next push)."""
    typer.echo(f"kb introspect {app_import}: runtime oracle is in the next push (DESIGN.md §8).")
    raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
