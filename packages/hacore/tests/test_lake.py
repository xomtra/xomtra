import os

import pytest

from hacore.lake import LakeSettings, attach_sql, check, init, secret_sql

ENV = {
    "XOMTRA_LAKE_PG_HOST": "db",
    "XOMTRA_LAKE_PG_DB": "ducklake",
    "XOMTRA_LAKE_PG_USER": "ducklake",
    "XOMTRA_LAKE_PG_PASSWORD": "pa'ss\\word",
    "XOMTRA_S3_ENDPOINT": "s3:8333",
    "XOMTRA_S3_ACCESS_KEY": "AK",
    "XOMTRA_S3_SECRET_KEY": "se'cret",
    "XOMTRA_S3_BUCKET": "xomtra",
}


def test_from_env_requires_all_values() -> None:
    with pytest.raises(ValueError, match="XOMTRA_S3_BUCKET"):
        LakeSettings.from_env({k: v for k, v in ENV.items() if k != "XOMTRA_S3_BUCKET"})


def test_repr_hides_secrets() -> None:
    settings = LakeSettings.from_env(ENV)
    assert "se'cret" not in repr(settings)
    assert "word" not in repr(settings)


def test_sql_quoting() -> None:
    settings = LakeSettings.from_env(ENV)
    assert "SECRET 'se''cret'" in secret_sql(settings)
    attach = attach_sql(settings)
    assert r"password=''pa\''ss\\word''" in attach
    assert "DATA_PATH 's3://xomtra/lake/'" in attach
    assert "DATA_INLINING_ROW_LIMIT 0" in attach


@pytest.mark.skipif(
    not os.environ.get("XOMTRA_LAKE_PG_HOST"),
    reason="integration test, needs the compose stack (see deploy/compose)",
)
def test_round_trip_against_stack() -> None:
    settings = LakeSettings.from_env()
    init(settings)
    check(settings)


def test_duckdb_resource_options() -> None:
    default = LakeSettings.from_env(ENV).duckdb_config()
    assert default["memory_limit"] == "2GB"
    assert default["threads"] == 2
    assert default["preserve_insertion_order"] is False
    assert "temp_directory" not in default
    custom = LakeSettings.from_env(
        {
            **ENV,
            "XOMTRA_DUCKDB_MEMORY_LIMIT": "512MB",
            "XOMTRA_DUCKDB_THREADS": "1",
            "XOMTRA_DUCKDB_TEMP_DIR": "/data/duckdb-tmp",
        }
    ).duckdb_config()
    assert custom["memory_limit"] == "512MB"
    assert custom["threads"] == 1
    assert custom["temp_directory"] == "/data/duckdb-tmp"
    import duckdb

    con = duckdb.connect(config={k: v for k, v in custom.items() if k != "temp_directory"})
    assert con.execute("SELECT current_setting('threads')").fetchone() == (1,)
    con.close()


LOCAL_ENV = {k: v for k, v in ENV.items() if not k.startswith("XOMTRA_S3_")} | {
    "XOMTRA_LAKE_DATA_PATH": "/data/lake",
}


def test_local_data_path_needs_no_object_storage() -> None:
    settings = LakeSettings.from_env(LOCAL_ENV)
    assert settings.is_local
    assert settings.data_path == "/data/lake/"
    assert "httpfs" not in settings.extensions
    assert "DATA_PATH '/data/lake/'" in attach_sql(settings)
    assert "se'cret" not in repr(settings)


def test_local_data_path_must_be_absolute() -> None:
    with pytest.raises(ValueError, match="absolute"):
        LakeSettings.from_env({**LOCAL_ENV, "XOMTRA_LAKE_DATA_PATH": "lake"})


def test_object_storage_needs_s3_values() -> None:
    with pytest.raises(ValueError, match="XOMTRA_S3_"):
        LakeSettings(pg_host="db", pg_db="d", pg_user="u", pg_password="p")
    full = LakeSettings.from_env(ENV)
    assert not full.is_local
    assert "httpfs" in full.extensions


def test_lite_edition_has_smaller_duckdb_budget() -> None:
    lite = LakeSettings.from_env({**LOCAL_ENV, "XOMTRA_EDITION": "lite"})
    assert lite.memory_limit == "512MB"
    explicit = LakeSettings.from_env(
        {**LOCAL_ENV, "XOMTRA_EDITION": "lite", "XOMTRA_DUCKDB_MEMORY_LIMIT": "1GB"}
    )
    assert explicit.memory_limit == "1GB"
    with pytest.raises(ValueError, match="XOMTRA_EDITION"):
        LakeSettings.from_env({**LOCAL_ENV, "XOMTRA_EDITION": "tiny"})
