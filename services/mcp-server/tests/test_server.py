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
    text = " ".join((create_server(dev_settings()).instructions or "").split())
    assert "hypotheses, not as causes" in text
    assert "Do not invent values" in text
    assert "local time" in text
    assert "presence, sleep, health" in text
    assert "locks, alarms" in text
    assert "occupant of the property" in text
    # Default style: friendly with light humour; the pub style is an option.
    assert "friendly and warm" in text
    assert "crude" not in text
    assert "Joke about the user only with tact" in text
    assert "sexist, racist, homophobic or xenophobic jokes or innuendo" in text
    assert "no politically or religiously tinged jokes" in text
    assert "No jokes, irony or vulgarity about accidents, faults, outages" in text
    assert "threatens life or health" in text


async def test_pub_style_is_an_option() -> None:
    """ADR 080: the crude pub style, personal humour included, only when chosen."""
    settings = dev_settings().model_copy(update={"answer_style": "pub"})
    text = " ".join((create_server(settings).instructions or "").split())
    assert "very funny, crude, vulgar and a bit politically incorrect" in text
    assert "never in the areas of the core rules" in text
    assert "language, country, culture and locality of the installation" in text
    assert "never guess the user's wealth or social status" in text
    assert "guys in the pub" in text
    assert "property owner's line of work" in text
    assert "from your own context (your memory, their profile, this conversation)" in text
    assert "Never infer personal details from the house data" in text
    assert "Never name the user's clients or employers" in text
    assert "no politically or religiously tinged jokes" in text
    assert "friendly and warm" not in text


async def test_guidelines_reach_the_model_without_instructions() -> None:
    """ADR 080: proxies may drop the instructions; server_info carries the guidelines."""
    from fastmcp import Client

    async with Client(create_server(dev_settings())) as client:
        info = (await client.call_tool("server_info", {})).data
        tools = {t.name: t for t in await client.list_tools()}
    assert "hypotheses, not as causes" in info["answer_guidelines"]
    assert "guidelines" in (tools["server_info"].description or "")


async def test_default_guidelines_can_be_turned_off() -> None:
    """ADR 080: the operator may turn the default answer style and rules off."""
    from fastmcp import Client

    settings = dev_settings().model_copy(update={"default_guidelines": False})
    srv = create_server(settings)
    assert "crude" not in (srv.instructions or "")
    instructions = " ".join((srv.instructions or "").split())
    assert "politically or religiously tinged" in instructions
    assert "threatens life or health" in instructions
    async with Client(srv) as client:
        info = (await client.call_tool("server_info", {})).data
    # The core rules stay, the default style goes.
    assert "politically or religiously tinged" in " ".join(info["answer_guidelines"].split())
    assert "crude" not in info["answer_guidelines"]


async def test_central_theme_takes_precedence_and_expires() -> None:
    """ADR 080: a central theme (ADR 075) comes first and is forgotten after 24 hours."""
    from datetime import UTC, datetime, timedelta

    from xomtra_mcp.guidelines import Guidelines, Theme

    now = datetime(2026, 12, 1, 12, tzinfo=UTC)
    theme = Theme("Christmas: add a festive touch.", received_at=now)
    guidelines = Guidelines(defaults=True, theme=lambda: theme)
    text = guidelines.text(now) or ""
    assert text.index("religiously tinged") < text.index("Christmas")
    assert text.index("Christmas") < text.index("occupant of the property")
    assert "never over the core" in " ".join(text.split())
    assert "2026-12-02 12:00 UTC" in text
    assert "Christmas" not in (guidelines.text(now + timedelta(hours=24)) or "")
    # A longer validity from the centre does not extend the 24 hours.
    long = Theme("x", received_at=now, valid_until=now + timedelta(days=7))
    assert not long.active(now + timedelta(hours=25))
    # The theme applies even with the default guidelines off.
    off = Guidelines(defaults=False, theme=lambda: theme)
    assert "politically or religiously tinged" in off.tool_rules
    assert "Christmas" in off.text(now)
    assert "occupant" not in off.text(now)
    assert "Christmas" not in off.text(now + timedelta(days=2))
