from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import duckdb
import numpy as np
import pytest

from hacore import anomalies as an
from hacore.exclusions import ExclusionLabels

NOW = datetime(2026, 6, 1, 12, 30, tzinfo=UTC)
END = NOW.replace(minute=0)
DAYS = 70
HOURS = DAYS * 24


@pytest.fixture
def plain() -> Iterator[duckdb.DuckDBPyConnection]:
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    con.execute("ATTACH ':memory:' AS lake")
    try:
        yield con
    finally:
        con.close()


def insert_stats(con: duckdb.DuckDBPyConnection, series: dict[str, np.ndarray]) -> None:
    hours = [END - timedelta(hours=HOURS - i) for i in range(HOURS)]
    rows = [
        (sid, "hour", h, float(v))
        for sid, values in series.items()
        for h, v in zip(hours, values, strict=True)
        if not np.isnan(v)
    ]
    cols = list(zip(*rows, strict=True))
    con.execute(
        "INSERT INTO lake.silver.statistics BY NAME SELECT unnest($1) AS statistic_id, "
        "unnest($2) AS period, unnest($3) AS period_start, unnest($4) AS mean",
        [list(c) for c in cols],
    )


def seed(con: duckdb.DuckDBPyConnection) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(1)
    t = np.arange(HOURS)
    daily = np.sin(2 * np.pi * t / 24)
    weekend = ((t // 24) % 7 >= 5).astype(float)
    power = 100 + 40 * daily + 30 * weekend + rng.normal(0, 5, HOURS)
    power[-30:-25] += 120  # five unusual hours a day ago
    power[-60] -= 80  # one unusual hour
    level = 20 + rng.normal(0, 0.5, HOURS) + 0.3 * daily
    level[-12 * 24 :] += 4  # a lasting shift 12 days ago
    normal = 50 + 10 * daily + rng.normal(0, 2, HOURS)
    outside = 10 + 5 * daily + rng.normal(0, 0.5, HOURS)
    series = {
        "sensor.power": power,
        "sensor.level": level,
        "sensor.normal": normal,
        "sensor.outside": outside,
    }
    con.execute("CREATE SCHEMA lake.silver")
    con.execute(
        "CREATE TABLE lake.silver.statistics (statistic_id VARCHAR, period VARCHAR, "
        "period_start TIMESTAMPTZ, mean DOUBLE, min DOUBLE, max DOUBLE, state DOUBLE, "
        "sum DOUBLE, last_reset TIMESTAMPTZ, source VARCHAR)"
    )
    insert_stats(con, series)
    con.execute(
        "CREATE TABLE lake.silver.entities "
        "(entity_id VARCHAR, device_id VARCHAR, label_ids VARCHAR[])"
    )
    return series


def params(**kw: object) -> an.AnomalyParams:
    base: dict[str, object] = {"timezone": "Europe/Prague", "outside_temp": "sensor.outside"}
    base.update(kw)
    return an.AnomalyParams(**base)  # type: ignore[arg-type]


def rows(con: duckdb.DuckDBPyConnection, kind: str) -> list[dict[str, object]]:
    cur = con.execute(
        "SELECT * FROM lake.catalog.finding WHERE finding_type = ? ORDER BY rank", [kind]
    )
    cols = [d[0] for d in cur.description or []]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def test_anomaly_episode_found_v1(plain: duckdb.DuckDBPyConnection) -> None:
    seed(plain)
    res = an.run(plain, params(method="v1"), now=NOW)
    assert res.notes == []
    found = rows(plain, "anomaly")
    power = [f for f in found if f["entity_id"] == "sensor.power"]
    long = next(f for f in power if not f["flags"])
    assert long["period_start"] == END - timedelta(hours=30)
    assert long["period_end"] == END - timedelta(hours=25)
    assert long["n_eff"] == 5
    assert float(long["score"]) > 10  # type: ignore[arg-type]
    assert float(long["observed"]) - float(long["expected"]) > 80  # type: ignore[arg-type]
    assert long["window_days"] == 7
    assert long["rank"] == 1
    short = next(f for f in power if f["flags"])
    assert "short" in short["flags"]  # type: ignore[operator]
    assert float(short["score"]) < 0  # type: ignore[arg-type]
    # the outside temperature is a driver, not a candidate; noise stays quiet
    assert not [f for f in found if f["entity_id"] == "sensor.outside"]
    assert not [f for f in found if f["entity_id"] == "sensor.normal" and not f["flags"]]
    flagged = [bool(f["flags"]) for f in found]
    assert flagged == sorted(flagged)


def test_anomaly_episode_found_v2(plain: duckdb.DuckDBPyConnection) -> None:
    """v2: the five unusual hours get a conformal p-value and q; the noise stays quiet.
    With three series the reference is small (168 series-days), so the single unusual
    hour does not pass BH: that is the price of the calibration in a tiny installation."""
    seed(plain)
    res = an.run(plain, params(), now=NOW)
    sig = res.significance["anomaly"]
    assert sig["reference_size"] > 0
    found = rows(plain, "anomaly")
    long = next(f for f in found if f["entity_id"] == "sensor.power" and not f["flags"])
    assert long["period_start"] == END - timedelta(hours=30)
    assert long["n_eff"] == 5
    assert float(long["q_value"]) <= 0.05  # type: ignore[arg-type]
    assert float(long["p_value"]) <= float(long["q_value"])  # type: ignore[arg-type]
    assert not [f for f in found if f["entity_id"] == "sensor.normal"]
    # The level shift 12 days ago makes the recent week unusual: flagged, not new.
    shifted = [f for f in found if f["entity_id"] == "sensor.level"]
    assert all("level_shift" in f["flags"] for f in shifted)  # type: ignore[operator]


def test_change_point_found_v1(plain: duckdb.DuckDBPyConnection) -> None:
    seed(plain)
    an.run(plain, params(method="v1"), now=NOW)
    found = rows(plain, "change_point")
    assert [f["entity_id"] for f in found] == ["sensor.level"]
    assert float(found[0]["score"]) > 3  # type: ignore[arg-type]


def test_change_point_found(plain: duckdb.DuckDBPyConnection) -> None:
    seed(plain)
    an.run(plain, params(), now=NOW)
    found = rows(plain, "change_point")
    assert [f["entity_id"] for f in found] == ["sensor.level"]
    cp = found[0]
    start = cp["period_start"]
    assert isinstance(start, datetime)
    assert abs((start - (END - timedelta(days=12))).total_seconds()) <= 24 * 3600
    assert float(cp["observed"]) - float(cp["expected"]) == pytest.approx(4, abs=0.5)  # type: ignore[arg-type]
    assert float(cp["score"]) > 3  # type: ignore[arg-type]
    assert float(cp["q_value"]) <= 0.05  # type: ignore[arg-type]
    assert cp["window_days"] == 90


def test_rerun_replaces_and_keeps_first_detected(plain: duckdb.DuckDBPyConnection) -> None:
    seed(plain)
    an.run(plain, params(), now=NOW)
    first = {f["finding_id"]: f for f in rows(plain, "anomaly")}
    later = NOW + timedelta(minutes=10)
    an.run(plain, params(), now=later)
    second = rows(plain, "anomaly")
    assert {f["finding_id"] for f in second} == set(first)
    assert all(f["first_detected_at"] == NOW and f["detected_at"] == later for f in second)


def test_labelled_entities_left_out(plain: duckdb.DuckDBPyConnection) -> None:
    seed(plain)
    plain.execute(
        "INSERT INTO lake.silver.entities VALUES "
        "('sensor.power', NULL, ['hammond_no_analysis']), ('sensor.level', NULL, [])"
    )
    res = an.run(plain, params(), now=NOW)
    assert res.series_excluded == 1
    assert not [f for f in rows(plain, "anomaly") if f["entity_id"] == "sensor.power"]


def test_custom_label(plain: duckdb.DuckDBPyConnection) -> None:
    seed(plain)
    plain.execute(
        "INSERT INTO lake.silver.entities VALUES ('sensor.power', NULL, ['private']), "
        "('sensor.level', NULL, ['hammond_no_analysis'])"
    )
    labels = ExclusionLabels(no_analysis="private")
    res = an.run(plain, params(labels=labels), now=NOW)
    assert res.series_excluded == 1
    found = {f["entity_id"] for f in rows(plain, "anomaly") + rows(plain, "change_point")}
    assert "sensor.power" not in found
    assert "sensor.level" in found


def test_weather_flag(plain: duckdb.DuckDBPyConnection) -> None:
    series = seed(plain)
    plain.execute("DELETE FROM lake.silver.statistics WHERE statistic_id = 'sensor.outside'")
    outside = series["sensor.outside"].copy()
    outside[-30:-25] -= 25  # a cold snap in the same hours
    insert_stats(plain, {"sensor.outside": outside})
    an.run(plain, params(), now=NOW)
    long = next(
        f for f in rows(plain, "anomaly") if f["entity_id"] == "sensor.power" and f["n_eff"] == 5
    )
    assert "weather" in long["flags"]  # type: ignore[operator]


def test_empty_lake(plain: duckdb.DuckDBPyConnection) -> None:
    res = an.run(plain, params(), now=NOW)
    assert res.notes == ["not enough data in the window"]
    assert rows(plain, "anomaly") == []


def test_episodes() -> None:
    z = np.array([0, 5, 6, 0, 5, 0, 0, -5, -6, 0, 5], dtype=float)
    assert an.episodes(z, 4) == [(1, 4, 6.0, 3), (7, 8, -6.0, 2), (10, 10, 5.0, 1)]


def test_best_shift() -> None:
    rng = np.random.default_rng(5)
    x = np.concatenate([rng.normal(0, 1, 60), rng.normal(5, 1, 20)])
    s = an.best_shift(x, 30)
    assert s is not None
    assert abs(s.split - 60) <= 1
    assert s.shift > 3
    assert s.consistent > 0.9
    flat = rng.normal(0, 1, 80)
    f = an.best_shift(flat, 30)
    assert f is None or abs(f.shift) < 3


def test_on_ducklake(lake: duckdb.DuckDBPyConnection) -> None:
    seed(lake)
    res = an.run(lake, params(), now=NOW)
    assert res.anomalies > 0
    assert res.change_points == 1


def test_ar1_surrogates_keep_scale_and_autocorrelation() -> None:
    rng = np.random.default_rng(9)
    e = rng.normal(0, 1, 2000)
    x = np.empty_like(e)
    x[0] = e[0]
    for i in range(1, len(e)):
        x[i] = 0.6 * x[i - 1] + e[i]
    sur = an._ar1_surrogates(x, 0.6, 20, rng)
    assert sur.shape == (20, 2000)
    assert np.std(sur) == pytest.approx(np.std(x), rel=0.1)
    lag1 = np.mean([np.corrcoef(s[:-1], s[1:])[0, 1] for s in sur])
    assert lag1 == pytest.approx(0.6, abs=0.05)


def test_change_points_v2_quiet_on_autocorrelated_noise() -> None:
    """Many autocorrelated series without a change: BH over series keeps the false
    change points rare (block permutations of weeks would have too little power to
    find real ones; AR(1) surrogates keep both)."""
    rng = np.random.default_rng(10)
    days, n = 90, 60
    hours = np.array([END - timedelta(hours=days * 24 - i) for i in range(days * 24)])
    daily = np.empty((days, n))
    daily[0] = rng.normal(0, 1, n)
    for d in range(1, days):
        daily[d] = 0.7 * daily[d - 1] + rng.normal(0, 1, n)
    values = np.repeat(daily, 24, axis=0) + rng.normal(0, 0.3, (days * 24, n))
    values[-10 * 24 :, 0] += 12  # one real change 10 days ago (~3.6 long-run SD)
    series = [an.corr.Series(f"sensor.s{j}", "mean", None) for j in range(n)]
    local = np.array([h.hour for h in hours])
    grid = an.corr.Grid(
        hours=hours,
        local_hour=local,
        local_dow=np.ones(len(hours)),
        series=series,
        values=values,
    )
    found, stats = an.detect_change_points_v2(grid, an.AnomalyParams(timezone="UTC"))
    assert stats["tested"] == n
    ids = [f["entity_id"] for f in found]
    assert "sensor.s0" in ids
    assert len(ids) <= 3


def test_anomalies_v2_nearly_constant_series_do_not_own_the_reference() -> None:
    """A sensor stuck at one value with rare jumps has a robust scale near 0 and z in
    the millions. Without the resolution floor and the z cap a few of them made the
    pooled reference so heavy that nothing passed (homelab, 0.4.0)."""
    rng = np.random.default_rng(3)
    days, n, stuck = 63, 40, 5
    t = days * 24
    hours = np.array([END - timedelta(hours=t - i) for i in range(t)])
    values = rng.normal(0, 1, (t, n))
    for j in range(n - stuck, n):
        values[:, j] = 20.0 + rng.normal(0, 1e-6, t)  # stuck, float jitter only
        jumps = rng.choice(t - 7 * 24, 3, replace=False)
        values[jumps, j] += rng.uniform(5, 50, 3)  # rare one-hour jumps in the baseline
    values[-36:-24, 0] += 15  # one real 12-hour episode in the recent week
    series = [an.corr.Series(f"sensor.s{j}", "mean", None) for j in range(n)]
    local = np.array([h.hour for h in hours])
    dow = np.array([h.isoweekday() for h in hours])
    grid = an.corr.Grid(hours=hours, local_hour=local, local_dow=dow, series=series, values=values)
    found, stats = an.detect_anomalies_v2(grid, an.AnomalyParams(timezone="UTC"))
    assert stats["candidates"] > 0
    assert "sensor.s0" in [f["entity_id"] for f in found]
    assert an._episode_stat(1e6, 4) == pytest.approx(an.Z_CAP * 2)


def test_resolution() -> None:
    v = np.array([[1.0, 0.0], [1.5, 0.0], [2.0, 0.0], [np.nan, 0.0]])
    res = an._resolution(v, np.ones(4, dtype=bool))
    assert res[0] == pytest.approx(0.5)
    assert res[1] == 0.0
