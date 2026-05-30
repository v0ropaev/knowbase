"""The ``kb`` command-line interface (DESIGN.md §11).

``kb index`` runs the spine for one commit. ``serve`` (MCP) and ``introspect`` (the eval-only
FastAPI oracle) are stubs in this push — they belong to the next push (DESIGN.md §8 "Next push").
"""

from __future__ import annotations

import json

import typer

from kb.daemon.pipeline import index_commit
from kb.extract.deterministic.fastapi_contract import FastAPIExtractor
from kb.extract.deterministic.imports import ImportExtractor
from kb.introspect import introspect_app
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
    result = index_commit(engine, repo, sha, extractors=[ImportExtractor(), FastAPIExtractor()])
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
def introspect(
    app_import: str = typer.Argument(..., help="module:appvar, e.g. app.main:app"),
    repo: str = typer.Option(".", "--repo", help="Repo root the app imports from."),
    root: str = typer.Option("", "--root", help="First-party subdir for sys.path, e.g. src"),
    timeout: float = typer.Option(15.0, "--timeout", help="Sandbox wall-clock timeout (seconds)."),
) -> None:
    """Emit a FastAPI app's openapi() as JSON, run in a sandbox (eval oracle; never on `index`)."""
    result = introspect_app(
        app_import, cwd=repo, extra_sys_path=[root] if root else (), timeout_s=timeout
    )
    typer.echo(json.dumps(result.openapi, indent=2, sort_keys=True))


if __name__ == "__main__":
    app()
