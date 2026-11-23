# syntax=docker/dockerfile:1
# Build context: repository root (uv workspace).
#   docker build -f services/ingest/Dockerfile -t hammond-ingest .
# One image, several roles (compose `command`): `hammond-ingest stream`,
# `dagster-daemon run`, `dagster-webserver`, and one-off CLI tasks.

ARG PYTHON_IMAGE=python:3.15-slim-trixie@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928

ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    HAMMOND_DUCKDB_EXTENSION_DIR=/app/duckdb_extensions
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY packages ./packages
COPY services ./services
RUN uv sync --locked --no-dev --no-editable --package hammond-ingest
# DuckDB extensions are downloaded at build time (no internet access at runtime).
RUN /app/.venv/bin/python -m hacore.lake install-extensions
COPY services/ingest/dagster /app/dagster

FROM ${PYTHON_IMAGE} AS runtime
LABEL org.opencontainers.image.source="https://github.com/zettabyte-cz/hammond" \
      org.opencontainers.image.description="HAmmond ingest (stream, Dagster, SQLMesh)" \
      org.opencontainers.image.licenses="Apache-2.0"
RUN apt-get update \
 && apt-get -y upgrade \
 && rm -rf /var/lib/apt/lists/* \
 && python -m pip uninstall -y pip \
 && rm -rf /usr/local/lib/python3.13/ensurepip /usr/local/bin/pip* \
 && groupadd --system --gid 10001 hammond \
 && useradd --system --uid 10001 --gid hammond --home-dir /data --no-create-home hammond \
 && mkdir -p /data && chown hammond:hammond /data
COPY --from=build /app /app
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    HAMMOND_DUCKDB_EXTENSION_DIR=/app/duckdb_extensions \
    DAGSTER_HOME=/app/dagster \
    HOME=/data
USER 10001:10001
WORKDIR /data
VOLUME ["/data"]
# No HEALTHCHECK here: it differs per role (stream heartbeat, Dagster UI) and lives in
# compose.yaml; "HEALTHCHECK NONE" would make "docker compose up --wait" fail.
ENTRYPOINT []
CMD ["hammond-ingest", "--help"]
