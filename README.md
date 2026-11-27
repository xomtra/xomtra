# xomtra

xomtra turns the history of a [Home Assistant](https://www.home-assistant.io/) installation
into a queryable data platform and exposes it to AI assistants (e.g. Claude) through the
[Model Context Protocol](https://modelcontextprotocol.io/), with identity and roles from
Microsoft Entra ID.

> **Status:** early development (phase 1b – analysis: correlations on top of the data ingest).
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
| `docs/security.md` | Security principles as the code implements them |

## Quick start (local development)

Requirements: [uv](https://docs.astral.sh/uv/), Docker with Compose v2.

```bash
uv sync                      # create .venv with all workspace packages + dev tools
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run pytest

# Contract tests against a real Home Assistant in a container (see the CI `contract` job)
docker run -d --name ha -p 127.0.0.1:8123:8123 \
  -v "$PWD/packages/hacore/tests/contract/ha-config:/config" homeassistant/home-assistant:stable
XOMTRA_CONTRACT_HA_URL=http://127.0.0.1:8123 uv run pytest -m contract

# No .env needed (ADR 077); then `docker compose exec admin xomtra-admin setup-link`.
docker compose -f deploy/compose/compose.yaml -f deploy/compose/compose.dev.yaml up -d --build
```

For local development the MCP server can run with `XOMTRA_AUTH_MODE=none`, which is
only allowed when it listens on a loopback address (local development).

## Releases and installation

**Home Assistant add-on:** add this repository to your Home Assistant with one click:

[![Open your Home Assistant instance and show the add add-on repository dialog with this repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fxomtra%2Fxomtra)

Or add `https://github.com/xomtra/xomtra` under Settings → Add-ons → Add-on store → ⋮ →
Repositories.

**Docker Compose** on any Linux machine: `docker compose up -d` in `deploy/compose`, then
connect Home Assistant in the browser, see [docs/setup/docker.md](docs/setup/docker.md).

Releases follow Semantic Versioning; each one lists its images pinned by digest and the
changes in [CHANGELOG.md](CHANGELOG.md). Install and upgrade from a release tag as
described in [docs/releasing.md](docs/releasing.md), then follow the
[setup guides](docs/setup/README.md).

## Security

Please report vulnerabilities privately, see [SECURITY.md](SECURITY.md).
Container images are published to `ghcr.io/xomtra/` and signed with
[cosign](https://docs.sigstore.dev/) v3 (keyless, GitHub OIDC; the signature is stored
in the new bundle format, which cosign v2 does not find). Verify with cosign v3 before
deploying (the identity is case-sensitive and uses the lowercase `xomtra/xomtra`). Released
images carry a second signature from the release workflow, see
[docs/releasing.md](docs/releasing.md); development builds of `main` only this one:

```bash
cosign verify ghcr.io/xomtra/xomtra-mcp@sha256:<digest> \
  --certificate-identity-regexp '^https://github.com/xomtra/xomtra/\.github/workflows/ci\.yml@refs/heads/main$' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

## License

[Apache License 2.0](LICENSE).
