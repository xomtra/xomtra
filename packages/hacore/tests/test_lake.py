import os

import pytest

from hacore.lake import LakeSettings, attach_sql, check, init, secret_sql

ENV = {
    "HAMMOND_LAKE_PG_HOST": "db",
    "HAMMOND_LAKE_PG_DB": "ducklake",
    "HAMMOND_LAKE_PG_USER": "ducklake",
    "HAMMOND_LAKE_PG_PASSWORD": "pa'ss\\word",
    "HAMMOND_S3_ENDPOINT": "s3:8333",
    "HAMMOND_S3_ACCESS_KEY": "AK",
    "HAMMOND_S3_SECRET_KEY": "se'cret",
    "HAMMOND_S3_BUCKET": "hammond",
}


def test_from_env_requires_all_values() -> None:
    with pytest.raises(ValueError, match="HAMMOND_S3_BUCKET"):
        LakeSettings.from_env({k: v for k, v in ENV.items() if k != "HAMMOND_S3_BUCKET"})


def test_repr_hides_secrets() -> None:
    settings = LakeSettings.from_env(ENV)
    assert "se'cret" not in repr(settings)
    assert "word" not in repr(settings)


def test_sql_quoting() -> None:
    settings = LakeSettings.from_env(ENV)
    assert "SECRET 'se''cret'" in secret_sql(settings)
    attach = attach_sql(settings)
    assert r"password=''pa\''ss\\word''" in attach
    assert "DATA_PATH 's3://hammond/lake/'" in attach
    assert "DATA_INLINING_ROW_LIMIT 0" in attach


@pytest.mark.skipif(
    not os.environ.get("HAMMOND_LAKE_PG_HOST"),
    reason="integration test, needs the compose stack (see deploy/compose)",
)
def test_round_trip_against_stack() -> None:
    settings = LakeSettings.from_env()
    init(settings)
    check(settings)
