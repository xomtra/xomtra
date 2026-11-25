import pytest
from pydantic import SecretStr

from hacore.analysis_settings import BY_KEY, SPECS, parse_stored
from hammond_ingest import analysis_store
from hammond_ingest.settings import IngestSettings


def test_specs_match_ingest_settings() -> None:
    """Every key the admin GUI may store exists in the ingest settings with the same
    default, and every value the GUI accepts at its bounds the ingest accepts too."""
    s = IngestSettings()
    for spec in SPECS:
        assert spec.key in IngestSettings.model_fields, spec.key
        assert getattr(s, spec.key) == spec.default, spec.key
        for edge in (spec.lo, spec.hi):
            if edge is None or spec.kind in ("bool", "choice"):
                continue
            value = (
                spec.default
                if (edge == spec.lo and spec.lo_open) or (edge == spec.hi and spec.hi_open)
                else edge
            )
            IngestSettings(**{spec.key: value})  # does not raise


def test_spec_parse() -> None:
    q = BY_KEY["correlation_max_q"]
    assert q.parse("0,02") == pytest.approx(0.02)
    for bad in ("0", "1.5", "x", "nan"):
        with pytest.raises(ValueError, match="correlation_max_q"):
            q.parse(bad)
    assert BY_KEY["correlation_top"].parse("50") == 50
    with pytest.raises(ValueError, match="whole number"):
        BY_KEY["correlation_top"].parse("50.5")
    assert BY_KEY["anomalies_enabled"].parse("off") is False
    with pytest.raises(ValueError, match="one of"):
        BY_KEY["correlation_method"].parse("v3")


def test_parse_stored_skips_unknown_and_invalid() -> None:
    values, problems = parse_stored(
        [("correlation_min_r", 0.4), ("ha_token", "x"), ("anomaly_max_q", 7)]
    )
    assert values == {"correlation_min_r": 0.4}
    assert len(problems) == 2


def test_stored_values_win_over_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HAMMOND_CORRELATION_MIN_R", "0.35")
    monkeypatch.setenv("HAMMOND_ANOMALY_MAX_Q", "0.1")
    s = IngestSettings()
    out = analysis_store.with_overrides(s, {"correlation_min_r": 0.5, "ha_token": "x"})  # type: ignore[dict-item]
    assert out.correlation_min_r == 0.5
    assert out.anomaly_max_q == 0.1  # environment, not stored
    assert out.ha_token.get_secret_value() == s.ha_token.get_secret_value()
    assert analysis_store.with_overrides(s, {}) is s


def test_no_database_means_no_overrides() -> None:
    assert analysis_store.load_overrides(IngestSettings(ops_pg_password=SecretStr(""))) == {}
    assert analysis_store.load_overrides(IngestSettings(analysis_settings_db="")) == {}


def test_unreachable_database_falls_back(caplog: pytest.LogCaptureFixture) -> None:
    s = IngestSettings(ops_pg_host="127.0.0.1", ops_pg_port=1, ops_pg_password=SecretStr("x"))
    assert analysis_store.load_overrides(s) == {}
    assert "using the environment" in caplog.text
