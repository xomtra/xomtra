# Catalog and MCP data tools (phase 1a.3)

The MCP server answers questions about the history of Home Assistant from the lake.
Every call passes a policy layer by Entra app role and is audited.

## Tools

| Tool | What it does |
| --- | --- |
| `whoami` | identity and roles from the token (always allowed) |
| `search_catalog` | find entities by words, area/floor, domain or profile kind |
| `describe_entity` | metadata, 30-day profile, latest state, statistics metadata, drift |
| `list_views` | SQL views available to the caller, with columns |
| `get_timeseries` | values over time: raw state changes, or 5-minute/hourly/daily buckets (HA statistics when available) |
| `query_sql` | one read-only DuckDB `SELECT` over the views |
| `server_info`, `ha_overview` | server version, live registry counts (Admin) |

Views: `entities`, `states`, `statistics`, `statistics_meta`, `catalog_entity`,
`catalog_drift` (Admin only by default). Times are UTC; daily buckets of
`get_timeseries` use `HAMMOND_TIMEZONE`.

## Catalog

The Dagster job `catalog_daily` (04:15 in `HAMMOND_TIMEZONE`) profiles every entity over
the last `HAMMOND_CATALOG_WINDOW_DAYS` (30) days into `catalog.entity` and appends the
changes against the previous profile to `catalog.drift`: new and removed entities, a
changed unit, device class, state class, area, name or kind, an entity that stopped or
resumed reporting, and a shift of the recent median outside the usual range.

Run it once right after deploying (the first run records no drift):

```sh
sudo docker compose exec dagster-daemon hammond-ingest catalog
```

## Roles and policy

The built-in policy (`services/mcp-server/src/hammond_mcp/policy.yaml`):

| Role | Tools | Views | Hidden entities | Hidden columns | Rows per call |
| --- | --- | --- | --- | --- | --- |
| `HAmmond.Admin` | all | all | none | none | 10 000 |
| `HAmmond.Reader` | catalog and data tools | all but `catalog_drift` | `person.*`, `device_tracker.*`, `zone.*`, `camera.*`, `image.*`, `lock.*`, `alarm_control_panel.*`, `*_location` | `states.attributes` | 1 000 |
| no role | `whoami` only | – | – | – | – |

Hidden entities and columns are filtered inside the database (the views a caller can
query already exclude them); asking for a hidden entity directly is refused and audited
as `denied`. State attributes are hidden from Reader because they carry free text (media
titles, addresses, coordinates, e-mails) also outside the hidden domains. To change
the policy, mount your own file into the `mcp` container and set `HAMMOND_POLICY_FILE`.

Assign the roles in Entra: **Enterprise applications → HAmmond MCP → Users and groups**
(see [Entra app registration](entra-app-registration.md), step 6). A new role takes
effect after the user reconnects the connector in claude.ai.

## SQL

`query_sql` accepts one `SELECT` (CTEs and set operations included) over the view names
without a schema. Table functions, file paths, `ATTACH`, `SET`, `PRAGMA` and functions
that read files or run SQL from strings are refused. Behind that check the query runs
on a DuckDB connection with the lake attached read-only, external file and network
access disabled and the configuration locked, limited by `HAMMOND_QUERY_TIMEOUT_SECONDS`
(30 s), `HAMMOND_MCP_DUCKDB_MEMORY_LIMIT` and the role's row limit.

## Audit

Each tool call is one row in `audit.tool_call` (database `audit`): time, Entra object
id, username, roles, OAuth client, tool, arguments (SQL included), decision, outcome,
error, rows and duration. The MCP server's role `hammond_audit` may only `INSERT`. The
same record is logged as a JSON line by the `hammond.audit` logger. If the database
cannot store the record, the call fails instead of returning data.

Records are kept for `AUDIT_RETENTION_DAYS` (default 90, `0` keeps everything). Once a day
the MCP server calls `audit.purge_expired()`, a function owned by `postgres` that deletes
only records older than that; the retention is set when `postgres-init` runs, so change
it in `.env` and run `sudo docker compose up -d`.

```sh
sudo docker compose exec postgres psql -U postgres -d audit -c \
  "SELECT ts, username, tool, decision, outcome, row_count FROM audit.tool_call ORDER BY ts DESC LIMIT 20"
```

## Upgrade from 1a.2

1. Add `AUDIT_PG_PASSWORD` (e.g. `openssl rand -hex 32`) to `.env`; optionally
   `HAMMOND_TIMEZONE`, `HAMMOND_QUERY_TIMEOUT_SECONDS`, `HAMMOND_MCP_*` limits.
2. Copy the new `deploy/compose`, pin the new image digests, then
   `sudo docker compose pull && sudo docker compose up -d` (`postgres-init` creates the
   audit database and role).
3. Run the first catalog profile (above) and test the tools from claude.ai.
