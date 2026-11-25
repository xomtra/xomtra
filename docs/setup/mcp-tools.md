# Catalog, findings and MCP data tools (phases 1a.3, 1b)

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
| `get_findings` | findings of the daily analysis with entity names; trivial ones hidden unless `include_flagged` |
| `get_weekly_summary` | one week: anomalies, change points, correlations new or gone since the week before |
| `server_info`, `ha_overview` | server version, live registry counts (Admin) |

Views: `entities`, `states`, `statistics`, `statistics_meta`, `catalog_entity`,
`catalog_finding`, `catalog_finding_weekly`, `catalog_drift` (Admin only by default). Times are UTC; daily buckets of
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
   hours (nearly constant series correlate with anything) and data in the last 3 days
   of the window (statistics of removed entities stay in HA). Clocks such as an uptime
   (the hourly mean grows by the same step) are left out: they only correlate through
   common restarts. Entities labelled `hammond_no_analysis` or `hammond_no_ingest` in Home
   Assistant (on the entity or its device) are left out; see `ingest.md`.
2. Residuals after the hour of the day and the day of the week (in `HAMMOND_TIMEZONE`)
   and, if `HAMMOND_CORRELATION_OUTSIDE_TEMP` names an entity with hourly statistics or
   a `weather.*` entity (its `temperature` attribute), the outside temperature. A
   temperature covering less than 70 % of the window is not used (the run notes it);
   hours without it are left out of the analysis. Without it, everything that depends
   on the weather correlates with everything else. Their centred 7-day moving average
   is then subtracted (a high-pass filter), so slow trends such as growing memory,
   uptime or a filling disk do not correlate with each other.
3. Correlation of every pair with a lag of 0–24 hours (the best lag wins), on hours
   both series have (Pearson over those hours, residuals clipped to ±4 standard
   deviations so a few spikes cannot carry a pair). Significance (ADR 062,
   `HAMMOND_CORRELATION_METHOD=v2`, the default): `z = r / sd` with the variance of `r`
   under independence from the autocorrelations of both series (Bartlett/xDF); the
   p-value (`p_value`) of the pair's largest `|z|` over both directions and all lags
   comes from an empirical null of circularly shifted pairs (whole days, so the daily
   cycle stays aligned) with a generalised Pareto tail; Benjamini–Hochberg FDR over
   pairs (`q_value`). `v1` is the method of 0.3.0 (Bonferroni over lags).
4. Series with a lag-0 `|r| >= 0.95` form a group of near-duplicates (one quantity
   measured twice: % and MB, a phase and the total, a daily and a monthly counter).
5. Pairs with `|score| >= 0.3` and `q_value <= 0.01`, at most 100 per window. A relation
   between two groups is one row (its strongest pair; the other members are in
   `entity_group` and `related_group`), and every pair of entities appears once (the
   strongest direction, lag and metric). Pairs of one entity are skipped; `flags` marks
   `same_device` and `duplicate_suspect` (a pair inside one group), which are usually
   trivial and are ranked after the unflagged ones.
