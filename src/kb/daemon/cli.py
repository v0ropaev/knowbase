"""The ``kb`` command-line interface (DESIGN.md §11).

``kb index`` runs the spine for one commit; ``migrate`` applies the schema; ``embed`` populates
embeddings; ``serve`` hosts the read-only MCP server over stdio; ``introspect`` is the eval-only
sandboxed FastAPI openapi oracle.
"""

from __future__ import annotations

import json

import typer

from kb.daemon.pipeline import index_commit
from kb.extract.deterministic.entities import EntityExtractor
from kb.extract.deterministic.fastapi_contract import FastAPIExtractor
from kb.extract.deterministic.imports import ImportExtractor
from kb.extract.deterministic.library_surface import LibrarySurfaceExtractor
from kb.introspect import introspect_app
from kb.store.engine import make_engine, resolve_db_url

app = typer.Typer(no_args_is_help=True, help="knowbase — a provenance-grounded knowledge layer.")


@app.command()
def index(
    repo: str = typer.Argument(..., help="Path to the git repository to index."),
    sha: str = typer.Option("HEAD", "--sha", help="Commit-ish to index (sha, branch, tag, HEAD)."),
    db_url: str | None = typer.Option(None, "--db-url", help="Postgres URL (else KB_DB_URL env)."),
    incremental: bool = typer.Option(
        False,
        "--incremental/--full",
        help="Reuse unchanged files' spans from the parent snapshot (parse only changed files).",
    ),
    parent: str | None = typer.Option(
        None,
        "--parent",
        help="Parent commit-ish to diff against (implies --incremental; must be indexed).",
    ),
) -> None:
    """Index one commit: ingest, parse spans, run deterministic extractors, write the snapshot."""
    engine = make_engine(db_url)
    result = index_commit(
        engine,
        repo,
        sha,
        extractors=[
            ImportExtractor(),
            FastAPIExtractor(),
            EntityExtractor(),
            LibrarySurfaceExtractor(),
        ],
        incremental=incremental,
        parent=parent,
    )
    engine.dispose()
    typer.echo(
        f"indexed {result.sha[:12]} ({result.mode}): {result.files_indexed} files "
        f"({result.parsed_files} parsed, {result.reused_files} reused), {result.spans} spans, "
        f"{result.artifacts} artifacts, {len(result.gaps)} gaps"
    )
    if result.gaps:
        typer.echo(f"  gaps (unparseable, recorded): {', '.join(result.gaps)}")


@app.command()
def migrate(
    db_url: str | None = typer.Option(None, "--db-url", help="Postgres URL (else KB_DB_URL env)."),
) -> None:
    """Apply all Alembic migrations up to head (creates/updates the schema)."""
    from kb.store.migrate import upgrade_to_head  # local import keeps alembic off other commands

    resolved = resolve_db_url(db_url)
    upgrade_to_head(resolved)
    typer.echo(f"migrated to head: {resolved}")


@app.command()
def embed(
    db_url: str | None = typer.Option(None, "--db-url", help="Postgres URL (else KB_DB_URL env)."),
    sha: str | None = typer.Option(None, "--sha", help="Snapshot sha to embed (default: latest)."),
) -> None:
    """Populate artifact embeddings for a snapshot (separate pass; pulls in the embedding model)."""
    from kb.embed.population import embed_snapshot  # local imports keep torch off the index path
    from kb.embed.providers import default_provider
    from kb.store.queries import latest_ingested_sha

    engine = make_engine(db_url)
    try:
        with engine.connect() as conn:
            target = sha or latest_ingested_sha(conn)
        if target is None:
            typer.echo("no snapshot to embed")
            raise typer.Exit(code=1)
        provider = default_provider()
        result = embed_snapshot(engine, target, provider)
        typer.echo(f"embedded {result.embedded} artifacts @ {target[:12]} with {provider.model_id}")
    finally:
        engine.dispose()


@app.command()
def describe(
    db_url: str | None = typer.Option(None, "--db-url", help="Postgres URL (else KB_DB_URL env)."),
    sha: str | None = typer.Option(None, "--sha", help="Snapshot sha to describe (def: latest)."),
) -> None:
    """LLM-grounded NL descriptions for a snapshot (separate key-gated pass; never on `index`)."""
    from kb.extract.semantic.describe import describe_snapshot  # lazy: keeps the LLM off other cmds
    from kb.llm.providers import default_llm_provider, has_llm_key
    from kb.store.queries import latest_ingested_sha

    if not has_llm_key():
        typer.echo("no LLM API key (set ANTHROPIC_API_KEY or OPENAI_API_KEY)")
        raise typer.Exit(code=1)
    engine = make_engine(db_url)
    try:
        with engine.connect() as conn:
            target = sha or latest_ingested_sha(conn)
        if target is None:
            typer.echo("no snapshot to describe")
            raise typer.Exit(code=1)
        provider = default_llm_provider()
        result = describe_snapshot(engine, target, provider)
        typer.echo(
            f"described {result.described} artifacts (dropped {result.dropped_claims} claims) "
            f"@ {target[:12]} with {provider.model_id}"
        )
    finally:
        engine.dispose()


@app.command()
def serve(
    db_url: str | None = typer.Option(None, "--db-url", help="Postgres URL (else KB_DB_URL env)."),
) -> None:
    """Run the read-only MCP server over stdio (find_provenance, get_knowledge)."""
    from kb.mcp.server import build_server  # local import keeps fastmcp off the `index` path

    engine = make_engine(db_url)
    try:
        build_server(engine).run()
    finally:
        engine.dispose()


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
