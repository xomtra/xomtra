from pathlib import Path

import pytest

from hammond_mcp.data import KNOWN_VIEWS
from hammond_mcp.identity import Identity
from hammond_mcp.policy import AccessDenied, Policy, PolicyError

POLICY = Policy.load()


def ident(*roles: str, authenticated: bool = True) -> Identity:
    return Identity(authenticated=authenticated, subject="s", roles=roles)


def test_no_role_only_whoami() -> None:
    access = POLICY.access_for(ident(), KNOWN_VIEWS)
    assert access.can_call("whoami")
    assert not access.can_call("query_sql")
    assert access.allowed_views == []
    unauth = POLICY.access_for(ident("HAmmond.Admin", authenticated=False), KNOWN_VIEWS)
    assert not unauth.can_call("query_sql")


def test_reader_and_admin() -> None:
    reader = POLICY.access_for(ident("HAmmond.Reader"), KNOWN_VIEWS)
    assert reader.can_call("query_sql")
    assert not reader.can_call("ha_overview")
    assert "catalog_drift" not in reader.allowed_views
    assert not reader.entity_allowed("person.jana")
    assert not reader.entity_allowed("sensor.phone_geocoded_location")
    assert reader.entity_allowed("sensor.temp")
    with pytest.raises(AccessDenied):
        reader.require_entity("device_tracker.phone")
    assert reader.max_rows == 1000

    both = POLICY.access_for(ident("HAmmond.Reader", "HAmmond.Admin"), KNOWN_VIEWS)
    assert both.entity_allowed("person.jana")  # only what every role denies stays denied
    assert both.can_call("ha_overview")
    assert both.allowed_views == sorted(KNOWN_VIEWS)
    assert both.max_rows == 10000


def test_unknown_role_ignored() -> None:
    assert not POLICY.access_for(ident("Other"), KNOWN_VIEWS).can_call("search_catalog")


def test_invalid_policies(tmp_path: Path) -> None:
    with pytest.raises(PolicyError):
        Policy.parse({"roles": []})
    with pytest.raises(PolicyError, match="patterns"):
        Policy.parse({"roles": {"R": {"deny_entities": ["x'; drop"]}}})
    with pytest.raises(PolicyError, match="max_rows"):
        Policy.parse({"roles": {"R": {"max_rows": 0}}})
    f = tmp_path / "p.yaml"
    f.write_text("roles:\n  R:\n    tools: [whoami, list_views]\n    views: [states]\n")
    access = Policy.load(f).access_for(ident("R"), KNOWN_VIEWS)
    assert access.allowed_views == ["states"]
