# syntax=docker/dockerfile:1
# Build context: repository root (uv workspace).
#   docker build -f services/mcp-server/Dockerfile -t hammond-mcp .

ARG PYTHON_IMAGE=python:3.13-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

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
RUN uv sync --locked --no-dev --no-editable --package hammond-mcp
# DuckDB extensions are downloaded at build time so the runtime needs no internet
# access and runs exactly the extensions that were scanned.
RUN /app/.venv/bin/python -m hacore.lake install-extensions

FROM ${PYTHON_IMAGE} AS runtime
LABEL org.opencontainers.image.source="https://github.com/xomtra/xomtra" \
      org.opencontainers.image.description="HAmmond MCP server" \
      org.opencontainers.image.licenses="Apache-2.0"
# Apply Debian security updates on top of the pinned base image and drop pip/ensurepip:
# the runtime installs nothing, and pip's vendored libraries only add scan findings.
RUN apt-get update \
 && apt-get -y upgrade \
 && rm -rf /var/lib/apt/lists/* \
 && python -m pip uninstall -y pip \
 && rm -rf /usr/local/lib/python3.13/ensurepip /usr/local/bin/pip* \
 && groupadd --system --gid 10001 hammond \
 && useradd --system --uid 10001 --gid hammond --home-dir /data --no-create-home hammond \
 && mkdir -p /data /lake && chown hammond:hammond /data /lake
COPY --from=build /app /app
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    HAMMOND_HOST=0.0.0.0 \
    HAMMOND_PORT=8000 \
    HAMMOND_DUCKDB_EXTENSION_DIR=/app/duckdb_extensions \
    FASTMCP_HOME=/data/fastmcp
USER 10001:10001
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"]
ENTRYPOINT ["hammond-mcp"]
