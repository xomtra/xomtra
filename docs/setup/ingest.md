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

The ingest uses the same `HAMMOND_HA_URL` / `HAMMOND_HA_TOKEN` as the MCP server: a
long-lived token of a dedicated **non-admin** user. Read access is all it needs
(registries, history, statistics); the contract tests in CI verify this with a non-admin
user on every pull request. The VM needs TCP 8123 to Home Assistant (firewall rule from
the DMZ to the HA host only).

## 2. Configuration (`.env`)

New values compared to 1a.1 (see `.env.example`):

```bash
HAMMOND_INGEST_IMAGE=ghcr.io/zettabyte-cz/hammond-ingest@sha256:...   # verified digest
OPS_PG_PASSWORD=<openssl rand -hex 32>   # role owning the Dagster + SQLMesh databases
HAMMOND_STATES_RETENTION_DAYS=365            # state history
HAMMOND_STATISTICS_5MIN_RETENTION_DAYS=365   # 5-minute statistics
HAMMOND_STATISTICS_HOUR_RETENTION_DAYS=1825  # hourly statistics (5 years)
HAMMOND_TIMEZONE=Europe/Prague           # time zone of the schedules
HAMMOND_DAGSTER_BIND=127.0.0.1           # Dagster UI on loopback only
```

Verify the image signature like the others (README, `cosign verify`) and pin the digest.

## 3. Deploy

```bash
sudo -i
cd /opt/hammond
cp -r ~<admin>/hammond/deploy/compose/. /opt/hammond/   # keeps your .env
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
sudo docker compose -f /opt/hammond/compose.yaml exec dagster-daemon \
  hammond-ingest backfill --start 10d
sudo docker compose -f /opt/hammond/compose.yaml exec dagster-daemon hammond-ingest sync
```

Or in the UI: *Jobs → history_backfill → Launchpad*, config `start: "10d"`. The backfill
recomputes the affected silver range at the end.

## 6. Operations

| Task | Command (in `dagster-daemon`) |
| --- | --- |
| Run ingest + transformations now | `hammond-ingest sync` |
| Recompute silver for a range | `hammond-ingest restate --start 2026-01-01 --end 2026-02-01` |
| Retention / maintenance now | `hammond-ingest retention`, `hammond-ingest maintenance` |
| Stream status | `docker compose logs ingest-stream`, health check = flush heartbeat |

Notes:

* **Gaps.** After an outage of the VM or Home Assistant the stream catches up from the HA
  history API, up to `HAMMOND_STREAM_MAX_CATCHUP_DAYS` (10, the default recorder
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
