MODEL (
  name silver.registry_current,
  kind FULL,
  cron '@hourly',
  grain (registry, item_id),
  description 'Current version of every registry item (removed items excluded).'
);

SELECT
  registry::VARCHAR AS registry,
  item_id::VARCHAR AS item_id,
  payload::JSON AS payload,
  fetched_at::TIMESTAMPTZ AS valid_from
FROM (
  SELECT
    *
  FROM lake.bronze.ha_registry
  QUALIFY
    row_number() OVER (PARTITION BY registry, item_id ORDER BY fetched_at DESC) = 1
) AS latest
WHERE
  NOT removed
