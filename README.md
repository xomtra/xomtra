# HAmmond

HAmmond turns the history of a [Home Assistant](https://www.home-assistant.io/) installation
into a queryable data platform and exposes it to AI assistants (e.g. Claude) through the
[Model Context Protocol](https://modelcontextprotocol.io/), with identity and roles from
Microsoft Entra ID.

> **Status:** early development (phase 1a.2 – data ingest: backfill, live stream, bronze/silver).
> Nothing here is production ready yet. APIs, layout and configuration will change.

## Repository layout

| Path | Contents |
| --- | --- |
| `packages/hacore` | Core library: Home Assistant WebSocket client, registries, history/statistics, bronze layer in DuckLake, live stream |
| `services/mcp-server` | MCP server (FastMCP) with OAuth proxy to Entra ID |
| `services/ingest` | Ingest service: live stream, Dagster jobs and schedules, SQLMesh models (bronze → silver) |
| `deploy/compose` | Docker Compose stack: Caddy + MCP server + ingest + Postgres (DuckLake catalog, Dagster/SQLMesh state) + SeaweedFS (S3) |
| `deploy/caddy` | Caddy image (TLS via ACME DNS-01) and Caddyfile with the public access rules |
| `deploy/vm` | Bootstrap script for the Debian 13 host VM |
| `docs/setup` | Step-by-step guides (Entra app registration, host VM, Claude connector) |

## Quick start (local development)

Requirements: [uv](https://docs.astral.sh/uv/), Docker with Compose v2.

```bash
uv sync                      # create .venv with all workspace packages + dev tools
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run pytest

# Contract tests against a real Home Assistant in a container (see the CI `contract` job)
docker run -d --name ha -p 127.0.0.1:8123:8123 \
  -v "$PWD/packages/hacore/tests/contract/ha-config:/config" homeassistant/home-assistant:stable
HAMMOND_CONTRACT_HA_URL=http://127.0.0.1:8123 uv run pytest -m contract

cp deploy/compose/.env.example deploy/compose/.env   # then edit the values
docker compose -f deploy/compose/compose.yaml -f deploy/compose/compose.dev.yaml up -d --build
```

Without Entra configuration the MCP server can run with `HAMMOND_AUTH_MODE=none`, which is
only allowed when it listens on a loopback address (local development).

## Security

Please report vulnerabilities privately, see [SECURITY.md](SECURITY.md).
Container images are published to `ghcr.io/zettabyte-cz/` and signed with
[cosign](https://docs.sigstore.dev/) (keyless, GitHub OIDC). Verify before deploying:

```bash
cosign verify ghcr.io/zettabyte-cz/hammond-mcp@sha256:<digest> \
  --certificate-identity-regexp '^https://github.com/zettabyte-cz/hammond/\.github/workflows/ci\.yml@refs/heads/main$' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

## License

[Apache License 2.0](LICENSE).
