import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import duckdb
import numpy as np
import pytest

from hacore import correlations as corr
from hacore import weather

NOW = datetime(2026, 6, 1, 12, 30, tzinfo=UTC)
END = NOW.replace(minute=0)
DAYS = 60
HOURS = DAYS * 24


@pytest.fixture
def plain() -> Iterator[duckdb.DuckDBPyConnection]:
    """Silver tables in a plain DuckDB database attached as ``lake`` (no DuckLake)."""
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    con.execute("ATTACH ':memory:' AS lake")
    try:
        yield con
    finally:
        con.close()


def insert(
    con: duckdb.DuckDBPyConnection, table: str, columns: list[str], rows: list[tuple[object, ...]]
) -> None:
    """Bulk insert through list parameters (executemany is slow in DuckDB)."""
    cols = list(zip(*rows, strict=True))
    select = ", ".join(f"unnest(${i + 1}) AS {c}" for i, c in enumerate(columns))
    con.execute(f"INSERT INTO {table} BY NAME SELECT {select}", [list(c) for c in cols])


def ar_noise(rng: np.random.Generator, n: int, phi: float = 0.5) -> np.ndarray:
    e = rng.normal(size=n)
    out = np.empty(n)
    out[0] = e[0]
    for i in range(1, n):
        out[i] = phi * out[i - 1] + e[i]
    return out


def seed(con: duckdb.DuckDBPyConnection) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(42)
    t = np.arange(HOURS)
    hours = [END - timedelta(hours=HOURS - int(i)) for i in t]
    daily = np.sin(2 * np.pi * t / 24)
    outside = 5 + 8 * daily + np.cumsum(rng.normal(scale=0.3, size=HOURS))
    a = ar_noise(rng, HOURS)
    b = np.roll(a, 3) + 0.5 * rng.normal(size=HOURS)  # follows a by 3 hours
    heating = 30 - 1.5 * outside + 3 * daily + 0.5 * rng.normal(size=HOURS)
    radiator = 50 - 2.0 * outside + 0.5 * rng.normal(size=HOURS)
    noise = rng.normal(size=HOURS)
    power = np.clip(a + 3, 0, None) + 0.2 * rng.uniform(size=HOURS)  # counter increase per hour
    series = {
        "sensor.outside": outside,
        "sensor.a": a,
        "sensor.b": b,
        "sensor.heating": heating,
        "sensor.radiator": radiator,
        "sensor.noise": noise,
    }
    con.execute("CREATE SCHEMA lake.silver")
    con.execute(
        "CREATE TABLE lake.silver.statistics (statistic_id VARCHAR, period VARCHAR, "
        "period_start TIMESTAMPTZ, mean DOUBLE, min DOUBLE, max DOUBLE, state DOUBLE, "
        "sum DOUBLE, last_reset TIMESTAMPTZ, source VARCHAR)"
    )
    rows = []
    for sid, values in series.items():
        rows += [(sid, "hour", h, float(v), None) for h, v in zip(hours, values, strict=True)]
    total = np.cumsum(power)
    rows += [
        ("sensor.energy", "hour", h, None, float(v)) for h, v in zip(hours, total, strict=True)
    ]
    insert(
        con,
        "lake.silver.statistics",
        ["statistic_id", "period", "period_start", "mean", "sum"],
        rows,
    )
    # A door that is open for the part of the hour when a is high.
    con.execute(
        "CREATE TABLE lake.silver.states (entity_id VARCHAR, domain VARCHAR, state VARCHAR, "
        "state_value DOUBLE, is_missing BOOLEAN, attributes JSON, last_changed TIMESTAMPTZ, "
        "last_updated TIMESTAMPTZ, context_id VARCHAR, source VARCHAR)"
    )
    states = []
    for h, v in zip(hours, a, strict=True):
        open_minutes = int(np.clip((v + 2) * 15, 0, 59))
        states.append(("binary_sensor.door", "on", False, h))
        states.append(("binary_sensor.door", "off", False, h + timedelta(minutes=open_minutes)))
    # an unavailable hour does not count
    states.append(("binary_sensor.door", "unavailable", True, hours[10] + timedelta(minutes=1)))
    insert(
        con,
        "lake.silver.states",
        ["entity_id", "domain", "state", "is_missing", "last_updated"],
        [(e, e.split(".")[0], s, m, ts) for e, s, m, ts in states],
    )
    con.execute(
        "CREATE TABLE lake.silver.entities "
        "(entity_id VARCHAR, device_id VARCHAR, label_ids VARCHAR[])"
    )
    con.execute(
        "INSERT INTO lake.silver.entities (entity_id, device_id) "
        "VALUES ('sensor.a', 'dev1'), ('sensor.energy', 'dev1')"
    )
    return series


