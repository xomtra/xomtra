"""Entity catalog: profiling of every entity and detection of drift (ADR 037).

Tables (schema ``catalog`` in DuckLake):

``entity``
    One row per entity: descriptive metadata from ``silver.entities`` plus a profile of
    its recent state history (``window_days``, default 30): how often it changes, share
    of numeric and missing values, distinct states, value quantiles, a derived ``kind``
    and flags ``stale`` (silent for 48 h although it reported daily) and ``range_shift``
    (median of the last 7 days outside the 5-95 % range of the 23 days before).
    Replaced as a whole by every run.
``drift``
    Append-only log of changes between two consecutive profiles: new and removed
    entities, metadata changes (unit, device class, state class, area, name, kind),
    an entity that stopped or resumed reporting and a shift of its value range.

The profile reads the silver views (``silver.entities``, ``silver.states``,
``silver.statistics_meta``) and writes in one transaction, so readers always see a
consistent catalog. Run daily by the Dagster job ``catalog_daily``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import duckdb

from hacore.lake import LAKE_ALIAS

logger = logging.getLogger(__name__)

SCHEMA = "catalog"
ENTITY = "entity"
DRIFT = "drift"

DEFAULT_WINDOW = timedelta(days=30)
# An entity that reported at least once a day is "stale" after this much silence.
STALE_AFTER = timedelta(hours=48)
# Recent period whose median is compared to the 5-95 % range of the rest of the window
# (the baseline); a median outside it is a "range_shift".
RECENT = timedelta(days=7)
# Fields whose change is reported as metadata drift.
TRACKED_FIELDS = (
    "name",
    "area_name",
    "unit_of_measurement",
    "device_class",
    "state_class",
    "kind",
)
KINDS = ("measurement", "counter", "binary", "enum", "event", "text", "unknown")
BINARY_DOMAINS = (
    "binary_sensor",
    "input_boolean",
    "switch",
    "light",
    "fan",
    "siren",
    "remote",
    "automation",
    "script",
)
EVENT_DOMAINS = ("event", "button", "input_button", "scene", "notify")

ENTITY_COLUMNS = """
    entity_id VARCHAR NOT NULL,
    domain VARCHAR,
    name VARCHAR,
    platform VARCHAR,
    device_name VARCHAR,
    manufacturer VARCHAR,
    model VARCHAR,
    area_name VARCHAR,
    floor_name VARCHAR,
    label_ids VARCHAR[],
    unit_of_measurement VARCHAR,
    device_class VARCHAR,
    state_class VARCHAR,
    entity_category VARCHAR,
    disabled BOOLEAN,
    hidden BOOLEAN,
    in_registry BOOLEAN,
    has_statistics BOOLEAN,
    statistics_unit VARCHAR,
    kind VARCHAR,
    first_seen TIMESTAMPTZ,
    last_seen TIMESTAMPTZ,
    changes BIGINT,
    changes_per_day DOUBLE,
    numeric_share DOUBLE,
    missing_share DOUBLE,
    distinct_states BIGINT,
    top_states STRUCT(state VARCHAR, n BIGINT)[],
    value_min DOUBLE,
    value_p05 DOUBLE,
    value_p50 DOUBLE,
    value_p95 DOUBLE,
    value_max DOUBLE,
    value_mean DOUBLE,
    recent_p50 DOUBLE,
    baseline_p05 DOUBLE,
    baseline_p95 DOUBLE,
    stale BOOLEAN,
    range_shift BOOLEAN,
    window_days INTEGER,
    profiled_at TIMESTAMPTZ
"""

DRIFT_COLUMNS = """
    detected_at TIMESTAMPTZ NOT NULL,
    entity_id VARCHAR NOT NULL,
    drift_type VARCHAR NOT NULL,
    field VARCHAR,
    old_value VARCHAR,
    new_value VARCHAR
"""


@dataclass(slots=True)
class ProfileResult:
    entities: int = 0
    drift: dict[str, int] = field(default_factory=dict)


def ensure_schema(con: duckdb.DuckDBPyConnection, alias: str = LAKE_ALIAS) -> None:
    con.execute(f"CREATE SCHEMA IF NOT EXISTS {alias}.{SCHEMA}")
    con.execute(f"CREATE TABLE IF NOT EXISTS {alias}.{SCHEMA}.{ENTITY} ({ENTITY_COLUMNS})")
    con.execute(f"CREATE TABLE IF NOT EXISTS {alias}.{SCHEMA}.{DRIFT} ({DRIFT_COLUMNS})")


def _profile_sql(alias: str) -> str:
    binary = ", ".join(f"'{d}'" for d in BINARY_DOMAINS)
    events = ", ".join(f"'{d}'" for d in EVENT_DOMAINS)
    s = f"{alias}.silver"
    # Parameters: $since, $now, $recent_since, $stale_before, $window_days
    return f"""
