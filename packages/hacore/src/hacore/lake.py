"""DuckLake access: Postgres catalog + data files in S3 compatible object storage
(SeaweedFS, full edition) or on the local file system (lite edition, ADR 050).

Configuration comes from environment variables (see ``LakeSettings.from_env``) so that
no installation specific values ever live in the repository. ``HAMMOND_LAKE_DATA_PATH``
set to an absolute local path selects local data files; the ``HAMMOND_S3_*`` variables
are then not needed.

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
from pathlib import PurePosixPath
from typing import Any

import duckdb

from hacore.edition import profile

logger = logging.getLogger(__name__)

REQUIRED_EXTENSIONS = ("ducklake", "postgres", "httpfs")
# httpfs is only loaded when the data files are in object storage.
OBJECT_STORAGE_EXTENSIONS = ("httpfs",)
LAKE_ALIAS = "lake"


def sql_literal(value: str) -> str:
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
    s3_endpoint: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_bucket: str = ""
    pg_port: int = 5432
    s3_region: str = "us-east-1"
    s3_use_ssl: bool = False
    data_prefix: str = "lake/"
    # Absolute local directory of the data files (lite edition); None = object storage.
    local_data_path: str | None = None
    extension_dir: str | None = None
    # DuckDB resources (ADR 031): a bounded memory budget that spills to disk instead of
    # taking the whole VM. ``temp_directory`` should be on a disk volume, not on tmpfs.
    memory_limit: str = "2GB"
    threads: int = 2
    temp_directory: str | None = None
    max_temp_directory_size: str = "8GB"

    def __post_init__(self) -> None:
        if self.local_data_path is not None:
            if not PurePosixPath(self.local_data_path).is_absolute():
                raise ValueError("HAMMOND_LAKE_DATA_PATH must be an absolute path")
        elif not all((self.s3_endpoint, self.s3_access_key, self.s3_secret_key, self.s3_bucket)):
            raise ValueError("object storage needs HAMMOND_S3_* or HAMMOND_LAKE_DATA_PATH")

    def __repr__(self) -> str:  # never leak secrets into logs
        return (
            f"LakeSettings(pg={self.pg_user}@{self.pg_host}:{self.pg_port}/{self.pg_db}, "
            f"data={self.data_path if self.is_local else self.s3_endpoint + '/' + self.data_path})"
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> LakeSettings:
        e = os.environ if env is None else env
        local = (e.get("HAMMOND_LAKE_DATA_PATH") or "").strip() or None
        required = [
            "HAMMOND_LAKE_PG_HOST",
            "HAMMOND_LAKE_PG_DB",
            "HAMMOND_LAKE_PG_USER",
            "HAMMOND_LAKE_PG_PASSWORD",
        ]
        if local is None:
            required += [
                "HAMMOND_S3_ENDPOINT",
                "HAMMOND_S3_ACCESS_KEY",
                "HAMMOND_S3_SECRET_KEY",
                "HAMMOND_S3_BUCKET",
            ]
        missing = [k for k in required if not e.get(k)]
        if missing:
            raise ValueError(f"Missing environment variables: {', '.join(missing)}")
        defaults = profile(env=e)
        return cls(
            pg_host=e["HAMMOND_LAKE_PG_HOST"],
            pg_port=int(e.get("HAMMOND_LAKE_PG_PORT", "5432")),
            pg_db=e["HAMMOND_LAKE_PG_DB"],
            pg_user=e["HAMMOND_LAKE_PG_USER"],
            pg_password=e["HAMMOND_LAKE_PG_PASSWORD"],
            s3_endpoint="" if local else e["HAMMOND_S3_ENDPOINT"],
            s3_access_key="" if local else e["HAMMOND_S3_ACCESS_KEY"],
            s3_secret_key="" if local else e["HAMMOND_S3_SECRET_KEY"],
            s3_bucket="" if local else e["HAMMOND_S3_BUCKET"],
            s3_region=e.get("HAMMOND_S3_REGION") or "us-east-1",
            s3_use_ssl=_bool(e.get("HAMMOND_S3_USE_SSL"), default=False),
            data_prefix=(e.get("HAMMOND_LAKE_DATA_PREFIX") or "lake/").strip("/") + "/",
            local_data_path=local,
            extension_dir=e.get("HAMMOND_DUCKDB_EXTENSION_DIR") or None,
            memory_limit=e.get("HAMMOND_DUCKDB_MEMORY_LIMIT") or defaults.duckdb_memory_limit,
            threads=int(e.get("HAMMOND_DUCKDB_THREADS") or defaults.duckdb_threads),
            temp_directory=e.get("HAMMOND_DUCKDB_TEMP_DIR") or None,
            max_temp_directory_size=e.get("HAMMOND_DUCKDB_MAX_TEMP_SIZE") or "8GB",
        )

    @property
    def is_local(self) -> bool:
        return self.local_data_path is not None

    @property
    def extensions(self) -> tuple[str, ...]:
        """Extensions to load: httpfs only for object storage."""
        if self.is_local:
            return tuple(x for x in REQUIRED_EXTENSIONS if x not in OBJECT_STORAGE_EXTENSIONS)
        return REQUIRED_EXTENSIONS

    def duckdb_config(self) -> dict[str, Any]:
        """DuckDB connection options shared by the ingest and SQLMesh."""
        config: dict[str, Any] = {
            "autoinstall_known_extensions": False,
            "memory_limit": self.memory_limit,
            "threads": self.threads,
            "max_temp_directory_size": self.max_temp_directory_size,
            # Large INSERT ... SELECT need not keep the input order; saves memory.
            "preserve_insertion_order": False,
        }
        if self.extension_dir:
            config["extension_directory"] = self.extension_dir
        if self.temp_directory:
            config["temp_directory"] = self.temp_directory
        return config

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
        if self.local_data_path is not None:
            return self.local_data_path.rstrip("/") + "/"
        return f"s3://{self.s3_bucket}/{self.data_prefix}"


def secret_sql(settings: LakeSettings) -> str:
    return (
        "CREATE OR REPLACE TEMPORARY SECRET hammond_s3 ("
        "TYPE s3, "
        f"KEY_ID {sql_literal(settings.s3_access_key)}, "
        f"SECRET {sql_literal(settings.s3_secret_key)}, "
        f"ENDPOINT {sql_literal(settings.s3_endpoint)}, "
        f"REGION {sql_literal(settings.s3_region)}, "
        "URL_STYLE 'path', "
        f"USE_SSL {'true' if settings.s3_use_ssl else 'false'}, "
        f"SCOPE {sql_literal(f's3://{settings.s3_bucket}')})"
    )


def attach_sql(settings: LakeSettings, alias: str = LAKE_ALIAS, *, read_only: bool = False) -> str:
    # Data inlining is disabled: all rows go to object storage, so that access control
    # on storage prefixes (phase 2) covers all data, not only larger inserts.
    mode = ", READ_ONLY" if read_only else ""
    return (
        f"ATTACH {sql_literal('ducklake:postgres:' + settings.catalog_dsn)} AS {alias} "
        f"(DATA_PATH {sql_literal(settings.data_path)}, DATA_INLINING_ROW_LIMIT 0{mode})"
    )


def connect(
    settings: LakeSettings, *, alias: str = LAKE_ALIAS, read_only: bool = False
) -> duckdb.DuckDBPyConnection:
    """Open an in-memory DuckDB connection with the lake attached as ``alias``."""
    con = duckdb.connect(config=settings.duckdb_config())
    try:
        # Timestamps are stored and compared in UTC regardless of the host time zone.
        con.execute("SET TimeZone = 'UTC'")
        for ext in settings.extensions:
            con.load_extension(ext)
        if not settings.is_local:
            con.execute(secret_sql(settings))
        con.execute(attach_sql(settings, alias, read_only=read_only))
    except Exception:
        con.close()
        raise
    return con


def connect_local(
    catalog_path: str,
    data_path: str,
    *,
    alias: str = LAKE_ALIAS,
    extension_dir: str | None = None,
    read_only: bool = False,
) -> duckdb.DuckDBPyConnection:
    """DuckLake with a local DuckDB catalog and local data files (tests, development)."""
    config: dict[str, Any] = {"autoinstall_known_extensions": False}
    if extension_dir:
        config["extension_directory"] = extension_dir
    con = duckdb.connect(config=config)
    try:
        con.execute("SET TimeZone = 'UTC'")
        con.load_extension("ducklake")
        con.execute(
            f"ATTACH {sql_literal('ducklake:' + catalog_path)} AS {alias} "
            f"(DATA_PATH {sql_literal(data_path)}, DATA_INLINING_ROW_LIMIT 0"
            f"{', READ_ONLY' if read_only else ''})"
        )
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
    if settings.local_data_path is not None:
        # Data files only for the lake's owner (the lite edition runs all services as
        # one user); DuckLake creates the subdirectories itself.
        os.makedirs(settings.local_data_path, mode=0o700, exist_ok=True)
    with connect(settings) as con:
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {LAKE_ALIAS}.meta")
        con.execute(
            f"CREATE TABLE IF NOT EXISTS {LAKE_ALIAS}.meta.healthcheck "
            "(check_id VARCHAR, checked_at TIMESTAMPTZ)"
        )
    logger.info("DuckLake initialised: %r", settings)


def check(settings: LakeSettings) -> None:
    """Write and read back a row; the data file lands in object storage or ``DATA_PATH``."""
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
        "DuckLake round-trip OK (%s data files in %s)",
        files[0] if files else 0,
        "local data path" if settings.is_local else "object storage",
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