def params(**kw: object) -> corr.CorrelationParams:
    base: dict[str, object] = {
        "window_days": 90,
        "max_lag": 6,
        "min_r": 0.3,
        "max_q": 0.01,
        "timezone": "Europe/Prague",
    }
    base.update(kw)
    return corr.CorrelationParams(**base)  # type: ignore[arg-type]


def findings(con: duckdb.DuckDBPyConnection) -> list[dict[str, object]]:
    cur = con.execute("SELECT * FROM lake.catalog.finding ORDER BY rank")
    cols = [d[0] for d in cur.description or []]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def pair(f: dict[str, object]) -> tuple[object, object]:
    return f["entity_id"], f["related_entity_id"]


def test_grid_metrics(plain: duckdb.DuckDBPyConnection) -> None:
    seed(plain)
    grid = corr.load_grid(plain, params(), now=NOW)
    assert grid is not None
    # The grid starts at the first data, not at the start of the 90-day window.
    assert len(grid.hours) == HOURS
    keys = {(s.entity_id, s.metric, s.state) for s in grid.series}
    assert ("sensor.energy", "delta", None) in keys
    assert ("sensor.a", "mean", None) in keys
    assert ("binary_sensor.door", "share", "on") in keys
    j = next(i for i, s in enumerate(grid.series) if s.entity_id == "binary_sensor.door")
    share = grid.values[:, j]
    assert np.nanmin(share) >= 0
    assert np.nanmax(share) <= 1
    e = next(i for i, s in enumerate(grid.series) if s.entity_id == "sensor.energy")
    assert np.isnan(grid.values[0, e])  # no previous hour for the first difference
    assert np.nanmin(grid.values[:, e]) >= 0


def test_finds_lagged_pair_and_removes_common_driver(plain: duckdb.DuckDBPyConnection) -> None:
    seed(plain)
    res = corr.run(plain, params(outside_temp="sensor.outside"), now=NOW)
    assert res.findings > 0
    assert res.notes == []
    found = {pair(f): f for f in findings(plain)}
    ab = found[("sensor.a", "sensor.b")]
    assert ab["lag_hours"] == 3
    assert float(ab["score"]) > 0.7  # type: ignore[arg-type]
    assert float(ab["q_value"]) < 1e-6  # type: ignore[arg-type]
    # heating and radiator only share the outside temperature
    assert ("sensor.heating", "sensor.radiator") not in found
    assert ("sensor.radiator", "sensor.heating") not in found
    # the driver itself is not a candidate, noise finds nothing
    assert not any("sensor.outside" in pair(f) for f in found.values())
    assert not any("sensor.noise" in pair(f) for f in found.values())
    # share of time of a binary sensor and a counter increase take part
    assert ("sensor.a", "binary_sensor.door") in found or (
        "binary_sensor.door",
        "sensor.a",
    ) in found
    energy = found.get(("sensor.a", "sensor.energy")) or found.get(("sensor.energy", "sensor.a"))
    assert energy is not None
    assert "same_device" in energy["flags"]  # type: ignore[operator]
    ranks = [f["rank"] for f in findings(plain)]
    assert ranks == list(range(1, len(ranks) + 1))


def test_without_outside_temperature_the_driver_shows(plain: duckdb.DuckDBPyConnection) -> None:
    seed(plain)
    corr.run(plain, params(), now=NOW)
    found = {pair(f) for f in findings(plain)}
    assert {("sensor.heating", "sensor.radiator"), ("sensor.radiator", "sensor.heating")} & found


