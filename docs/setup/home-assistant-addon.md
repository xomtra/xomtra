# Home Assistant add-on (lite edition)

Status: **in development** (phase 1c.2, PR 9). The add-on image is built, scanned and
started in CI on every pull request and published with every release.

The add-on needs Home Assistant OS or Supervised (installations with the Supervisor).
Home Assistant Container and Core have no add-ons; there HAmmond runs next to Home
Assistant with Docker Compose ([lite or full edition](README.md)).

## Installing

In the add-on store of Home Assistant, add the repository
`https://github.com/xomtra/HAmmond` (three-dot menu → Repositories), then install
**HAmmond**. The Supervisor pulls the signed image
`ghcr.io/xomtra/hammond-addon-{amd64,aarch64}` of the version in `config.yaml`;
images exist for every release after 0.4.1 ([releasing](../releasing.md)).

## What runs inside

One container, supervised by [s6-overlay](https://github.com/just-containers/s6-overlay):

| Service | What it does |
| --- | --- |
| `init-hammond` | first start: generates every internal password and key into `/data/secrets.env` (mode 600) and the Postgres cluster in `/data/postgres`; later starts only add keys introduced by newer versions |
| `postgres` | DuckLake catalog, audit, personal tokens, analysis settings (ADR 063); listens on 127.0.0.1 only |
| `init-databases` | roles and databases (the same script as compose) and the DuckLake catalog |
| `worker` | live stream from Home Assistant and the scheduled jobs (ADR 050) |
| `mcp` | MCP server for the LAN on port 8000 with personal tokens (ADR 056, 059) |
| `admin` | admin GUI in the Home Assistant sidebar (Ingress, port 8100 inside the hassio network only, ADR 070) |

Postgres runs as user `postgres`, the HAmmond services as user `hammond`; s6 itself
starts as root, as in other add-ons.

Home Assistant is reached through the Supervisor (`http://supervisor/core`) with the
add-on's own token (`homeassistant_api: true`), so no long-lived access token is needed.

## Admin GUI (Ingress)

The admin GUI opens as **HAmmond** in the Home Assistant sidebar, for Home Assistant
administrators only (`panel_admin`). There is no separate sign-in: Home Assistant signs
the user in and its Ingress proxy (the Supervisor, `172.30.32.2`) passes the user in the
`X-Remote-User-*` headers. The GUI therefore accepts connections only from that address
(`HAMMOND_AUTH_MODE=ingress`) and refuses everything else; when the Supervisor also sends
the user's groups, it checks the administrator group itself. Every page view is in the
audit with the Home Assistant user. Port 8100 is not published outside the add-on.

Pages: overview, catalog, drift, retention, findings, analysis settings, audit, personal
tokens and connecting clients (with the Claude Desktop extension). The Dagster UI of the
full edition does not exist here. Without the `mcp_url` option, the connect page assumes
`http://<the host you opened Home Assistant on>:8000/mcp`.

## Data

| Path | Content | In Home Assistant backups |
| --- | --- | --- |
| `/data/postgres` | catalog, audit, tokens, settings | yes (cold backup: the add-on stops while it is copied) |
| `/data/lake` | Parquet files of the lake (tens of GB in large installations) | no |
| `/data/secrets.env` | generated internal secrets | yes |

Requirements: SSD, at least 4 GB RAM and 20 GB of free space. Home Assistant Green
(32 GB eMMC) and SD cards are not supported; for state retention over one year use the
virtual appliance.

## Options

| Option | Default | Meaning |
| --- | --- | --- |
| `log_level` | `info` | log level of the HAmmond services |
| `states_retention_days` | 90 | days of state history kept in the lake |
| `statistics_5min_retention_days` | 90 | days of 5-minute statistics |
| `statistics_hour_retention_days` | 1825 | days of hourly statistics |
| `mcp_url` | empty | MCP endpoint as clients in the LAN reach it, e.g. `http://homeassistant.local:8000/mcp`; shown with new tokens |

The time zone is Home Assistant's (`TZ` from the Supervisor).

## Building and testing locally

```bash
docker build -f addon/hammond/Dockerfile -t hammond-addon:dev .
addon/hammond/smoke-test.sh hammond-addon:dev
```

The smoke test starts the image like the Supervisor (a `/data` directory with
`options.json`, `SUPERVISOR_TOKEN`, a network like hassio), waits for the MCP server and
the admin GUI, checks that only the Ingress proxy's address may open the admin GUI, that
the secrets and the Postgres cluster are in `/data` and that a restart keeps them.
At the end it reports the memory of the idle add-on per process and fails above 2 GiB
(with an empty lake this is the floor; a real installation adds the lake's caches).
