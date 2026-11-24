import pytest

from hacore.edition import PROFILES, Edition, edition_from_env, profile


def test_default_is_full() -> None:
    assert edition_from_env({}) is Edition.FULL
    assert profile(env={}) == PROFILES[Edition.FULL]


def test_lite_from_env() -> None:
    assert edition_from_env({"HAMMOND_EDITION": " Lite "}) is Edition.LITE
    lite = profile(Edition.LITE)
    assert lite.states_retention_days == 90
    assert lite.statistics_hour_retention_days == 5 * 365


def test_unknown_edition() -> None:
    with pytest.raises(ValueError, match="full, lite"):
        edition_from_env({"HAMMOND_EDITION": "pro"})