def test_rerun_keeps_first_detected_and_replaces(plain: duckdb.DuckDBPyConnection) -> None:
    seed(plain)
    corr.run(plain, params(outside_temp="sensor.outside"), now=NOW)
    first = {f["finding_id"]: f for f in findings(plain)}
    later = NOW + timedelta(hours=1)
    corr.run(plain, params(outside_temp="sensor.outside"), now=later)
    second = findings(plain)
    assert len(second) == len(first)
    for f in second:
        assert f["detected_at"] == later
        if f["finding_id"] in first:
            assert f["first_detected_at"] == NOW
    # another window is kept separately
    corr.run(plain, params(window_days=30, outside_temp="sensor.outside"), now=later)
    windows = plain.execute(
        "SELECT window_days, count(*) FROM lake.catalog.finding GROUP BY 1 ORDER BY 1"
    ).fetchall()
    assert [w for w, _ in windows] == [30, 90]


def test_empty_lake(plain: duckdb.DuckDBPyConnection) -> None:
    res = corr.run(plain, params(), now=NOW)
    assert res.findings == 0
    assert res.notes == ["not enough data in the window"]


def test_missing_outside_temperature_is_noted(plain: duckdb.DuckDBPyConnection) -> None:
    seed(plain)
    res = corr.run(plain, params(outside_temp="sensor.nope"), now=NOW)
    assert res.notes == ["outside temperature sensor.nope not in the grid"]


def test_benjamini_hochberg() -> None:
    p = np.array([0.01, 0.04, 0.03, 0.5])
    q = corr.benjamini_hochberg(p)
    np.testing.assert_allclose(q, [0.04, 0.16 / 3, 0.16 / 3, 0.5])


def test_ffill_limit() -> None:
    nan = np.nan
    x = np.array([[1.0], [nan], [nan], [nan], [nan], [5.0]])
    out = corr.ffill(x, limit=3)
    assert out[:, 0].tolist()[:4] == [1.0, 1.0, 1.0, 1.0]
    assert np.isnan(out[4, 0])
    assert out[5, 0] == 5.0


def test_on_ducklake(lake: duckdb.DuckDBPyConnection) -> None:
    seed(lake)
    res = corr.run(lake, params(outside_temp="sensor.outside"), now=NOW)
    assert res.findings > 0
    assert ("sensor.a", "sensor.b") in {pair(f) for f in findings(lake)}


def test_weather_entity_as_outside_temperature(plain: duckdb.DuckDBPyConnection) -> None:
    series = seed(plain)
    # The temperature of a weather entity lives in its attributes, two updates an hour.
    hours = [END - timedelta(hours=HOURS - i) for i in range(HOURS)]
    rows = []
    for h, v in zip(hours, series["sensor.outside"], strict=True):
        for minute in (5, 35):
            attrs = json.dumps({"temperature": round(float(v), 2), "temperature_unit": "°C"})
            rows.append(
                ("weather.home", "weather", "cloudy", False, attrs, h + timedelta(minutes=minute))
            )
    insert(
        plain,
        "lake.silver.states",
        ["entity_id", "domain", "state", "is_missing", "attributes", "last_updated"],
        rows,
    )
    plain.execute("DELETE FROM lake.silver.statistics WHERE statistic_id = 'sensor.outside'")
    res = corr.run(plain, params(outside_temp="weather.home"), now=NOW)
    assert res.notes == []
    found = {pair(f) for f in findings(plain)}
    assert ("sensor.heating", "sensor.radiator") not in found
    assert ("sensor.radiator", "sensor.heating") not in found
    assert "weather.home" not in {e for p in found for e in p}


def test_short_outside_temperature_is_not_used(plain: duckdb.DuckDBPyConnection) -> None:
    seed(plain)
    plain.execute(
        "DELETE FROM lake.silver.statistics WHERE statistic_id = 'sensor.outside' "
        "AND period_start < $cut",
        {"cut": END - timedelta(days=10)},
    )
    res = corr.run(plain, params(outside_temp="sensor.outside"), now=NOW)
    assert res.notes == [
        "outside temperature sensor.outside covers only 17% of the window, not used"
    ]


