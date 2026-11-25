# syntax=docker/dockerfile:1
# Home Assistant add-on image (lite edition). Build context: repository root.
#   docker build -f addon/xomtra/Dockerfile -t xomtra-addon .
# One container: s6-overlay supervises Postgres, the worker and the MCP server.

ARG PYTHON_IMAGE=python:3.13-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d

ARG S6_OVERLAY_VERSION=3.2.1.0

FROM ${UV_IMAGE} AS uv

# s6-overlay archives of both architectures; the runtime stage unpacks the one it needs.
FROM scratch AS s6
ARG S6_OVERLAY_VERSION
ADD --checksum=sha256:42e038a9a00fc0fef70bf0bc42f625a9c14f8ecdfe77d4ad93281edf717e10c5 \
    https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-noarch.tar.xz /s6-overlay-noarch.tar.xz
ADD --checksum=sha256:8bcbc2cada58426f976b159dcc4e06cbb1454d5f39252b3bb0c778ccf71c9435 \
    https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-x86_64.tar.xz /s6-overlay-amd64.tar.xz
ADD --checksum=sha256:c8fd6b1f0380d399422fc986a1e6799f6a287e2cfa24813ad0b6a4fb4fa755cc \
    https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-aarch64.tar.xz /s6-overlay-arm64.tar.xz

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    XOMTRA_DUCKDB_EXTENSION_DIR=/app/duckdb_extensions
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY packages ./packages
COPY services ./services
RUN uv sync --locked --no-dev --no-editable --all-packages
# DuckDB extensions are downloaded at build time (no internet access at runtime).
RUN /app/.venv/bin/python -m hacore.lake install-extensions

FROM ${PYTHON_IMAGE} AS runtime
LABEL org.opencontainers.image.source="https://github.com/xomtra/xomtra" \
      org.opencontainers.image.description="Xomtra Home Assistant add-on (lite edition)" \
      org.opencontainers.image.licenses="Apache-2.0" \
      io.hass.type="addon"
ARG TARGETARCH
RUN --mount=from=s6,target=/s6 \
    apt-get update \
 && apt-get -y upgrade \
 && apt-get install -y --no-install-recommends postgresql-17 xz-utils \
 && rm -f /etc/ssl/private/ssl-cert-snakeoil.key /etc/ssl/certs/ssl-cert-snakeoil.pem \
 && tar -C / -Jxpf /s6/s6-overlay-noarch.tar.xz \
 && tar -C / -Jxpf "/s6/s6-overlay-${TARGETARCH}.tar.xz" \
 && apt-get purge -y xz-utils \
 && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/* \
 && python -m pip uninstall -y pip \
 && rm -rf /usr/local/lib/python3.13/ensurepip /usr/local/bin/pip* \
 && groupadd --system --gid 10001 xomtra \
 && useradd --system --uid 10001 --gid xomtra --home-dir /data --no-create-home xomtra
COPY --from=build /app /app
COPY deploy/compose/postgres/ensure-databases.sh /usr/local/lib/xomtra/ensure-databases.sh
COPY addon/xomtra/rootfs /
ENV PATH=/app/.venv/bin:/usr/lib/postgresql/17/bin:/command:$PATH \
    PYTHONUNBUFFERED=1 \
    XOMTRA_DUCKDB_EXTENSION_DIR=/app/duckdb_extensions \
    S6_BEHAVIOUR_IF_STAGE2_FAILS=2 \
    S6_CMD_WAIT_FOR_SERVICES_MAXTIME=0 \
    S6_KEEP_ENV=1
HEALTHCHECK --interval=60s --timeout=5s --start-period=120s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"]
# s6-overlay starts as root and runs Postgres as user postgres and the Xomtra services
# as user xomtra (s6-setuidgid); the Trivy finding DS-0002 is ignored for this file only
# (.trivyignore.yaml).
ENTRYPOINT ["/init"]
