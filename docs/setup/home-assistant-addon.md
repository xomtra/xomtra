# Home Assistant add-on (lite edition)

Status: **in development** (phase 1c.2, PR 9). The add-on image is built and started in
CI on every pull request; published images, the add-on repository and the admin GUI in
the Home Assistant sidebar (Ingress) follow in the next steps.

## What runs inside

One container, supervised by [s6-overlay](https://github.com/just-containers/s6-overlay):

| Service | What it does |
| --- | --- |
| `init-hammond` | first start: generates every internal password and key into `/data/secrets.env` (mode 600) and the Postgres cluster in `/data/postgres`; later starts only add keys introduced by newer versions |
| `postgres` | DuckLake catalog, audit, personal tokens, analysis settings (ADR 063); listens on 127.0.0.1 only |
| `init-databases` | roles and databases (the same script as compose) and the DuckLake catalog |
| `worker` | live stream from Home Assistant and the scheduled jobs (ADR 050) |
| `mcp` | MCP server for the LAN on port 8000 with personal tokens (ADR 056, 059) |

Postgres runs as user `postgres`, the HAmmond services as user `hammond`; s6 itself
starts as root, as in other add-ons.

Home Assistant is reached through the Supervisor (`http://supervisor/core`) with the
add-on's own token (`homeassistant_api: true`), so no long-lived access token is needed.

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
`options.json`, `SUPERVISOR_TOKEN`), waits for the MCP server, checks that the secrets
and the Postgres cluster are in `/data` and that a restart keeps them.