def test_open_meteo_archive_fills_the_driver(plain: duckdb.DuckDBPyConnection) -> None:
    series = seed(plain)
    outside = series["sensor.outside"]
    hours = [END - timedelta(hours=HOURS - i) for i in range(HOURS)]
    archive = dict(zip(hours, (float(v) for v in outside), strict=True))
    # The entity itself only has the last 10 days.
    plain.execute(
        "DELETE FROM lake.silver.statistics WHERE statistic_id = 'sensor.outside' "
        "AND period_start < $cut",
        {"cut": END - timedelta(days=10)},
    )
    calls: list[tuple[float, float, object, object]] = []

    def fetch(lat: float, lon: float, start: object, end: object) -> list[tuple[datetime, float]]:
        calls.append((lat, lon, start, end))
        return [(h, v) for h, v in archive.items() if start <= h.date() <= end]  # type: ignore[operator]

    res = weather.sync(plain, 50.08812, 14.42076, days=DAYS - 1, now=NOW, fetch=fetch)
    assert calls[0][:2] == (50.09, 14.42)  # rounded before leaving the installation
    assert res.hours > (DAYS - 2) * 24
    # A second sync only re-fetches the recent days.
    weather.sync(plain, 50.08812, 14.42076, days=DAYS - 1, now=NOW, fetch=fetch)
    assert calls[1][2] == (END - weather.REFETCH).date()
    stored = plain.execute("SELECT count(*) FROM lake.bronze.ext_outside_temperature").fetchone()
    assert stored == (res.hours,)

    result = corr.run(plain, params(outside_temp="sensor.outside"), now=NOW)
    assert result.notes == []
    assert result.temperature_archive_hours > (DAYS - 12) * 24
    found = {pair(f) for f in findings(plain)}
    assert ("sensor.heating", "sensor.radiator") not in found
    assert ("sensor.radiator", "sensor.heating") not in found