6. Structure of the significant relations (v2), also ranked after the unflagged ones:
   - `common_factor`: the relation disappears once the largest common components of
     all series are removed (Marchenko–Pastur edge, at most five), or the pair and at
     least two common neighbours fit one hidden driver (tetrad test: the occupancy of a
     room, one appliance seen by several sensors). An observed hub (a series that
     itself drives the others) is not a hidden driver: its relations stay direct.
     Three series sharing a driver cannot be told from direct relations by any test on
     the data alone.
   - `indirect`: a third significant series C explains the pair (partial correlation
     not significant, lags consistent along A → C → B); C is in `explained_by`.
   - `unstable`: in a window of at least 180 days the relation does not keep its sign
     and `|r| >= 0.2` in both halves.
   - `same_area` (ADR 064): both entities are in one Home Assistant area (the entity's
     area, else its device's). Motion, light and CO₂ of one room correlate through its
     occupancy; such a relation is explained, not a discovery. An entity without an
     area is matched by its id and name against the area ids, names and aliases
     (whole words, diacritics ignored, one area only). An entity with no area found
     gets no flag: its relations stay among the unflagged ones.
   The run logs the histogram of p- and q-values (no entity names) for the
   calibration of `q`.

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

Example question for Claude: *"Use get_findings for the 365-day correlations and
explain the top five."*

## Anomalies and change points (phase 1b.2)

After the correlations, `catalog_daily` looks at every series of the same hourly grid
(same exclusions) one by one and writes to `catalog.finding`:

- `finding_type = 'anomaly'` (`window_days` = `HAMMOND_ANOMALY_RECENT_DAYS`, 7): hours
  of the last 7 days that are unusual for their hour of the week. The usual value is
  the median of the same local hour of the week over the 56 days before
  (`HAMMOND_ANOMALY_BASELINE_DAYS`; an hour of the week with fewer than 3 values uses
  the hour of the day). The deviation is scaled by the robust standard deviation
  (1.4826 × MAD) of that hour, shrunk towards the series' overall one. Hours with
  `|z| >= 4` (`HAMMOND_ANOMALY_Z`) of the same sign form an episode (one normal hour in
  between is bridged); one finding per episode, at most 3 per series: `score` = peak z,
  `n_eff` = anomalous hours, `observed`/`expected` = mean value and usual value of
  those hours, `period_start`/`period_end` = the episode. A series needs 14 days of
  baseline, so binary and enum entities (from the state history) take part only once
  HAmmond has collected that much.
- `finding_type = 'change_point'` (`window_days` = `HAMMOND_CHANGEPOINT_WINDOW_DAYS`,
  90): a lasting shift of the daily mean (local days). For every split in the last 30
  days (`HAMMOND_CHANGEPOINT_RECENT_DAYS`) with at least 14 days before and 7 after, the
  difference of the medians is scaled by the robust deviation of the days; the
  strongest split is reported when it reaches 3 (`HAMMOND_CHANGEPOINT_MIN_SHIFT`) and
  80 % of the days after lie beyond the median before. `period_start` = first day of
  the new level, `observed`/`expected` = median after/before, `n_eff` = days after.

Flags (the finding is kept but ranked last and hidden by `get_findings` by default):
`short` (an anomaly of one hour), `weather` (the outside temperature, when configured,
was itself unusual: mean `|z| >= 3` over the episode, or a shift of 3 at the same day),
`level_shift` (an anomaly after a change point of the same series: the level changed,
the hours are not unusual for the new one).

```sh
sudo docker compose exec dagster-daemon hammond-ingest anomalies
```

## Weekly summary

The Dagster job `findings_weekly` (Monday 07:00 in `HAMMOND_TIMEZONE`) copies the
current findings to `catalog.finding_weekly` under the Monday of the week just ended
(`week_start`), keeping 104 weeks (`HAMMOND_FINDINGS_WEEKLY_KEEP_WEEKS`). The MCP tool
`get_weekly_summary` compares a snapshot with the one before: anomalies and change
points of that week, correlations that are new or gone, and counts per type. It is
rendered when read, under the caller's policy, so a role never sees entities it may
not see. Create the first snapshot by hand after deploying:

```sh
sudo docker compose exec dagster-daemon hammond-ingest findings-weekly
```

Example questions: *"What did HAmmond find last week?"*, *"Are there anomalies on the
heat pump in the last days?"* (`get_findings` with `entity_id`).

## Roles and policy

The built-in policy (`services/mcp-server/src/hammond_mcp/policy.yaml`):

| Role | Tools | Views | Hidden entities | Hidden columns | Rows per call |
| --- | --- | --- | --- | --- | --- |
| `HAmmond.Admin` | all | all | none | none | 10 000 |
| `HAmmond.Reader` | catalog, data and findings tools | all but `catalog_drift` | `person.*`, `device_tracker.*`, `zone.*`, `camera.*`, `image.*`, `lock.*`, `alarm_control_panel.*`, `*_location` | `states.attributes` | 1 000 |
| no role | `whoami` only | – | – | – | – |

Hidden entities and columns are filtered inside the database (the views a caller can
query already exclude them; a finding is hidden when either of its entities is, and
hidden entities are removed from its group lists); asking
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
