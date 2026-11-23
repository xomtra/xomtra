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
`catalog_finding`, `catalog_drift` (Admin only by default). Times are UTC; daily buckets of
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

## Correlations (phase 1b.1)

After the profile, `catalog_daily` looks for entities that move together and writes the
strongest pairs to `catalog.finding` (view `catalog_finding`, `finding_type =
'correlation'`). The method comes from the prototype script `ha_correlations.py`:

1. An hourly grid over each window of `HAMMOND_CORRELATION_WINDOWS_DAYS` (default
   `[365, 30]`): hourly means of measurements and hourly increases of counters from the
   HA statistics, and for binary and enum entities the share of the hour spent in a
   state (`on`, or up to three most frequent states). Gaps of up to 3 hours are filled
   forward. A series needs at least 7 days of data, 70 % coverage of its own span (an
   entity added later still takes part) and a value that changes in at least 5 % of
   hours (nearly constant series correlate with anything).
2. Residuals after the hour of the day and the day of the week (in `HAMMOND_TIMEZONE`)
   and, if `HAMMOND_CORRELATION_OUTSIDE_TEMP` names an entity with hourly statistics or
   a `weather.*` entity (its `temperature` attribute), the outside temperature. A
   temperature covering less than 70 % of the window is not used (the run notes it);
   hours without it are left out of the analysis. Without it, everything that depends
   on the weather correlates with everything else.
3. Correlation of every pair with a lag of 0–24 hours (the best lag wins), on hours
   both series have; significance corrected for autocorrelation, Bonferroni over lags
   and Benjamini–Hochberg FDR over pairs.
4. Pairs with `|score| >= 0.3` and `q_value <= 0.01`, one row per pair of entities (the
   strongest direction, lag and metric), at most 100 per window. Pairs of one entity
   are skipped; `flags` marks `same_device` and `duplicate_suspect` (`|r| > 0.97`), which
   are usually trivial and are ranked after the unflagged ones.

`entity_id` leads `related_entity_id` by `lag_hours`. `first_detected_at` stays from the
first run that found the pair; every run replaces the findings of its window. A
correlation is a hint to look at, not a cause.

```sh
sudo docker compose exec dagster-daemon hammond-ingest correlations
```

### Outside temperature history from Open-Meteo

A `weather.*` entity only has the history HAmmond collected. With
`HAMMOND_OPEN_METEO_ENABLED=true`, `catalog_daily` first stores the hourly
`temperature_2m` of the [Open-Meteo historical archive](https://open-meteo.com/en/docs/historical-weather-api)
for the HA home location (from the HA configuration, rounded to two decimals, about
1 km) into `bronze.ext_outside_temperature`, for the longest correlation window, and
the correlations fill the hours the outside temperature entity lacks from it. The
archive lags a few days; the entity covers the recent hours. The ingest needs outbound
HTTPS to `archive-api.open-meteo.com`; a failed download is logged and the correlations
run without it. Open-Meteo is free for non-commercial use; check its terms otherwise.

```sh
sudo docker compose exec dagster-daemon hammond-ingest outside-temperature
```

Example question for Claude: *"Which findings in catalog_finding for the 365-day
window are not flagged, and what could explain the top five?"*

## Roles and policy

The built-in policy (`services/mcp-server/src/hammond_mcp/policy.yaml`):

| Role | Tools | Views | Hidden entities | Hidden columns | Rows per call |
| --- | --- | --- | --- | --- | --- |
| `HAmmond.Admin` | all | all | none | none | 10 000 |
| `HAmmond.Reader` | catalog and data tools | all but `catalog_drift` | `person.*`, `device_tracker.*`, `zone.*`, `camera.*`, `image.*`, `lock.*`, `alarm_control_panel.*`, `*_location` | `states.attributes` | 1 000 |
| no role | `whoami` only | – | – | – | – |

Hidden entities and columns are filtered inside the database (the views a caller can
query already exclude them; a finding is hidden when either of its entities is); asking
for a hidden entity directly is refused and audited as `denied`. State attributes are
hidden from Reader because they carry free text (media titles, addresses, coordinates,
e-mails) also outside the hidden domains. To change
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