def test_trivial_pairs_last_and_one_row_per_pair(plain: duckdb.DuckDBPyConnection) -> None:
    series = seed(plain)
    hours = [END - timedelta(hours=HOURS - i) for i in range(HOURS)]
    a = series["sensor.a"]
    step = np.where(np.arange(HOURS) < HOURS // 2, 1.0, 2.0)  # one jump: nearly constant
    extra = {"sensor.a_percent": a * 100 + 7, "sensor.step": step, "sensor.step_too": step * 3}
    rows = [
        (sid, "hour", h, float(v), None)
        for sid, values in extra.items()
        for h, v in zip(hours, values, strict=True)
    ]
    insert(
        plain,
        "lake.silver.statistics",
        ["statistic_id", "period", "period_start", "mean", "sum"],
        rows,
    )
    corr.run(plain, params(outside_temp="sensor.outside"), now=NOW)
    found = findings(plain)
    pairs = [frozenset((f["entity_id"], f["related_entity_id"])) for f in found]
    assert len(pairs) == len(set(pairs))  # never both directions of one pair
    assert not any("sensor.step" in p or "sensor.step_too" in p for p in pairs)
    dup = next(
        f
        for f in found
        if pair(f) in {("sensor.a", "sensor.a_percent"), ("sensor.a_percent", "sensor.a")}
    )
    assert "duplicate_suspect" in dup["flags"]  # type: ignore[operator]
    flagged = [bool(f["flags"]) for f in found]
    assert flagged == sorted(flagged)  # unflagged first
    assert not found[0]["flags"]


def test_near_duplicates_grouped(plain: duckdb.DuckDBPyConnection) -> None:
    series = seed(plain)
    hours = [END - timedelta(hours=HOURS - i) for i in range(HOURS)]
    rng = np.random.default_rng(7)
    a = series["sensor.a"]
    rows = [
        ("sensor.a_copy", "hour", h, float(v), None)
        for h, v in zip(hours, a * 2 + rng.normal(0, 0.01, HOURS), strict=True)
    ]
    insert(
        plain,
        "lake.silver.statistics",
        ["statistic_id", "period", "period_start", "mean", "sum"],
        rows,
    )
    corr.run(plain, params(outside_temp="sensor.outside"), now=NOW)
    found = findings(plain)
    to_b = [f for f in found if "sensor.b" in pair(f) and not f["flags"]]
    # a -> b and a_copy -> b are one relation, reported once with the group listed.
    assert len([f for f in to_b if {"sensor.a", "sensor.a_copy"} & set(pair(f))]) == 1
    row = next(f for f in to_b if {"sensor.a", "sensor.a_copy"} & set(pair(f)))
    groups = set(row["entity_group"]) | set(row["related_group"])  # type: ignore[arg-type]
    assert groups & {"sensor.a", "sensor.a_copy"}
    dup = [f for f in found if set(pair(f)) == {"sensor.a", "sensor.a_copy"}]
    assert dup
    assert "duplicate_suspect" in dup[0]["flags"]  # type: ignore[operator]


def test_highpass_removes_shared_trend() -> None:
    rng = np.random.default_rng(3)
    n = 24 * 60
    trend = np.linspace(0, 50, n)
    x = np.column_stack([trend + rng.normal(0, 1, n), trend * 2 + rng.normal(0, 1, n)])
    raw = np.corrcoef(x.T)[0, 1]
    hp = corr.highpass(x)
    ok = ~np.isnan(hp).any(axis=1)
    assert raw > 0.9
    assert abs(np.corrcoef(hp[ok].T)[0, 1]) < 0.15
    assert corr.highpass(x, 0) is x


def test_correlation_is_pearson_over_the_common_hours() -> None:
    rng = np.random.default_rng(11)
    n = 2000
    a = rng.normal(0, 1, n)
    a[:300] *= 3  # the overlap has its own variance
    b = np.full(n, np.nan)
    b[:300] = 0.5 * a[:300] / 3 + rng.normal(0, 1, 300)
    pairs = corr.lagged_correlations(np.column_stack([a, b]), 0)
    expected = np.corrcoef(np.clip(a[:300] / a.std(), -4, 4), b[:300])[0, 1]
    assert len(pairs.r) == 1
    assert abs(float(pairs.r[0]) - expected) < 0.02
    assert abs(float(pairs.r[0])) < 0.9


def test_clocks_and_removed_entities_are_left_out(plain: duckdb.DuckDBPyConnection) -> None:
    series = seed(plain)
    hours = [END - timedelta(hours=HOURS - i) for i in range(HOURS)]
    rng = np.random.default_rng(5)
    up = np.arange(HOURS, dtype=float)
    for restart in (300, 700, 1100):  # both hosts restart together after an outage
        up[restart:] -= up[restart]
    stale = series["sensor.a"] * 3 + rng.normal(0, 0.05, HOURS)
    extra = {
        "sensor.host1_uptime": up * 3600,
        "sensor.host2_uptime": up * 60,
        "sensor.removed": np.where(np.arange(HOURS) < HOURS - 240, stale, np.nan),
    }
    rows = [
        (sid, "hour", h, float(v), None)
        for sid, values in extra.items()
        for h, v in zip(hours, values, strict=True)
        if not np.isnan(v)
    ]
    insert(
        plain,
        "lake.silver.statistics",
        ["statistic_id", "period", "period_start", "mean", "sum"],
        rows,
    )
    corr.run(plain, params(outside_temp="sensor.outside"), now=NOW)
    entities = {e for f in findings(plain) for e in pair(f)}
    assert not entities & set(extra)
    assert {"sensor.a", "sensor.b"} <= entities


def test_label_leaves_an_entity_out_of_the_analysis(plain: duckdb.DuckDBPyConnection) -> None:
    seed(plain)
    plain.execute(
        "INSERT INTO lake.silver.entities VALUES "
        "('sensor.b', 'dev2', ['hammond_no_analysis']), "
        "('sensor.outside', NULL, ['hammond_no_analysis'])"
    )
    result = corr.run(plain, params(outside_temp="sensor.outside"), now=NOW)
    assert result.series_excluded == 1  # the outside temperature stays a driver
    assert result.notes == []
    assert not any("sensor.b" in pair(f) for f in findings(plain))
