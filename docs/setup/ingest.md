# Data ingest (phase 1a.2)

The ingest copies the history of Home Assistant into the lake and keeps it current:

| Part | What it does | Runs as |
| --- | --- | --- |
| Live stream | every `state_changed` event into `bronze.ha_states`; after a reconnect it fills the gap from the HA history API (catch-up) | service `ingest-stream` |
| Hourly ingest | registries and statistics metadata (change log), 5-minute and hourly statistics, then SQLMesh `bronze → silver` | Dagster job `ingest_hourly`, every hour at :07 |
| Retention | deletes old rows (states 365 days, 5-minute statistics 365 days, hourly statistics 5 years by default), compacts files, expires snapshots | Dagster job `retention_daily`, 03:30 |
| Catalog | profile of every entity over 30 days and drift against the previous day into `catalog.entity` / `catalog.drift` (see [mcp-tools.md](mcp-tools.md)) | Dagster job `catalog_daily`, 04:15 |
| Restate after outage | recomputes silver for a range the stream caught up after a longer outage | Dagster sensor `restate_after_catchup` → job `silver_restate` |
| Backfill | the state history Home Assistant still has (recorder, 10 days by default) | Dagster job `history_backfill` or CLI, once |

Everything is idempotent: rows are keyed by `(entity_id, last_updated)` for states and
`(statistic_id, period, period_start)` for statistics, so repeated runs and the
catch-up never create duplicates.

Data comes only from the running Home Assistant instance. History older than what its
recorder still holds is not imported (no import from backups, ADR 033).

Layers in the lake (DuckLake, schema per layer):

* `bronze.ha_states`, `bronze.ha_statistics`, `bronze.ha_registry` – raw data with
  `source` (`ha_live`, `ha_history`, `ha_api`) and ingest time.
* `silver.states`, `silver.statistics` – deduplicated, typed (`state_value`,
  `is_missing`), incremental per hour; `silver.entities`, `silver.statistics_meta`,
  `silver.registry_current` – current catalog (device, effective area, floor, labels,
  unit, device class, state class).

## 1. Home Assistant user and network

The ingest uses the same `XOMTRA_HA_URL` / `XOMTRA_HA_TOKEN` as the MCP server: a
long-lived token of a dedicated **non-admin** user. Read access is all it needs
(registries, history, statistics); the contract tests in CI verify this with a non-admin
user on every pull request. The VM needs TCP 8123 to Home Assistant (firewall rule from
the DMZ to the HA host only).

## 2. Configuration (`.env`)

New values compared to 1a.1 (see `.env.example`):

```bash
XOMTRA_INGEST_IMAGE=ghcr.io/xomtra/xomtra-ingest@sha256:...   # verified digest
OPS_PG_PASSWORD=<openssl rand -hex 32>   # role owning the Dagster + SQLMesh databases
XOMTRA_STATES_RETENTION_DAYS=365            # state history
XOMTRA_STATISTICS_5MIN_RETENTION_DAYS=365   # 5-minute statistics
XOMTRA_STATISTICS_HOUR_RETENTION_DAYS=1825  # hourly statistics (5 years)
XOMTRA_TIMEZONE=Europe/Prague           # time zone of the schedules
XOMTRA_DAGSTER_BIND=127.0.0.1           # Dagster UI on loopback only
```

Verify the image signature like the others (README, `cosign verify`) and pin the digest.

### Editions: full and lite (ADR 050)

`XOMTRA_EDITION` selects the defaults of a resource profile; every value can still be
set explicitly with its own variable.

| | `full` (default, compose/VM) | `lite` (HA add-on, small machines) |
|---|---|---|
| Lake data files | object storage (`XOMTRA_S3_*`) | local directory `XOMTRA_LAKE_DATA_PATH` |
| State / 5-minute / hourly retention | 365 / 365 / 1825 days | 90 / 90 / 1825 days |
| DuckDB memory per process | 2 GB | 512 MB |

`XOMTRA_LAKE_DATA_PATH` (an absolute path) puts the data files on the local file
system; the `XOMTRA_S3_*` variables are then not needed and the DuckLake catalog stays
in Postgres. Keep the directory on an SSD, not on an SD card or eMMC.

**Lite stack with Docker compose.** `compose.lite.yaml` turns `compose.yaml` into the
lite edition: no SeaweedFS and no Dagster; the lake data files are in the volume
`lake-data`, and one `worker` container (`xomtra-ingest worker`) runs the live stream
and the scheduled jobs with the same schedules as Dagster (hourly ingest at :07,
retention 03:30, analyses 04:15, weekly snapshot Monday 07:00, in `XOMTRA_TIMEZONE`).

