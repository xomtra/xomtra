"""Sign-in with Home Assistant (ADR 053) and the local access mode with personal tokens
(ADR 056/059): configuration, verifiers, MCP endpoint and the Claude Desktop bridge."""

import io
import json
import shutil
import socket
import subprocess
import threading
import time
import zipfile
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest
import uvicorn
from fastmcp.server.auth import TokenVerificationError
from mcp.server.auth.provider import TokenError
from pydantic import SecretStr, ValidationError

from hacore.testing import VALID_TOKEN, FakeHA, running_fake_ha
from xomtra_mcp import desktop
from xomtra_mcp.audit import MemoryAuditSink
from xomtra_mcp.config import Settings
from xomtra_mcp.ha_auth import (
    DEFAULT_ROLE_MAP,
    HAUser,
    HomeAssistantProvider,
    HomeAssistantTokenVerifier,
    RoleMap,
    roles_for,
)
from xomtra_mcp.identity import identity_from_claims
from xomtra_mcp.server import build_auth, create_server
from xomtra_mcp.tokens import (
    MemoryTokenStore,
    PersonalTokenVerifier,
    TokenStoreError,
    expiry,
    token_hash,
)

BASE = "https://xomtra.example.org"
AUDIT = {"audit_pg_host": "postgres", "audit_pg_password": SecretStr("pw")}
ADMIN_USER = {"id": "u1", "name": "Jana", "is_owner": False, "is_admin": True}
PLAIN_USER = {"id": "u2", "name": "Petr", "is_owner": False, "is_admin": False}


@pytest.fixture
async def fake_ha() -> AsyncIterator[tuple[FakeHA, str]]:
    async with running_fake_ha() as running:
        yield running


# ---- roles ---------------------------------------------------------------------------


def test_role_map_parse() -> None:
    assert RoleMap.parse("admin=xomtra.Admin,user=") == RoleMap()
    assert RoleMap.parse(DEFAULT_ROLE_MAP) == RoleMap(user=None)
    assert RoleMap.parse("admin=xomtra.Admin,user=xomtra.Reader") == RoleMap(user="xomtra.Reader")
    with pytest.raises(ValueError, match="XOMTRA_HA_ROLE_MAP"):
        RoleMap.parse("system-admin=xomtra.Admin")


def test_roles_for() -> None:
    admin, plain = HAUser.from_reply(ADMIN_USER), HAUser.from_reply(PLAIN_USER)
    owner = HAUser("u0", "Owner", is_owner=True, is_admin=False)
    assert roles_for(admin, RoleMap()) == ["xomtra.Admin"]
    assert roles_for(owner, RoleMap()) == ["xomtra.Admin"]
    assert roles_for(plain, RoleMap()) == []  # by default only administrators
    readers = RoleMap(user="xomtra.Reader")
    assert roles_for(plain, readers) == ["xomtra.Reader"]
    assert roles_for(plain, readers, ["jana", "u9"]) == []
    assert roles_for(admin, readers, ["JANA"]) == ["xomtra.Admin"]
    assert roles_for(plain, readers, ["u2"]) == ["xomtra.Reader"]


# ---- configuration -------------------------------------------------------------------


def test_ha_auth_settings() -> None:
    with pytest.raises(ValidationError, match=r"Home Assistant auth requires: .*XOMTRA_HA_URL"):
        Settings(auth_mode="ha", base_url=BASE, jwt_signing_key=SecretStr("k" * 40), **AUDIT)  # type: ignore[arg-type]
    s = Settings(
        auth_mode="ha",
        base_url=BASE,  # type: ignore[arg-type]
        ha_url="http://ha.lan:8123",
        jwt_signing_key=SecretStr("k" * 40),
        ha_allowed_users="u1, Petr",  # type: ignore[arg-type]
        **AUDIT,
    )
    assert s.browser_ha_url == "http://ha.lan:8123"
    assert s.ha_allowed_users == ["u1", "Petr"]
    with pytest.raises(ValidationError, match="XOMTRA_HA_ROLE_MAP"):
        Settings(auth_mode="ha", ha_role_map="x=y", **AUDIT)  # type: ignore[arg-type]


