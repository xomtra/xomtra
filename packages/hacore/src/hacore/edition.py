"""Editions of xomtra (ADR 050): one code base, two resource profiles.

``full``  Docker compose / VM / AWS: object storage (S3, SeaweedFS), Dagster, longer
          retention, a larger DuckDB memory budget.
``lite``  Home Assistant add-on and small machines: lake on the local file system, the
          built-in worker instead of Dagster, shorter default retention and a smaller
          DuckDB budget so that the whole stack stays within 2 GB RAM at rest.

The edition only changes *defaults*; every value can still be set explicitly with its
own ``XOMTRA_*`` variable. Choose it with ``XOMTRA_EDITION`` (default ``full``, so
existing installations keep their behaviour).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

ENV_VAR = "XOMTRA_EDITION"


class Edition(StrEnum):
    FULL = "full"
    LITE = "lite"


@dataclass(frozen=True, slots=True)
class Profile:
    """Defaults of an edition."""

    states_retention_days: int
    statistics_5min_retention_days: int
    statistics_hour_retention_days: int
    duckdb_memory_limit: str
    duckdb_threads: int


PROFILES: dict[Edition, Profile] = {
    Edition.FULL: Profile(
        states_retention_days=365,
        statistics_5min_retention_days=365,
        statistics_hour_retention_days=5 * 365,
        duckdb_memory_limit="2GB",
        duckdb_threads=2,
    ),
    # States are by far the largest table (~40-50 GB a year on a large installation);
    # hourly statistics are small and carry the long-term analyses.
    Edition.LITE: Profile(
        states_retention_days=90,
        statistics_5min_retention_days=90,
        statistics_hour_retention_days=5 * 365,
        duckdb_memory_limit="512MB",
        duckdb_threads=2,
    ),
}


def edition_from_env(env: Mapping[str, str] | None = None) -> Edition:
    value = (os.environ if env is None else env).get(ENV_VAR, "").strip().lower()
    if not value:
        return Edition.FULL
    try:
        return Edition(value)
    except ValueError:
        allowed = ", ".join(e.value for e in Edition)
        raise ValueError(f"{ENV_VAR} must be one of: {allowed} (got {value!r})") from None


def profile(edition: Edition | None = None, env: Mapping[str, str] | None = None) -> Profile:
    return PROFILES[edition or edition_from_env(env)]
