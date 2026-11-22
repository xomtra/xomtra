MODEL (
  name silver.states,
  kind INCREMENTAL_BY_TIME_RANGE (
    time_column last_updated,
    -- re-read the last hours: the live stream and its catch-up may deliver late rows
    lookback 3,
    batch_size 168
  ),
  cron '@hourly',
  start '1 year ago',
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