def test_local_access_needs_only_the_audit_database() -> None:
    with pytest.raises(ValidationError, match="Local access requires"):
        Settings(access_mode="local")
    s = Settings(access_mode="local", **AUDIT)  # type: ignore[arg-type]
    assert isinstance(build_auth(s), PersonalTokenVerifier)


def test_build_auth_ha() -> None:
    s = Settings(
        auth_mode="ha",
        base_url=BASE,  # type: ignore[arg-type]
        ha_url="http://ha.lan:8123",
        ha_auth_url="https://ha.example.org",
        jwt_signing_key=SecretStr("k" * 40),
        **AUDIT,
    )
    auth = build_auth(s)
    assert isinstance(auth, HomeAssistantProvider)
    assert auth._upstream_authorization_endpoint == "https://ha.example.org/auth/authorize"
    assert auth._upstream_token_endpoint == "http://ha.lan:8123/auth/token"
    assert auth._upstream_client_id == f"{BASE}/"
    assert auth._upstream_client_secret is None


# ---- Home Assistant token verifier ---------------------------------------------------


async def test_ha_verifier(fake_ha: tuple[FakeHA, str]) -> None:
    fake, url = fake_ha
    fake.results["auth/current_user"] = ADMIN_USER
    verifier = HomeAssistantTokenVerifier(ha_url=url, role_map=RoleMap())
    token = await verifier.verify_token(VALID_TOKEN)
    assert token is not None
    assert token.claims["roles"] == ["xomtra.Admin"]
    ident = identity_from_claims(token.claims, token.scopes)
    assert (ident.subject, ident.username, ident.idp) == ("u1", "Jana", "ha")
    assert await verifier.verify_token("wrong") is None
    # Cached: a second check does not ask HA again.
    calls = len(fake.received)
    assert await verifier.verify_token(VALID_TOKEN) is not None
    assert len(fake.received) == calls


async def test_ha_verifier_refuses_users_without_role(fake_ha: tuple[FakeHA, str]) -> None:
    fake, url = fake_ha
    fake.results["auth/current_user"] = PLAIN_USER
    verifier = HomeAssistantTokenVerifier(ha_url=url, role_map=RoleMap(user=None))
    assert await verifier.verify_token(VALID_TOKEN) is None


async def test_ha_verifier_unreachable_is_not_a_verdict() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    verifier = HomeAssistantTokenVerifier(ha_url=f"http://127.0.0.1:{port}", role_map=RoleMap())
    with pytest.raises(TokenVerificationError):
        await verifier.verify_token("any")


async def test_provider_refuses_not_allowed_user_at_token_exchange(
    fake_ha: tuple[FakeHA, str],
) -> None:
    fake, url = fake_ha
    fake.results["auth/current_user"] = PLAIN_USER
    provider = HomeAssistantProvider(
        base_url=BASE,
        ha_url=url,
        ha_auth_url=url,
        jwt_signing_key="k" * 40,
        role_map=RoleMap(),
        allowed_users=["u1"],
    )
    with pytest.raises(TokenError):
        await provider._extract_upstream_claims({"access_token": VALID_TOKEN})
    fake.results["auth/current_user"] = ADMIN_USER
    claims = await provider._extract_upstream_claims({"access_token": VALID_TOKEN})
    assert claims is not None
    assert claims["idp"] == "ha"


# ---- personal tokens -----------------------------------------------------------------


