# Data ingest (phase 1a.2)

The ingest copies the history of Home Assistant into the lake and keeps it current:

| Part | What it does | Runs as |
| --- | --- | --- |
| Live stream | every `state_changed` event into `bronze.ha_states`; after a reconnect it fills the gap from the HA history API (catch-up) | service `ingest-stream` |
| Hourly ingest | registries and statistics metadata (change log), 5-minute and hourly statistics, then SQLMesh `bronze → silver` | Dagster job `ingest_hourly`, every hour at :07 |
| Retention | deletes rows older than `HAMMOND_RETENTION_DAYS` (default 365), compacts files, expires snapshots | Dagster job `retention_daily`, 03:30 |
| Backfill | the state history Home Assistant still has (recorder, 10 days by default) | Dagster job `history_backfill` or CLI, once |
| Backup import | older history from copies of the recorder database (e.g. HA VM backups in PBS) | Dagster job `recorder_import` or CLI, as needed |

Everything is idempotent: rows are keyed by `(entity_id, last_updated)` for states and
`(statistic_id, period, period_start)` for statistics, so repeated runs, overlapping
backups and the catch-up never create duplicates.

Layers in the lake (DuckLake, schema per layer):

* `bronze.ha_states`, `bronze.ha_statistics`, `bronze.ha_registry` – raw data with
  `source` (`ha_live`, `ha_history`, `ha_api`, `recorder_import`) and ingest time.
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
HAMMOND_RETENTION_DAYS=365
HAMMOND_TIMEZONE=Europe/Prague           # time zone of the schedules
HAMMOND_DAGSTER_BIND=127.0.0.1           # Dagster UI on loopback only
HAMMOND_IMPORT_DIR=./import              # recorder database copies to import
```

Verify the image signature like the others (README, `cosign verify`) and pin the digest.

## 3. Deploy

```bash
sudo -i
cd /opt/hammond
cp -r ~<admin>/hammond/deploy/compose/. /opt/hammond/   # keeps your .env
install -d -o 10001 -g 10001 -m 700 /opt/hammond/import
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

The UI is published on the VM's loopback only. From the admin machine:

```bash
ssh -L 3000:127.0.0.1:3000 <admin>@<vm-ip>
# then open http://127.0.0.1:3000
```

The schedules `ingest_hourly` and `retention_daily` are active from the start. Jobs
run one at a time (queued run coordinator); the live stream runs next to them.

## 5. First backfill

Copy what Home Assistant still has in its recorder (default 10 days of states, 10 days
of 5-minute statistics, hourly statistics for up to a year):

```bash
sudo docker compose -f /opt/hammond/compose.yaml exec dagster-daemon \
  hammond-ingest backfill --start 10d
sudo docker compose -f /opt/hammond/compose.yaml exec dagster-daemon hammond-ingest sync
```

Or in the UI: *Jobs → history_backfill → Launchpad*, config `start: "10d"`. The backfill
recomputes the affected silver range at the end.

## 6. Older history from backups (PBS)

Home Assistant purges states after `purge_keep_days`, so anything older exists only in
backups. Each backup contains the recorder database `home-assistant_v2.db`; import as
many backups as you like, in any order. Overlaps are skipped.

Get a copy of the database out of a backup, for example:

* **HA VM backup in Proxmox Backup Server:** in the Proxmox UI open the backup →
  *File Restore* → the data partition (`hassos-data`) →
  `supervisor/homeassistant/home-assistant_v2.db` (plus `-wal` if present) → *Download*.
* **Home Assistant backup (`.tar`):** extract `homeassistant.tar.gz` from it and take
  `data/home-assistant_v2.db`.

Copy the file(s) to the import directory on the VM and run the import:

```bash
scp home-assistant_v2.db* <admin>@<vm-ip>:/tmp/
sudo install -o 10001 -g 10001 -m 600 /tmp/home-assistant_v2.db* /opt/hammond/import/
sudo docker compose -f /opt/hammond/compose.yaml exec dagster-daemon \
  hammond-ingest import-recorder /import/home-assistant_v2.db
sudo rm /opt/hammond/import/home-assistant_v2.db* /tmp/home-assistant_v2.db*
```

The import reads states, 5-minute and hourly statistics and the statistics metadata of
removed sensors, writes them to bronze and then restates silver for the imported range.
Supported are recorder databases of Home Assistant 2023.4 and newer (schema 43+), SQLite
only. `--start` / `--end` limit the range. The directory must be writable for the
container user when a `-wal` file is present (SQLite creates its index file next to it).

Rows older than `HAMMOND_RETENTION_DAYS` are removed again by the next retention run,
so raise the retention first if you want to keep older backups in the lake.

## 7. Operations

| Task | Command (in `dagster-daemon`) |
| --- | --- |
| Run ingest + transformations now | `hammond-ingest sync` |
| Recompute silver for a range | `hammond-ingest restate --start 2026-01-01 --end 2026-02-01` |
| Retention / maintenance now | `hammond-ingest retention`, `hammond-ingest maintenance` |
| Stream status | `docker compose logs ingest-stream`, health check = flush heartbeat |

Notes:

* **Gaps.** After an outage of the VM or Home Assistant the stream catches up from the HA
  history API, up to `HAMMOND_STREAM_MAX_CATCHUP_DAYS` (10). Longer gaps need a backup
  import.
* **Attributes.** Live events carry all attributes; the HA recorder drops some
  ("unrecorded" attributes such as `min`/`max` of `input_number`). Rows that came from
  history or backups can therefore have fewer attributes than live rows.
* **Units.** Statistics from the API are in the display unit reported in
  `silver.statistics_meta`; statistics from backups are in the unit the recorder stored.
* **Deleting data.** Retention deletes rows at once; the files disappear from object
  storage after the snapshot retention (7 days), when the maintenance job expires the
  snapshots that still reference them.
