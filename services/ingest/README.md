# hammond-ingest

Ingest of Home Assistant data into the HAmmond lake (full edition):

* `hammond-ingest stream` – live `state_changed` stream with catch-up (service);
* Dagster code location `hammond_ingest.definitions` – hourly ingest + SQLMesh, daily
  retention, manual backfill / backup import / restate jobs;
* SQLMesh project `hammond_ingest/sqlmesh_project` – silver models on DuckLake.

The ingest logic itself (HA API, bronze writers, recorder import, stream) lives in
`hacore` so that the lite edition can reuse it. Deployment and operations:
[docs/setup/ingest.md](../../docs/setup/ingest.md).
