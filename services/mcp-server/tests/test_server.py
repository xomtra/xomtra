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


async def test_instructions_carry_the_core_rules_and_the_default_set() -> None:
    """ADR 073, 090: the client puts the server instructions in the model's context."""
    text = " ".join((create_server(dev_settings()).instructions or "").split())
    # Core rules (not editable)
    assert "No jokes, irony or vulgarity about accidents, faults, outages" in text
    assert "threatens life or health" in text
    assert "sexist, racist, homophobic or xenophobic jokes or innuendo" in text
    assert "nothing politically or religiously tinged" in text
    assert "presence, sleep, health or habits" in text
    assert "never invent data" in text
    assert "only reads data and cannot control the house" in text
    assert "locks, alarms" in text
    # The default factory set: relevance and readable presentation, friendly tone.
    assert 'Answer guidelines set "Default"' in text
    assert "Answer first" in text
    assert "line chart" in text
    assert "hypotheses" in text
    assert "local time" in text
    assert "Friendly and warm" in text
    assert "crude" not in text
    assert "pub" not in text
    assert "win where they differ" in text


async def test_guidelines_reach_the_model_without_instructions() -> None:
    """ADR 080: proxies may drop the instructions; server_info carries the guidelines."""
    async with Client(create_server(dev_settings())) as client:
        info = (await client.call_tool("server_info", {})).data
        tools = {t.name: t for t in await client.list_tools()}
    assert "Answer first" in info["answer_guidelines"]
    assert "guidelines" in (tools["server_info"].description or "")


async def test_active_set_is_read_for_every_call() -> None:
    """ADR 090: a set chosen in the admin GUI applies without a restart; core first."""
    from xomtra_mcp.answer_sets import MemoryAnswerSetStore
    from xomtra_mcp.guidelines import CORE_ONLY_SET_ID, Guidelines

    store = MemoryAnswerSetStore()
    guidelines = Guidelines(store.active_set, ttl=0)
    srv = create_server(dev_settings(), guidelines=guidelines)
    set_id = store.create("Haiku", "", "Answer in a haiku.", by="admin")
    store.activate(set_id, by="admin")
    async with Client(srv) as client:
        text = (await client.call_tool("server_info", {})).data["answer_guidelines"]
        assert 'set "Haiku"' in text
        assert text.index("threatens life") < text.index("haiku")
        store.update(set_id, "Haiku", "", "Answer in two haiku.", by="admin")
        text = (await client.call_tool("server_info", {})).data["answer_guidelines"]
        assert "two haiku" in text
        store.activate(CORE_ONLY_SET_ID, by="admin")
        text = (await client.call_tool("server_info", {})).data["answer_guidelines"]
        assert "haiku" not in text
        assert "Answer first" not in text
        assert "threatens life or health" in text
        # A deleted custom set falls back to the default set.
        store.activate(set_id, by="admin")
        store.delete(set_id, by="admin")
        text = (await client.call_tool("server_info", {})).data["answer_guidelines"]
        assert "Answer first" in text


def test_store_outage_keeps_the_last_set() -> None:
    from xomtra_mcp.guidelines import FACTORY_BY_ID, Guidelines

    calls = {"n": 0}

    def source():  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("down")
        return FACTORY_BY_ID["factory:concise"]

    guidelines = Guidelines(source, ttl=0)
    assert guidelines.active().name == "Concise"
    assert guidelines.active().name == "Concise"
    assert calls["n"] == 2


def test_check_set_limits() -> None:
    from xomtra_mcp.guidelines import TEXT_MAX, check_set, clean_text

    assert check_set("Moje sada", "", "text") == []
    assert check_set("", "", "x")
    assert check_set("<b>", "", "x")
    assert check_set("ok", "", "x" * (TEXT_MAX + 1))
    assert check_set("ok", "", "a\x00b")
    assert clean_text("a  \r\n\r\n\r\n\nb ") == "a\n\nb"