WITH st AS (
  SELECT entity_id, domain, state, state_value, is_missing, last_updated
  FROM {s}.states
  WHERE last_updated >= $since AND last_updated <= $now
), agg AS (
  SELECT
    entity_id,
    any_value(domain) AS domain,
    min(last_updated) AS first_seen,
    max(last_updated) AS last_seen,
    count(*) AS changes,
    avg(CASE WHEN state_value IS NOT NULL THEN 1.0 ELSE 0.0 END) AS numeric_share,
    avg(CASE WHEN is_missing THEN 1.0 ELSE 0.0 END) AS missing_share,
    approx_count_distinct(state) FILTER (WHERE NOT is_missing) AS distinct_states,
    min(state_value) AS value_min,
    approx_quantile(state_value, 0.05) AS value_p05,
    approx_quantile(state_value, 0.5) AS value_p50,
    approx_quantile(state_value, 0.95) AS value_p95,
    max(state_value) AS value_max,
    avg(state_value) AS value_mean,
    approx_quantile(state_value, 0.5) FILTER (WHERE last_updated >= $recent_since)
      AS recent_p50,
    approx_quantile(state_value, 0.05) FILTER (WHERE last_updated < $recent_since)
      AS baseline_p05,
    approx_quantile(state_value, 0.95) FILTER (WHERE last_updated < $recent_since)
      AS baseline_p95,
    count(state_value) FILTER (WHERE last_updated < $recent_since) AS baseline_n,
    count(state_value) AS numeric_n,
    count(*) FILTER (WHERE last_updated >= $recent_since AND state_value IS NOT NULL)
      AS recent_n
  FROM st
  GROUP BY entity_id
), counts AS (
  SELECT entity_id, state, count(*) AS n
  FROM st
  WHERE NOT is_missing
  GROUP BY entity_id, state
), top AS (
  SELECT entity_id, list({{'state': state, 'n': n}} ORDER BY n DESC, state) AS top_states
  FROM (
    SELECT *, row_number() OVER (PARTITION BY entity_id ORDER BY n DESC, state) AS rn
    FROM counts
  )
  WHERE rn <= 5
  GROUP BY entity_id
), ids AS (
  SELECT entity_id FROM {s}.entities
  UNION
  SELECT entity_id FROM agg
)
SELECT
  i.entity_id,
  coalesce(e.domain, a.domain, split_part(i.entity_id, '.', 1)) AS domain,
  e.name,
  e.platform,
  e.device_name,
  e.manufacturer,
  e.model,
  e.area_name,
  e.floor_name,
  e.label_ids,
  e.unit_of_measurement,
  e.device_class,
  e.state_class,
  e.entity_category,
  coalesce(e.disabled, FALSE) AS disabled,
  coalesce(e.hidden, FALSE) AS hidden,
  coalesce(e.in_registry, FALSE) AS in_registry,
  coalesce(e.has_statistics, FALSE) AS has_statistics,
  m.unit_of_measurement AS statistics_unit,
  CASE
    WHEN e.state_class IN ('total', 'total_increasing') THEN 'counter'
    WHEN e.state_class = 'measurement' THEN 'measurement'
    WHEN coalesce(e.domain, a.domain) IN ({events}) THEN 'event'
    WHEN coalesce(e.domain, a.domain) IN ({binary}) THEN 'binary'
    WHEN a.changes IS NULL THEN 'unknown'
    WHEN a.numeric_share >= 0.9 * (1 - a.missing_share) AND a.numeric_n > 0
      THEN 'measurement'
    WHEN a.distinct_states <= 2 THEN 'binary'
    WHEN a.distinct_states <= 20 THEN 'enum'
    ELSE 'text'
  END AS kind,
  a.first_seen,
  a.last_seen,
  coalesce(a.changes, 0) AS changes,
  coalesce(a.changes, 0) / $window_days AS changes_per_day,
  a.numeric_share,
  a.missing_share,
  a.distinct_states,
  t.top_states,
  a.value_min,
  a.value_p05,
  a.value_p50,
  a.value_p95,
  a.value_max,
  a.value_mean,
  a.recent_p50,
  a.baseline_p05,
  a.baseline_p95,
  (coalesce(a.changes, 0) / $window_days >= 1 AND a.last_seen < $stale_before) AS stale,
  (
    a.baseline_n >= 100 AND a.recent_n >= 20 AND a.baseline_p95 > a.baseline_p05
    AND (a.recent_p50 < a.baseline_p05 OR a.recent_p50 > a.baseline_p95)
  ) AS range_shift,
  $window_days AS window_days,
  $now AS profiled_at
