MODEL (
  name silver.statistics,
  kind INCREMENTAL_BY_TIME_RANGE (
    time_column period_start,
    lookback 6,
    batch_size 720
  ),
  -- Same as bronze; see silver.states for why the partitioning is explicit.
  partitioned_by (period, year(period_start)),
  cron '@hourly',
  start '5 years ago',
  grain (statistic_id, period, period_start),
  description '5-minute and hourly statistics (mean/min/max for measurements, state/sum for counters).',
  audits (
    not_null(columns := (statistic_id, period, period_start)),
    unique_combination_of_columns(columns := (statistic_id, period, period_start)),
    accepted_values(column := period, is_in := ('5minute', 'hour'))
  )
);

SELECT
  s.statistic_id::VARCHAR AS statistic_id,
  s.period::VARCHAR AS period,
  s.period_start::TIMESTAMPTZ AS period_start,
  s.mean::DOUBLE AS mean,
  s.min::DOUBLE AS min,
  s.max::DOUBLE AS max,
  s.state::DOUBLE AS state,
  s.sum::DOUBLE AS sum,
  s.last_reset::TIMESTAMPTZ AS last_reset,
  s.source::VARCHAR AS source
FROM lake.bronze.ha_statistics AS s
WHERE
  s.period_start BETWEEN @start_ts AND @end_ts
QUALIFY
  row_number() OVER (PARTITION BY s.statistic_id, s.period, s.period_start ORDER BY s.ingested_at) = 1