def test_memory_store_lifecycle() -> None:
    store = MemoryTokenStore()
    info, token = store.issue(
        owner="jana",
        description="laptop",
        role="xomtra.Reader",
        created_by="admin",
        expires_at=expiry(30),
    )
    assert token.startswith(f"xmt_{info.id}_")
    assert len(token) > 50
    found = store.verify(token_hash(token))
    assert found is not None
    assert found.last_used_at is not None
    assert store.verify(token_hash(token + "x")) is None
    assert store.revoke(info.id, "admin") is True
    assert store.revoke(info.id, "admin") is False
    assert store.verify(token_hash(token)) is None
    _, old = store.issue(
        owner="x",
        description="y",
        role="xomtra.Reader",
        created_by=None,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    assert store.verify(token_hash(old)) is None
    assert expiry(0) is None


async def test_personal_token_verifier() -> None:
    store = MemoryTokenStore()
    info, token = store.issue(
        owner="jana",
        description="laptop",
        role="xomtra.Reader",
        created_by=None,
        expires_at=None,
    )
    verifier = PersonalTokenVerifier(store, cache_seconds=0)
    access = await verifier.verify_token(token)
    assert access is not None
    ident = identity_from_claims(access.claims, access.scopes)
    assert ident.subject == f"token:{info.id}"
    assert ident.username == "jana"
    assert ident.roles == ("xomtra.Reader",)
    assert ident.idp == "token"
    assert await verifier.verify_token("not-a-token") is None
    store.revoke(info.id, None)
    assert await verifier.verify_token(token) is None


async def test_personal_token_verifier_store_down() -> None:
    class Down(MemoryTokenStore):
        def verify(self, digest: bytes) -> Any:
            raise TokenStoreError("down")

    with pytest.raises(TokenVerificationError):
        await PersonalTokenVerifier(Down()).verify_token("xmt_x_y")


# ---- local MCP endpoint over HTTP ----------------------------------------------------


def local_server(store: MemoryTokenStore, audit: MemoryAuditSink) -> Any:
    settings = Settings(access_mode="local", **AUDIT)  # type: ignore[arg-type]
    return create_server(settings, tokens=store, audit=audit)


INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


@asynccontextmanager
async def http_client(server: Any) -> AsyncIterator[httpx2.AsyncClient]:
    app = server.http_app()
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        yield client


async def test_local_endpoint_requires_a_valid_token() -> None:
    store, audit = MemoryTokenStore(), MemoryAuditSink()
    info, token = store.issue(
        owner="jana",
        description="laptop",
        role="xomtra.Reader",
        created_by=None,
        expires_at=None,
    )
    async with http_client(local_server(store, audit)) as client:
        assert (await client.post("/mcp", json=INIT, headers=HEADERS)).status_code == 401
        bad = HEADERS | {"authorization": "Bearer xmt_nope_nope"}
        assert (await client.post("/mcp", json=INIT, headers=bad)).status_code == 401
        ok = await client.post(
            "/mcp", json=INIT, headers=HEADERS | {"authorization": f"Bearer {token}"}
        )
        assert ok.status_code == 200
    assert store.verify(token_hash(token)) is not None
    assert info.id


# ---- Claude Desktop extension --------------------------------------------------------


def test_mcpb_contents() -> None:
    data = desktop.build_mcpb("https://xomtra.lan/mcp", version="1.2.3")
    assert data == desktop.build_mcpb("https://xomtra.lan/mcp", version="1.2.3")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert sorted(archive.namelist()) == ["icon.png", "manifest.json", "server/index.js"]
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["version"] == "1.2.3"
    assert manifest["user_config"]["url"]["default"] == "https://xomtra.lan/mcp"
    assert manifest["user_config"]["token"]["sensitive"] is True
    assert manifest["server"]["mcp_config"]["env"]["XOMTRA_TOKEN"] == "${user_config.token}"
    assert "xmt_" not in json.dumps(manifest["user_config"]["url"])
    assert manifest["user_config"]["allow_http"]["default"] is False


def test_mcpb_for_an_http_endpoint_allows_http() -> None:
    """The add-on serves MCP on plain http in the LAN; its download must work as is."""
    plain = desktop.manifest("http://homeassistant.local:8000/mcp")
    assert plain["user_config"]["allow_http"]["default"] is True
    assert desktop.manifest()["user_config"]["allow_http"]["default"] is False


@pytest.fixture
def running_local_server() -> Iterator[tuple[str, str, MemoryTokenStore]]:
    store, audit = MemoryTokenStore(), MemoryAuditSink()
    _, token = store.issue(
        owner="jana",
        description="desktop",
        role="xomtra.Reader",
        created_by=None,
        expires_at=None,
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    app = local_server(store, audit).http_app()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}/mcp", token, store
    finally:
        server.should_exit = True
        thread.join(5)


def run_bridge(
    url: str,
    token: str,
    lines: list[dict[str, Any]],
    wait_for: int,
    env: dict[str, str] | None = None,
    *,
    required: bool = False,
) -> list[Any]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = Path(desktop.__file__).parent / "server" / "index.js"
    # required: loaded by another module, as Claude Desktop's built-in Node does.
    args = [node, "-e", f"require({json.dumps(str(script))})"] if required else [node, str(script)]
    proc = subprocess.Popen(  # noqa: S603 - test runs our own script
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={"XOMTRA_URL": url, "XOMTRA_TOKEN": token, "PATH": "", **(env or {})},
    )
    assert proc.stdin is not None
    assert proc.stdout is not None
    out: list[Any] = []
    for line in lines:
        proc.stdin.write(json.dumps(line) + "\n")
        proc.stdin.flush()
    while len(out) < wait_for:
        raw = proc.stdout.readline()
        if not raw:
            break
        out.append(json.loads(raw))
    proc.stdin.close()
    proc.wait(10)
    return out


def test_desktop_bridge_end_to_end(running_local_server: tuple[str, str, Any]) -> None:
    url, token, _ = running_local_server
    replies = run_bridge(
        url,
        token,
        [
            INIT,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "whoami", "arguments": {}},
            },
        ],
        wait_for=3,
    )
    by_id = {r["id"]: r for r in replies}
    assert by_id[1]["result"]["serverInfo"]["name"] == "xomtra"
    names = {t["name"] for t in by_id[2]["result"]["tools"]}
    assert {"whoami", "server_info"} <= names
    who = by_id[3]["result"]["structuredContent"]
    assert who["username"] == "jana"
    assert who["identity_provider"] == "token"


