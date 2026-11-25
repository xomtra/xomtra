# xomtra-ingest

Ingest of Home Assistant data into the Xomtra lake (full edition):

* `xomtra-ingest stream` – live `state_changed` stream with catch-up (service);
* Dagster code location `xomtra_ingest.definitions` – hourly ingest + SQLMesh, daily
  retention, manual backfill / restate jobs;
* SQLMesh project `xomtra_ingest/sqlmesh_project` – silver models on DuckLake.

The ingest logic itself (HA API, bronze writers, stream) lives in
`hacore` so that the lite edition can reuse it. Deployment and operations:
[docs/setup/ingest.md](../../docs/setup/ingest.md).
