# syntax=docker/dockerfile:1
#
# knowbase image. Multi-stage, uv-based.
#   slim (default):  EXTRAS=""              -> kb index / migrate / serve / introspect (no torch)
#   embed:           EXTRAS="--extra embed" -> adds CPU-torch for `kb embed` + search_knowledge
#
# The project is installed in EDITABLE mode and the source tree is kept at /app, because
# kb.store.migrate resolves migrations/ + db/*.sql via Path(__file__).parents[3] (the repo layout).

# ---- builder: resolve + install deps and the project into /app/.venv ----
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app

ARG EXTRAS=""

# 1) Dependencies only (cached layer keyed on the lockfile; project not installed yet).
COPY pyproject.toml uv.lock ./
# hadolint ignore=SC2086
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev ${EXTRAS}

# 2) Source layout the runtime needs (src + migrations + db + alembic.ini), then install the project.
COPY src ./src
COPY migrations ./migrations
COPY db ./db
COPY alembic.ini README.md LICENSE ./
# hadolint ignore=SC2086
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev ${EXTRAS} \
 && /app/.venv/bin/kb --help >/dev/null

# ---- runtime: slim image with just the venv + source tree ----
FROM python:3.12-slim-bookworm AS runtime

ARG EXTRAS=""
# torch (the embed extra) needs OpenMP at runtime; slim installs no apt packages.
# hadolint ignore=DL3008
RUN case "${EXTRAS}" in \
      *embed*) apt-get update \
            && apt-get install -y --no-install-recommends libgomp1 \
            && rm -rf /var/lib/apt/lists/* ;; \
    esac \
 && useradd --create-home --uid 10001 app

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY --from=builder --chown=app:app /app /app

# OCI image metadata (the build/version/revision args are supplied by CI).
ARG VERSION="0.0.0-dev"
ARG REVISION=""
ARG CREATED=""
LABEL org.opencontainers.image.title="knowbase" \
      org.opencontainers.image.description="Versioned, provenance-grounded knowledge layer over a codebase, served via MCP." \
      org.opencontainers.image.source="https://github.com/v0ropaev/knowbase" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.created="${CREATED}"

USER app
ENTRYPOINT ["kb"]
CMD ["--help"]
