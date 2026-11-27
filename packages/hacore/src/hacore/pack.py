"""Provenance of findings: analyzer, parameter pack and source instance (ADR 089, P0).

Every finding records which analyzer produced it, from which parameter pack (the
domain-specific parameters, rules and guardrails) in which version, and from which
instance of a data source. xomtra has one pack (``home``) and one Home Assistant
instance per installation; the columns exist now so that the schema of the findings
does not change when the code is split into a domain-neutral core and plugins.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

PACK = "home"
SOURCE_INSTANCE = "ha"


def _version() -> str:
    try:
        return version("hacore")
    except PackageNotFoundError:  # pragma: no cover - source tree without metadata
        return "unknown"


PACK_VERSION = _version()
