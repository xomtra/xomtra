from collections.abc import AsyncIterator

import pytest
from fastmcp import Client
from fastmcp.server.auth.providers.azure import AzureProvider
from pydantic import SecretStr

from hacore.testing import VALID_TOKEN, FakeHA, running_fake_ha
from xomtra_mcp.config import Settings
from xomtra_mcp.identity import identity_from_claims
from xomtra_mcp.server import build_auth, create_server


@pytest.fixture
async def fake_ha() -> AsyncIterator[tuple[FakeHA, str]]:
    async with running_fake_ha() as running:
        yield running


def dev_settings(**kwargs: object) -> Settings:
    return Settings(auth_mode="none", host="127.0.0.1", **kwargs)  # type: ignore[arg-type]


async def test_tools_without_ha() -> None:
    async with Client(create_server(dev_settings())) as client:
        names = {t.name for t in await client.list_tools()}
        assert names == {"whoami", "server_info"}
        info = (await client.call_tool("server_info", {})).data
        assert info["home_assistant_configured"] is False
        who = (await client.call_tool("whoami", {})).data
        assert who["authenticated"] is False


async def test_ha_overview(fake_ha: tuple[FakeHA, str]) -> None:
    fake, url = fake_ha
    fake.results.update(
        {
            "config/floor_registry/list": [],
            "config/area_registry/list": [{"area_id": "a", "name": "A"}],
            "config/label_registry/list": [],
            "config/device_registry/list": [],
            "config/entity_registry/list": [
                {"entity_id": "sensor.x"},
                {"entity_id": "light.y"},
            ],
        }
    )
    server = create_server(dev_settings(ha_url=url, ha_token=SecretStr(VALID_TOKEN)))
    async with Client(server) as client:
        result = (await client.call_tool("ha_overview", {})).data
    assert result["areas"] == 1
    assert result["entities_by_domain"] == {"light": 1, "sensor": 1}


def test_build_auth_entra() -> None:
    settings = Settings(
        auth_mode="entra",
        base_url="https://mcp.example.org",  # type: ignore[arg-type]
        entra_tenant_id="00000000-0000-0000-0000-000000000001",
        entra_client_id="00000000-0000-0000-0000-000000000002",
        entra_client_secret=SecretStr("secret"),
        jwt_signing_key=SecretStr("k" * 48),
        audit_pg_host="postgres",
        audit_pg_password=SecretStr("pw"),
    )
    auth = build_auth(settings)
    assert isinstance(auth, AzureProvider)
    assert auth.identifier_uri == "api://00000000-0000-0000-0000-000000000002"


def test_identity_from_nested_claims() -> None:
    ident = identity_from_claims(
        {
            "upstream_claims": {
                "oid": "o1",
                "name": "Jane",
                "preferred_username": "jane@example.org",
                "roles": ["xomtra.Reader"],
            }
        },
        ["api://x/mcp.access"],
    )
    assert ident.subject == "o1"
    assert ident.roles == ("xomtra.Reader",)
    assert ident.username == "jane@example.org"


def test_identity_from_flat_claims() -> None:
    ident = identity_from_claims({"sub": "s1", "upn": "u@example.org"}, [])
    assert ident.subject == "s1"
    assert ident.roles == ()


async def test_instructions_carry_the_default_guardrails() -> None:
    """ADR 073: the client puts the server instructions in the model's context."""
    text = create_server(dev_settings()).instructions or ""
    assert "hypotheses, not as causes" in text
    assert "Do not invent values" in text
    assert "local time" in text
    assert "presence, sleep, health" in text
    assert "locks, alarms" in text
    assert "friendly and warm" in text
