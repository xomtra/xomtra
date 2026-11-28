# Back up and restore the Docker Compose installation

How to back up xomtra installed with [Docker Compose](docker.md) and how to restore it,
on the same machine or on a new one. (The Home Assistant add-on is backed up by Home
Assistant, without the lake: see [the add-on](home-assistant-addon.md).)

## What to back up

| Docker volume | Content | Needed |
| --- | --- | --- |
| `xomtra_secrets` | every internal password and key; `XOMTRA_SETTINGS_KEY` decrypts the stored Home Assistant token | **yes**: without it the databases and the Home Assistant connection cannot be opened |
| `xomtra_postgres-data` | Postgres: the DuckLake catalog (database `ducklake`: which Parquet files make up which table and snapshot), tokens, sessions, settings, answer sets, the Home Assistant connection and the audit (`audit`), Dagster runs (`dagster`), SQLMesh state (`sqlmesh`) | **yes** |
| `xomtra_seaweedfs-data` | the lake: Parquet data files and delete files in SeaweedFS (S3) | **yes**: the history |
| `xomtra_caddy-data` | Caddy's local certificate authority and certificates | yes, unless you want clients to trust a new CA |
| `.env` in `deploy/compose` | your settings, if you have one (may hold the Home Assistant token) | yes |
| `xomtra_caddy-config`, `xomtra_mcp-data`, `xomtra_ingest-data` | Caddy's autosaved config, temporary query files, Dagster logs | no |

All data rows are in the lake; Postgres holds no Home Assistant data except the audit of
tool calls. Data inlining is off (`DATA_INLINING_ROW_LIMIT 0`), so the catalog holds no
rows either.

## Why the order matters

The two parts belong together. The catalog in Postgres lists the Parquet files of every
table; the files are in SeaweedFS. DuckLake never changes a Parquet file: inserts,
deletes (as delete files), compaction (`merge_adjacent_files`) and rewrites
(`rewrite_data_files`) always write new files. Old files are deleted only by the daily
job `retention_daily` (03:30 in `XOMTRA_TIMEZONE`, UTC by default), and only files that
no snapshot still uses and that were scheduled for deletion more than 7 days earlier
(snapshots are kept for 7 days). The same job deletes files that no catalog entry
knows (orphans of aborted writes) once they are older than 7 days.

So a backup taken while xomtra runs is consistent when:

1. **the catalog is dumped first and the files are copied afterwards.** The copy then
   has every file the dumped catalog needs, plus perhaps a few newer ones that the
   restored catalog does not know (orphans; maintenance deletes them 7 days after the
   restore). The other way round the catalog lists files that are missing in the copy
   and queries of the affected tables fail;
2. **both parts are taken in one go,** at a time when `retention_daily` does not run
   (not around 03:30). Even if it does, a file the dump needs is deleted at the
   earliest 7 days after it stopped being used, so a backup finished within hours is
   safe.

Copy the lake through its S3 interface (below), not the files of the running
SeaweedFS volume: SeaweedFS keeps its own index and metadata next to the data, and a
copy of them taken while it writes can be torn. A copy of the volume is fine when the
stack is stopped (cold backup).

A snapshot of the whole virtual machine or disk (Proxmox Backup Server with the QEMU
guest agent, an LVM or ZFS snapshot) takes all volumes at one moment and is also
consistent; after a restore Postgres recovers like after a power cut.

## Cold backup (simplest)

Stop the stack for the time of the copy (minutes), copy the volumes, start it again.
Nothing is lost: after the start the live stream fills the gap from the Home Assistant
history (catch-up, up to 10 days), and the hourly ingest picks up the statistics.

In `xomtra/deploy/compose`:

```bash
B=~/xomtra-backup/$(date +%F-%H%M); mkdir -p "$B"
docker compose stop
for v in secrets postgres-data seaweedfs-data caddy-data; do
  docker run --rm -v xomtra_$v:/v:ro -v "$B":/b alpine tar czf /b/$v.tgz -C /v .
done
[ ! -f .env ] || cp .env "$B/"   # if you have one
docker compose up -d
ls -lh "$B"
```

### Restore a cold backup

