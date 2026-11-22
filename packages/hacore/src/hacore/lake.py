"""DuckLake access: Postgres catalog + S3 compatible object storage (SeaweedFS).

Configuration comes from environment variables (see ``LakeSettings.from_env``) so that
no installation specific values ever live in the repository.

CLI (used by the compose ``ducklake-init`` service)::

    python -m hacore.lake init    # create the catalog and meta schema, then run check
    python -m hacore.lake check   # round-trip a row through catalog + object storage
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import duckdb

logger = logging.getLogger(__name__)

REQUIRED_EXTENSIONS = ("ducklake", "postgres", "httpfs")
LAKE_ALIAS = "lake"


def _sql_literal(value: str) -> str:
    """Quote a value as a SQL string literal."""
    return "'" + value.replace("'", "''") + "'"


def _libpq_value(value: str) -> str:
    """Quote a value for a libpq key=value connection string."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True, repr=False)
class LakeSettings:
    pg_host: str
    pg_db: str
    pg_user: str
    pg_password: str
    s3_endpoint: str
    s3_access_key: str
    s3_secret_key: str
    s3_bucket: str
    pg_port: int = 5432
    s3_region: str = "us-east-1"
    s3_use_ssl: bool = False
    data_prefix: str = "lake/"
    extension_dir: str | None = None

    def __repr__(self) -> str:  # never leak secrets into logs
        return (
            f"LakeSettings(pg={self.pg_user}@{self.pg_host}:{self.pg_port}/{self.pg_db}, "
            f"s3={self.s3_endpoint}/{self.s3_bucket}/{self.data_prefix})"
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> LakeSettings:
        e = os.environ if env is None else env
        missing = [
            k
            for k in (
                "HAMMOND_LAKE_PG_HOST",
                "HAMMOND_LAKE_PG_DB",
                "HAMMOND_LAKE_PG_USER",
                "HAMMOND_LAKE_PG_PASSWORD",
                "HAMMOND_S3_ENDPOINT",
                "HAMMOND_S3_ACCESS_KEY",
                "HAMMOND_S3_SECRET_KEY",
                "HAMMOND_S3_BUCKET",
            )
            if not e.get(k)
        ]
        if missing:
            raise ValueError(f"Missing environment variables: {', '.join(missing)}")
        return cls(
            pg_host=e["HAMMOND_LAKE_PG_HOST"],
            pg_port=int(e.get("HAMMOND_LAKE_PG_PORT", "5432")),
            pg_db=e["HAMMOND_LAKE_PG_DB"],
            pg_user=e["HAMMOND_LAKE_PG_USER"],
            pg_password=e["HAMMOND_LAKE_PG_PASSWORD"],
            s3_endpoint=e["HAMMOND_S3_ENDPOINT"],
            s3_access_key=e["HAMMOND_S3_ACCESS_KEY"],
            s3_secret_key=e["HAMMOND_S3_SECRET_KEY"],
            s3_bucket=e["HAMMOND_S3_BUCKET"],
            s3_region=e.get("HAMMOND_S3_REGION") or "us-east-1",
            s3_use_ssl=_bool(e.get("HAMMOND_S3_USE_SSL"), default=False),
            data_prefix=(e.get("HAMMOND_LAKE_DATA_PREFIX") or "lake/").strip("/") + "/",
            extension_dir=e.get("HAMMOND_DUCKDB_EXTENSION_DIR") or None,
        )

    @property
    def catalog_dsn(self) -> str:
        parts = {
            "host": self.pg_host,
            "port": str(self.pg_port),
            "dbname": self.pg_db,
            "user": self.pg_user,
            "password": self.pg_password,
        }
        return " ".join(f"{k}={_libpq_value(v)}" for k, v in parts.items())

    @property
    def data_path(self) -> str:
        return f"s3://{self.s3_bucket}/{self.data_prefix}"


def secret_sql(settings: LakeSettings) -> str:
    return (
        "CREATE OR REPLACE TEMPORARY SECRET hammond_s3 ("
        "TYPE s3, "
        f"KEY_ID {_sql_literal(settings.s3_access_key)}, "
        f"SECRET {_sql_literal(settings.s3_secret_key)}, "
        f"ENDPOINT {_sql_literal(settings.s3_endpoint)}, "
        f"REGION {_sql_literal(settings.s3_region)}, "
        "URL_STYLE 'path', "
        f"USE_SSL {'true' if settings.s3_use_ssl else 'false'}, "
        f"SCOPE {_sql_literal(f's3://{settings.s3_bucket}')})"
    )


def attach_sql(settings: LakeSettings, alias: str = LAKE_ALIAS) -> str:
    # Data inlining is disabled: all rows go to object storage, so that access control
    # on storage prefixes (phase 2) covers all data, not only larger inserts.
    return (
        f"ATTACH {_sql_literal('ducklake:postgres:' + settings.catalog_dsn)} AS {alias} "
        f"(DATA_PATH {_sql_literal(settings.data_path)}, DATA_INLINING_ROW_LIMIT 0)"
    )


def connect(settings: LakeSettings, *, alias: str = LAKE_ALIAS) -> duckdb.DuckDBPyConnection:
    """Open an in-memory DuckDB connection with the lake attached as ``alias``."""
    config: dict[str, Any] = {"autoinstall_known_extensions": False}
    if settings.extension_dir:
        config["extension_directory"] = settings.extension_dir
    con = duckdb.connect(config=config)
    try:
        for ext in REQUIRED_EXTENSIONS:
            con.load_extension(ext)
        con.execute(secret_sql(settings))
        con.execute(attach_sql(settings, alias))
    except Exception:
        con.close()
        raise
    return con


def install_extensions(extension_dir: str | None = None) -> None:
    """Download required extensions (image build time, not at runtime)."""
    config: dict[str, Any] = {}
    if extension_dir:
        config["extension_directory"] = extension_dir
    with duckdb.connect(config=config) as con:
        for ext in REQUIRED_EXTENSIONS:
            con.install_extension(ext)


def init(settings: LakeSettings) -> None:
    with connect(settings) as con:
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {LAKE_ALIAS}.meta")
        con.execute(
            f"CREATE TABLE IF NOT EXISTS {LAKE_ALIAS}.meta.healthcheck "
            "(check_id VARCHAR, checked_at TIMESTAMPTZ)"
        )
    logger.info("DuckLake initialised: %r", settings)


def check(settings: LakeSettings) -> None:
    """Write and read back a row; the data file lands in object storage."""
    check_id = str(uuid.uuid4())
    with connect(settings) as con:
        con.execute("INSERT INTO lake.meta.healthcheck VALUES (?, now())", [check_id])
        row = con.execute(
            "SELECT count(*) FROM lake.meta.healthcheck WHERE check_id = ?",
            [check_id],
        ).fetchone()
        files = con.execute(
            "SELECT count(*) FROM ducklake_list_files(?, 'healthcheck', schema => 'meta')",
            [LAKE_ALIAS],
        ).fetchone()
    if row is None or row[0] != 1:
        raise RuntimeError("DuckLake round-trip failed")
    logger.info(
        "DuckLake round-trip OK (%s data files in object storage)", files[0] if files else 0
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hacore.lake")
    parser.add_argument("command", choices=["init", "check", "install-extensions"])
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if args.command == "install-extensions":
        install_extensions(os.environ.get("HAMMOND_DUCKDB_EXTENSION_DIR"))
        return 0
    settings = LakeSettings.from_env()
    if args.command == "init":
        init(settings)
    check(settings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
