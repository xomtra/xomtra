"""Analysis settings stored by the admin GUI (ADR 060), applied over the environment.

The admin GUI appends changes to ``analysis.setting_change`` in the audit database; the
ingest's ops role may only call ``analysis.current_settings()``. Every analysis run
reads them again, so a change needs no restart. If the database cannot be reached the
run goes on with the environment and the defaults, and says so in the log.
"""

from __future__ import annotations

import logging
from typing import Any

from hacore.analysis_settings import BY_KEY, Value, parse_stored
from hammond_ingest.settings import IngestSettings

logger = logging.getLogger(__name__)


def load_overrides(s: IngestSettings) -> dict[str, Value]:
    """Valid stored values by key; {} when there are none or the store is unavailable."""
    if not s.analysis_settings_db or not s.ops_pg_password.get_secret_value():
        return {}
    import psycopg2  # noqa: PLC0415 - only needed with a database

    try:
        with (
            psycopg2.connect(s.ops_pg_uri(s.analysis_settings_db), connect_timeout=5) as conn,
            conn.cursor() as cur,
        ):
            cur.execute("SELECT key, value FROM analysis.current_settings()")
            rows: list[tuple[str, Any]] = cur.fetchall()
    except psycopg2.Error as err:
        logger.warning("Stored analysis settings not read, using the environment: %s", err)
        return {}
    values, problems = parse_stored(rows)
    for problem in problems:
        logger.warning("Stored analysis setting %s", problem)
    return values


def with_overrides(s: IngestSettings, overrides: dict[str, Value] | None = None) -> IngestSettings:
    """``s`` with the stored analysis settings applied (stored > environment > default)."""
    values = load_overrides(s) if overrides is None else overrides
    values = {k: v for k, v in values.items() if k in BY_KEY}
    if not values:
        return s
    logger.info("Stored analysis settings: %s", values)
    data = s.model_dump()
    data.update(values)
    return type(s)(**data)