FROM ids AS i
LEFT JOIN {s}.entities AS e ON e.entity_id = i.entity_id
LEFT JOIN agg AS a ON a.entity_id = i.entity_id
LEFT JOIN top AS t ON t.entity_id = i.entity_id
LEFT JOIN {s}.statistics_meta AS m ON m.statistic_id = i.entity_id
"""


def _drift_sql(alias: str) -> str:
    c = f"{alias}.{SCHEMA}"
    meta = "\n  UNION ALL\n".join(
        f"""  SELECT n.entity_id, 'metadata_change', '{f}', o.{f}::VARCHAR, n.{f}::VARCHAR
  FROM new_profile AS n JOIN {c}.{ENTITY} AS o USING (entity_id)
  WHERE o.{f} IS DISTINCT FROM n.{f}"""
        for f in TRACKED_FIELDS
    )
    return f"""
INSERT INTO {c}.{DRIFT}
SELECT $now, entity_id, drift_type, field, old_value, new_value FROM (
  SELECT n.entity_id, 'new_entity' AS drift_type, NULL AS field, NULL AS old_value,
         n.kind AS new_value
  FROM new_profile AS n ANTI JOIN {c}.{ENTITY} AS o USING (entity_id)
  UNION ALL
  SELECT o.entity_id, 'removed_entity', NULL, o.kind, NULL
  FROM {c}.{ENTITY} AS o ANTI JOIN new_profile AS n USING (entity_id)
  UNION ALL
{meta}
  UNION ALL
  SELECT n.entity_id, CASE WHEN n.stale THEN 'stopped_reporting' ELSE 'resumed_reporting' END,
         'last_seen', o.last_seen::VARCHAR, n.last_seen::VARCHAR
  FROM new_profile AS n JOIN {c}.{ENTITY} AS o USING (entity_id)
  WHERE coalesce(o.stale, FALSE) <> coalesce(n.stale, FALSE)
  UNION ALL
  SELECT n.entity_id, 'range_shift', 'recent_p50',
         printf('%g..%g', n.baseline_p05, n.baseline_p95), n.recent_p50::VARCHAR
  FROM new_profile AS n JOIN {c}.{ENTITY} AS o USING (entity_id)
  WHERE coalesce(n.range_shift, FALSE) AND NOT coalesce(o.range_shift, FALSE)
)
"""


def profile(
    con: duckdb.DuckDBPyConnection,
    *,
    now: datetime | None = None,
    window: timedelta = DEFAULT_WINDOW,
    alias: str = LAKE_ALIAS,
) -> ProfileResult:
    """Profile all entities and record the drift against the previous profile.

    The first run (empty catalog) records no drift: every entity would be "new".
    """
    ensure_schema(con, alias)
    now = (now or datetime.now(UTC)).astimezone(UTC)
    params = {
        "since": now - window,
        "now": now,
        "recent_since": now - RECENT,
        "stale_before": now - STALE_AFTER,
        "window_days": max(window / timedelta(days=1), 1.0),
    }
    c = f"{alias}.{SCHEMA}"
    result = ProfileResult()
    con.execute(f"CREATE OR REPLACE TEMP TABLE new_profile AS {_profile_sql(alias)}", params)
    try:
        con.execute("BEGIN TRANSACTION")
        try:
            first = con.execute(f"SELECT count(*) FROM {c}.{ENTITY}").fetchone()
            if first is not None and first[0] > 0:
                con.execute(_drift_sql(alias), {"now": now})
            con.execute(f"DELETE FROM {c}.{ENTITY}")
            con.execute(f"INSERT INTO {c}.{ENTITY} BY NAME SELECT * FROM new_profile")
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        row = con.execute("SELECT count(*) FROM new_profile").fetchone()
        result.entities = int(row[0]) if row else 0
        rows = con.execute(
            f"SELECT drift_type, count(*) FROM {c}.{DRIFT} WHERE detected_at = $now "
            "GROUP BY ALL ORDER BY 1",
            {"now": now},
        ).fetchall()
        result.drift = {str(t): int(n) for t, n in rows}
    finally:
        con.execute("DROP TABLE IF EXISTS new_profile")
    logger.info("Catalog profiled: %d entities, drift %s", result.entities, result.drift)
    return result
