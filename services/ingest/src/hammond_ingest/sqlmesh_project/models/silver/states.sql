MODEL (
  name silver.states,
  kind INCREMENTAL_BY_TIME_RANGE (
    time_column last_updated,
    -- re-read the last hours: the live stream and its catch-up may deliver late rows
    lookback 3,
    -- 6 hours per query: a real installation writes ~35k state changes an hour with
    -- attributes; a week per batch needed more than 8 GB of memory (deduplication)
    batch_size 6
  ),
  -- Explicit coarse partitions: by default SQLMesh partitions an incremental model by
  -- its time column, which on DuckLake means one directory and file per distinct
  -- timestamp (hundreds of thousands of files per batch, memory exhaustion).
  partitioned_by year(last_updated),
  cron '@hourly',
  -- HAmmond only has what the HA recorder still holds (10 days by default, ADR 033);
  -- an earlier start would only add empty 6-hour batches to the first plan.
  start '14 days ago',
  grain (entity_id, last_updated),
  description 'Every state change of every entity, one row per (entity_id, last_updated).',
  audits (
    not_null(columns := (entity_id, last_updated)),
    unique_combination_of_columns(columns := (entity_id, last_updated))
  )
);

SELECT
  entity_id::VARCHAR AS entity_id,
  split_part(entity_id, '.', 1)::VARCHAR AS domain,
  state::VARCHAR AS state,
  -- numeric value for measurements; NULL for text states and unavailable/unknown
  TRY_CAST(state AS DOUBLE) AS state_value,
  (state IS NULL OR state IN ('unavailable', 'unknown'))::BOOLEAN AS is_missing,
  attributes::JSON AS attributes,
  last_changed::TIMESTAMPTZ AS last_changed,
  last_updated::TIMESTAMPTZ AS last_updated,
  context_id::VARCHAR AS context_id,
  source::VARCHAR AS source
FROM lake.bronze.ha_states
WHERE
  last_updated BETWEEN @start_ts AND @end_ts
QUALIFY
  row_number() OVER (PARTITION BY entity_id, last_updated ORDER BY ingested_at) = 1