On the same version of xomtra as the backup (upgrade afterwards). On a new machine
clone that release first ([Docker Compose, step 1](docker.md#1-start)) without starting
it; on the same machine remove the old stack first with `docker compose down -v`
(this deletes its volumes).

```bash
B=~/xomtra-backup/<date>                 # the backup to restore
[ ! -f "$B/.env" ] || cp "$B/.env" .    # if the backup has one
docker compose create                    # creates the containers and empty volumes
for v in secrets postgres-data seaweedfs-data caddy-data; do
  docker run --rm -v xomtra_$v:/v -v "$B":/b:ro alpine tar xzf /b/$v.tgz -C /v
done
docker compose up -d
```

## Hot backup (xomtra keeps running)

The order is fixed: the other databases, then the catalog `ducklake`, then the lake
files, then the keys. The databases other than `ducklake` go first because a state that
is older than the lake is safe: SQLMesh and the ingest only redo an interval they do
not know is done, and every run is idempotent. The lake is copied with
[rclone](https://rclone.org) in its official container, over the internal network of
the stack, with the S3 keys from the volume `secrets`.

In `xomtra/deploy/compose` (if you changed `S3_BUCKET` in `.env`, use that name
instead of `xomtra` in `lake:xomtra`):

```bash
B=~/xomtra-backup/$(date +%F-%H%M); mkdir -p "$B/lake"
RCLONE=rclone/rclone:1.71.2@sha256:3103526c506266a9ecdf064efe99bf3677d92ef6407af124d8c56b4f49cbaa51
lake_rclone() {   # lake_rclone <rclone arguments>: rclone with the lake as "lake:"
  docker run --rm --network xomtra_backend -v xomtra_secrets:/secrets:ro -v "$B/lake":/backup \
    -e RCLONE_CONFIG_LAKE_TYPE=s3 -e RCLONE_CONFIG_LAKE_PROVIDER=SeaweedFS \
    -e RCLONE_CONFIG_LAKE_ENDPOINT=http://seaweedfs:8333 \
    --entrypoint sh "$RCLONE" -c '. /secrets/seaweedfs.env &&
      export RCLONE_CONFIG_LAKE_ACCESS_KEY_ID="$S3_ACCESS_KEY" RCLONE_CONFIG_LAKE_SECRET_ACCESS_KEY="$S3_SECRET_KEY" &&
      exec rclone "$@"' rclone "$@"
}

# 1. roles, then every database, the catalog "ducklake" last
docker compose exec -T postgres pg_dumpall -U postgres --globals-only > "$B/globals.sql"
for db in $(docker compose exec -T postgres psql -U postgres -Atc \
    "SELECT datname FROM pg_database WHERE NOT datistemplate AND datname NOT IN ('postgres', 'ducklake')") ducklake; do
  docker compose exec -T postgres pg_dump -U postgres -Fc "$db" > "$B/$db.dump"
done
# 2. then the lake files
lake_rclone sync lake:xomtra /backup
# 3. the keys and the local CA (they change only at start or on a certificate renewal)
for v in secrets caddy-data; do
  docker run --rm -v xomtra_$v:/v:ro -v "$B":/b alpine tar czf /b/$v.tgz -C /v .
done
[ ! -f .env ] || cp .env "$B/"   # if you have one
ls -lh "$B"
```

For daily backups run it from cron at a time away from 03:30 and keep several complete
backup directories. Do not let two backups share one lake directory: `rclone sync`
deletes there the files that maintenance deleted in the lake, and the catalog dump of
the older backup still needs them.

### Restore a hot backup

As for the cold backup: the same version of xomtra, on a new machine the cloned release
without starting it, on the same machine after `docker compose down -v`. Define `B`,
`RCLONE` and `lake_rclone` as above, with `B` pointing to the backup.

```bash
[ ! -f "$B/.env" ] || cp "$B/.env" .    # if the backup has one
docker compose create                    # creates the containers and empty volumes
for v in secrets caddy-data; do
  docker run --rm -v xomtra_$v:/v -v "$B":/b:ro alpine tar xzf /b/$v.tgz -C /v
done
# an empty Postgres and the empty bucket, nothing else yet
docker compose up -d postgres seaweedfs-init
# "role "postgres" already exists" is expected
docker compose exec -T postgres psql -q -U postgres < "$B/globals.sql"
for f in "$B"/*.dump; do
  docker compose exec -T postgres pg_restore -U postgres --create -d postgres < "$f"
done
lake_rclone sync /backup lake:xomtra
docker compose up -d
```

## After a restore

- The history ends at the time of the backup. The live stream fills the gap from Home
  Assistant as far back as its recorder still has it (10 days by default); older gaps
  stay.
- Tokens, sign-ins, settings and answer sets are those of the backup; tokens issued
  later no longer work.
- On a new machine with another address set `XOMTRA_LOCAL_HOST` if needed and run
  `docker compose up -d --force-recreate` ([Optional settings](docker.md#optional-settings-env)).
  With `caddy-data` restored the clients keep trusting the server.
- Check the admin GUI: Overview (Home Assistant connected, ingest running) and Catalog.

## Keep the backup safe

`secrets.tgz` contains every internal password and key, `globals.sql` the password
hashes of the database roles, and `.env` may contain the Home Assistant token. The lake
and the databases hold the history of your home. Keep the backup like a password: not
in a shared folder, encrypted if it leaves the machine. Afterwards
`docker image rm alpine "$RCLONE"` removes the helper images.
