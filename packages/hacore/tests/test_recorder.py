import sqlite3
from pathlib import Path

import duckdb
import pytest

from hacore import bronze
from hacore.history import StateRow, to_us
from hacore.recorder import RecorderSchemaError, import_recorder_db

T = 1_767_261_600.123456  # 2026-01-01 10:00:00.123456 UTC

SCHEMA = """
CREATE TABLE schema_changes (change_id INTEGER PRIMARY KEY, schema_version INTEGER);
CREATE TABLE states_meta (metadata_id INTEGER PRIMARY KEY, entity_id TEXT);
CREATE TABLE state_attributes (attributes_id INTEGER PRIMARY KEY, hash INTEGER, shared_attrs TEXT);
CREATE TABLE states (
  state_id INTEGER PRIMARY KEY, entity_id TEXT, state TEXT, attributes TEXT,
  last_changed_ts FLOAT, last_updated_ts FLOAT, attributes_id INTEGER, metadata_id INTEGER
);
CREATE TABLE statistics_meta (
  id INTEGER PRIMARY KEY, statistic_id TEXT, source TEXT, unit_of_measurement TEXT,
  unit_class TEXT, has_mean INTEGER, has_sum INTEGER, name TEXT, mean_type INTEGER
);
CREATE TABLE statistics (
  id INTEGER PRIMARY KEY, metadata_id INTEGER, start_ts FLOAT, mean FLOAT, min FLOAT,
  max FLOAT, last_reset_ts FLOAT, state FLOAT, sum FLOAT
);
CREATE TABLE statistics_short_term (
  id INTEGER PRIMARY KEY, metadata_id INTEGER, start_ts FLOAT, mean FLOAT, min FLOAT,
  max FLOAT, last_reset_ts FLOAT, state FLOAT, sum FLOAT
);
"""


def make_db(path: Path, version: int = 50) -> Path:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.execute("INSERT INTO schema_changes VALUES (1, ?)", (version,))
    con.execute("INSERT INTO states_meta VALUES (1, 'sensor.t'), (2, 'switch.s')")
    con.execute("""INSERT INTO state_attributes VALUES (1, 0, '{"unit_of_measurement":"°C"}')""")
    rows = [(i + 1, None, str(20 + i), None, None, T + i * 60, 1, 1) for i in range(10)] + [
        (100, None, "on", None, T, T + 5, None, 2)
    ]
    con.executemany("INSERT INTO states VALUES (?,?,?,?,?,?,?,?)", rows)
    con.execute(
        "INSERT INTO statistics_meta VALUES (1, 'sensor.t', 'recorder', '°C', 'temperature', 1, 0,"
        " NULL, 1)"
    )
    con.execute("INSERT INTO statistics VALUES (1, 1, ?, 21.0, 20.0, 22.0, NULL, NULL, NULL)", (T,))
    con.executemany(
        "INSERT INTO statistics_short_term VALUES (?, 1, ?, 21.0, 20.0, 22.0, NULL, NULL, NULL)",
        [(i, T + i * 300) for i in range(1, 4)],
    )
    con.commit()
    con.close()
    return path


def test_import_and_reimport(tmp_path: Path, lake: duckdb.DuckDBPyConnection) -> None:
    db = make_db(tmp_path / "home-assistant_v2.db")
    # A row already ingested from the live stream must not be duplicated.
    live = StateRow("sensor.t", "20", None, to_us(T), to_us(T))
    assert bronze.write_states(lake, [live], source="ha_live") == 1

    result = import_recorder_db(db, lake, batch_rows=3)
    assert (result.states_read, result.states_inserted) == (11, 10)
    assert result.statistics_inserted == {"hour": 1, "5minute": 3}
    assert result.metadata_inserted == 1
    assert result.first_state is not None
    assert result.last_state is not None

    again = import_recorder_db(db, lake)
    assert again.states_inserted == 0
    assert again.statistics_inserted == {"hour": 0, "5minute": 0}

    row = lake.execute(
        "SELECT attributes->>'unit_of_measurement', epoch_us(last_changed) "
        "FROM lake.bronze.ha_states WHERE entity_id = 'sensor.t' AND state = '21'"
    ).fetchone()
    assert row == ("°C", to_us(T + 60))  # last_changed_ts NULL -> last_updated


def test_import_time_range(tmp_path: Path, lake: duckdb.DuckDBPyConnection) -> None:
    from datetime import UTC, datetime

    db = make_db(tmp_path / "r.db")
    start = datetime.fromtimestamp(T + 120, UTC)
    end = datetime.fromtimestamp(T + 300, UTC)
    result = import_recorder_db(db, lake, start=start, end=end)
    assert result.states_inserted == 3  # +120, +180, +240


def test_rejects_old_schema(tmp_path: Path, lake: duckdb.DuckDBPyConnection) -> None:
    db = make_db(tmp_path / "old.db", version=30)
    with pytest.raises(RecorderSchemaError, match="too old"):
        import_recorder_db(db, lake)


def test_rejects_non_recorder_file(tmp_path: Path, lake: duckdb.DuckDBPyConnection) -> None:
    other = tmp_path / "x.db"
    sqlite3.connect(other).close()
    with pytest.raises(RecorderSchemaError):
        import_recorder_db(other, lake)