```bash
docker compose -f compose.yaml -f compose.lite.yaml up -d
docker compose -f compose.yaml -f compose.lite.yaml exec worker xomtra-ingest runs
docker compose -f compose.yaml -f compose.lite.yaml exec worker xomtra-ingest job catalog_daily
```

The worker records its runs in the Postgres database `ops` (`worker.job_run`, kept 90
days). Jobs run one at a time; a schedule missed while the worker was down runs once
after the start. Below 10 % free space on the lake's disk
(`XOMTRA_WORKER_MIN_FREE_RATIO`) it logs a warning and runs the retention early, at
most once a day.

## 3. Deploy

```bash
sudo -i
cd /opt/xomtra
cp -r ~<admin>/xomtra/deploy/compose/. /opt/xomtra/   # keeps your .env
editor .env                                              # values from step 2
docker compose pull
docker compose up -d
docker compose ps        # postgres-init + ducklake-init exited (0), ingest-stream healthy
docker compose logs -f ingest-stream                     # "Authenticated to Home Assistant"
exit
```

`postgres-init` runs on every `up` and creates missing roles and databases, so an
existing 1a.1 installation gets the `dagster` and `sqlmesh` databases without touching
the DuckLake catalog. It also keeps the role passwords in sync with `.env`.

## 4. Dagster UI

Open `https://<public-host>/admin/dagster/` from the admin network after signing in to
the [admin GUI](admin-gui.md). As a fallback the UI is also published on the VM's
loopback:

```bash
ssh -L 3000:127.0.0.1:3000 <admin>@<vm-ip>
# then open http://127.0.0.1:3000/admin/dagster/
```

The schedules `ingest_hourly`, `retention_daily` and `catalog_daily` are active from the
start. Jobs
run one at a time (queued run coordinator); the live stream runs next to them.

## 5. First backfill

Copy what Home Assistant still has in its recorder (default 10 days of states, 10 days
of 5-minute statistics, hourly statistics for up to 5 years):

```bash
sudo docker compose -f /opt/xomtra/compose.yaml exec dagster-daemon \
  xomtra-ingest backfill --start 10d
sudo docker compose -f /opt/xomtra/compose.yaml exec dagster-daemon xomtra-ingest sync
```

Or in the UI: *Jobs → history_backfill → Launchpad*, config `start: "10d"`. The backfill
recomputes the affected silver range at the end.

## Leaving entities out (labels in Home Assistant)

Create these labels in Home Assistant (Settings → Areas, labels & zones → Labels) and put
them on an entity or on a device (a device label also covers its future entities):

| Label | Effect |
|-------|--------|
| `xomtra_no_ingest` | Not stored at all: live stream, catch-up, history backfill, statistics. Data stored before stays. |
| `xomtra_no_analysis` | Stored and queryable, but left out of correlations, anomalies and change points. |

The label ids are settings: `XOMTRA_LABEL_NO_INGEST` and `XOMTRA_LABEL_NO_ANALYSIS`
(defaults above) take the id of any label you already use. Home Assistant derives a
label's id from its name when the label is created (lower case, spaces become `_`) and
keeps it when the label is renamed later; after changing a setting restart the
`ingest-stream` and `dagster-daemon` containers.

`xomtra_no_ingest` implies `xomtra_no_analysis`. The stream re-reads the labels every
5 minutes; the correlations on every run (from `silver.entities`, i.e. after the next
hourly ingest). Removing `xomtra_no_ingest` later backfills only what the HA recorder
still keeps (its retention, 10 days by default). Who sees what over MCP stays with
`policy.yaml`.

## 6. Operations

| Task | Command (in `dagster-daemon`) |
| --- | --- |
| Run ingest + transformations now | `xomtra-ingest sync` |
| Recompute silver for a range | `xomtra-ingest restate --start 2026-01-01 --end 2026-02-01` |
| Retention / maintenance now | `xomtra-ingest retention`, `xomtra-ingest maintenance` |
| Stream status | `docker compose logs ingest-stream`, health check = flush heartbeat |

Notes:

* **Gaps.** After an outage of the VM or Home Assistant the stream catches up from the HA
  history API, up to `XOMTRA_STREAM_MAX_CATCHUP_DAYS` (10, the default recorder
  retention of HA). Data of longer outages is not recovered. When a catch-up reaches
  further back than the silver lookback (2 hours), the stream records the range and the
  sensor `restate_after_catchup` starts `silver_restate` for it within 5 minutes.
* **Attributes.** Live events carry all attributes; the HA recorder drops some
  ("unrecorded" attributes such as `min`/`max` of `input_number`). Rows that came from
  the history API (backfill, catch-up) can therefore have fewer attributes than live rows.
* **Units.** Statistics are in the display unit reported in `silver.statistics_meta`.
* **Deleting data.** Retention deletes rows at once; the files disappear from object
  storage after the snapshot retention (7 days), when the maintenance job expires the
  snapshots that still reference them.
