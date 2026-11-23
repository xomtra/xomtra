MODEL (
  name silver.entities,
  kind FULL,
  cron '@hourly',
  grain entity_id,
  description 'Current entities with device, effective area, floor, labels and descriptive attributes.',
  audits (
    not_null(columns := (entity_id)),
    unique_values(columns := (entity_id))
  )
);

WITH reg AS (
  SELECT registry, item_id, payload FROM silver.registry_current
), entities AS (
  SELECT
    payload ->> 'entity_id' AS entity_id,
    item_id AS registry_id,
    payload ->> 'platform' AS platform,
    payload ->> 'device_id' AS device_id,
    payload ->> 'area_id' AS area_id,
    coalesce(payload ->> 'name', payload ->> 'original_name') AS registry_name,
    payload ->> 'entity_category' AS entity_category,
    (payload ->> 'disabled_by') IS NOT NULL AS disabled,
    (payload ->> 'hidden_by') IS NOT NULL AS hidden,
    CAST(payload -> 'labels' AS VARCHAR[]) AS labels
  FROM reg
  WHERE registry = 'entities'
), attrs AS (
  SELECT
    item_id AS entity_id,
    payload ->> 'friendly_name' AS friendly_name,
    payload ->> 'unit_of_measurement' AS unit_of_measurement,
    payload ->> 'device_class' AS device_class,
    payload ->> 'state_class' AS state_class
  FROM reg
  WHERE registry = 'entity_attributes'
), devices AS (
  SELECT
    item_id AS device_id,
    coalesce(payload ->> 'name_by_user', payload ->> 'name') AS device_name,
    payload ->> 'manufacturer' AS manufacturer,
    payload ->> 'model' AS model,
    payload ->> 'area_id' AS area_id,
    CAST(payload -> 'labels' AS VARCHAR[]) AS labels
  FROM reg
  WHERE registry = 'devices'
), areas AS (
  SELECT item_id AS area_id, payload ->> 'name' AS area_name, payload ->> 'floor_id' AS floor_id
  FROM reg
  WHERE registry = 'areas'
), floors AS (
  SELECT item_id AS floor_id, payload ->> 'name' AS floor_name
  FROM reg
  WHERE registry = 'floors'
), stats AS (
  SELECT item_id AS statistic_id FROM reg WHERE registry = 'statistics_meta'
), all_ids AS (
  SELECT entity_id FROM entities
  UNION
  SELECT entity_id FROM attrs
)
SELECT
  i.entity_id::VARCHAR AS entity_id,
  split_part(i.entity_id, '.', 1)::VARCHAR AS domain,
  coalesce(e.registry_name, a.friendly_name)::VARCHAR AS name,
  e.platform::VARCHAR AS platform,
  e.device_id::VARCHAR AS device_id,
  d.device_name::VARCHAR AS device_name,
  d.manufacturer::VARCHAR AS manufacturer,
  d.model::VARCHAR AS model,
  coalesce(e.area_id, d.area_id)::VARCHAR AS area_id,
  ar.area_name::VARCHAR AS area_name,
  ar.floor_id::VARCHAR AS floor_id,
  f.floor_name::VARCHAR AS floor_name,
  list_distinct(list_concat(coalesce(e.labels, []), coalesce(d.labels, [])))::VARCHAR[] AS label_ids,
  a.unit_of_measurement::VARCHAR AS unit_of_measurement,
  a.device_class::VARCHAR AS device_class,
  a.state_class::VARCHAR AS state_class,
  e.entity_category::VARCHAR AS entity_category,
  coalesce(e.disabled, FALSE)::BOOLEAN AS disabled,
  coalesce(e.hidden, FALSE)::BOOLEAN AS hidden,
  (e.entity_id IS NOT NULL)::BOOLEAN AS in_registry,
  (s.statistic_id IS NOT NULL)::BOOLEAN AS has_statistics
FROM all_ids AS i
LEFT JOIN entities AS e
  ON e.entity_id = i.entity_id
LEFT JOIN attrs AS a
  ON a.entity_id = i.entity_id
LEFT JOIN devices AS d
  ON d.device_id = e.device_id
LEFT JOIN areas AS ar
  ON ar.area_id = coalesce(e.area_id, d.area_id)
LEFT JOIN floors AS f
  ON f.floor_id = ar.floor_id
LEFT JOIN stats AS s
  ON s.statistic_id = i.entity_id