def test_desktop_bridge_starts_when_another_module_loads_it(
    running_local_server: tuple[str, str, Any],
) -> None:
    """Claude Desktop's built-in Node loads the script from its own loader, where
    require.main is not the script; the bridge must answer there too."""
    url, token, _ = running_local_server
    replies = run_bridge(url, token, [INIT], wait_for=1, required=True)
    assert replies[0]["result"]["serverInfo"]["name"] == "xomtra"


def test_desktop_bridge_reports_a_refused_token(
    running_local_server: tuple[str, str, Any],
) -> None:
    url, _, _ = running_local_server
    replies = run_bridge(url, "xmt_wrong_token", [INIT], wait_for=1)
    assert "refused the token" in replies[0]["error"]["message"]


def test_desktop_bridge_reports_an_unreachable_server_quickly() -> None:
    """Claude Desktop waits 60 s for an answer; a connection that does not open (macOS
    Local Network permission, wrong address) gets an error with a hint well before."""
    started = time.monotonic()
    replies = run_bridge(
        "http://10.255.255.1:8000/mcp",  # not routed: the connection never opens
        "xmt_x",
        [INIT],
        wait_for=1,
        env={"XOMTRA_ALLOW_HTTP": "true", "XOMTRA_CONNECT_TIMEOUT_MS": "500"},
    )
    assert time.monotonic() - started < 20
    message = replies[0]["error"]["message"]
    assert message.startswith("Cannot reach xomtra at http://10.255.255.1:8000")
    assert "Local Network" in message


def test_desktop_bridge_refuses_plain_http_to_lan() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = Path(desktop.__file__).parent / "server" / "index.js"
    proc = subprocess.run(  # noqa: S603
        [node, str(script)],
        input="",
        capture_output=True,
        text=True,
        env={"XOMTRA_URL": "http://192.168.1.5/mcp", "XOMTRA_TOKEN": "xmt_x", "PATH": ""},
        timeout=10,
        check=False,
    )
    assert proc.returncode == 1
    assert "unencrypted" in proc.stderr
