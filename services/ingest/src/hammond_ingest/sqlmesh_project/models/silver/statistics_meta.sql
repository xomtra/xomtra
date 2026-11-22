MODEL (
  name silver.statistics_meta,
  kind FULL,
  cron '@hourly',
  grain statistic_id,
  description 'Current metadata of each statistic; the unit is the one the values are stored in.',
  audits (
    unique_values(columns := (statistic_id))
  )
);

SELECT
  item_id::VARCHAR AS statistic_id,
  (payload ->> 'source')::VARCHAR AS source,
  (payload ->> 'name')::VARCHAR AS name,
  coalesce(
    payload ->> 'display_unit_of_measurement',
    payload ->> 'statistics_unit_of_measurement'
  )::VARCHAR AS unit_of_measurement,
  (payload ->> 'unit_class')::VARCHAR AS unit_class,
  (payload ->> 'has_mean')::BOOLEAN AS has_mean,
  (payload ->> 'has_sum')::BOOLEAN AS has_sum,
  TRY_CAST(payload ->> 'mean_type' AS INTEGER) AS mean_type
FROM silver.registry_current
WHERE
  registry = 'statistics_meta'
