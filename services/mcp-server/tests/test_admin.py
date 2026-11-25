import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
from joserfc import jwt
from joserfc.jwk import RSAKey
from pydantic import SecretStr

from hacore.testing import VALID_TOKEN, running_fake_ha
from xomtra_mcp.admin import status
from xomtra_mcp.admin.app import SESSION_COOKIE, AdminApp
from xomtra_mcp.admin.config import AdminSettings
from xomtra_mcp.admin.oidc import EntraOIDC
from xomtra_mcp.admin.session import Signer
from xomtra_mcp.audit import MemoryAuditSink
from xomtra_mcp.data import DataAccess

TENANT = "00000000-0000-0000-0000-000000000001"
CLIENT = "00000000-0000-0000-0000-000000000002"
BASE = "https://mcp.example.org"
KEY = RSAKey.generate_key(2048, parameters={"kid": "k1"})


def test_signer_roundtrip_tamper_expiry() -> None:
    s = Signer("x" * 40, "session")
    token = s.dumps({"a": 1}, 60)
    assert s.loads(token) is not None
    assert s.loads(token)["a"] == 1  # type: ignore[index]
    assert s.loads(token[:-2] + "AA") is None
    assert Signer("x" * 40, "login").loads(token) is None  # other purpose
    assert s.loads(s.dumps({"a": 1}, 60, now=time.time() - 120)) is None
    assert s.loads("garbage") is None


class FakeEntra:
    """Token and JWKS endpoints of Entra; issues ID tokens with the given roles."""

    def __init__(self) -> None:
        self.roles: list[str] = ["Xomtra.Admin"]
        self.nonce = ""
        self.tenant = TENANT

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/discovery/v2.0/keys"):
            return httpx2.Response(200, json={"keys": [KEY.as_dict(private=False)]})
        if request.url.path.endswith("/oauth2/v2.0/token"):
            form = parse_qs(request.content.decode())
            assert form["code_verifier"]
            claims = {
                "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
                "aud": CLIENT,
                "exp": int(time.time()) + 600,
                "iat": int(time.time()),
                "nonce": self.nonce,
                "tid": self.tenant,
                "oid": "user-oid",
                "name": "Admin",
                "preferred_username": "admin@example.org",
                "roles": self.roles,
            }
            token = jwt.encode({"alg": "RS256", "kid": "k1"}, claims, KEY)
            return httpx2.Response(200, json={"id_token": token})
        return httpx2.Response(404)


def settings(**kwargs: Any) -> AdminSettings:
    values: dict[str, Any] = {
        "base_url": BASE,
        "entra_tenant_id": TENANT,
        "entra_client_id": CLIENT,
        "entra_client_secret": SecretStr("secret"),
        "admin_session_key": SecretStr("s" * 40),
    }
    values.update(kwargs)
    return AdminSettings(**values)


def make_app(entra: FakeEntra, data: DataAccess | None, audit: MemoryAuditSink) -> Any:
    s = settings()
    oidc = EntraOIDC(
        tenant_id=TENANT,
        client_id=CLIENT,
        client_secret="secret",
        redirect_uri=s.redirect_uri,
        http=httpx2.AsyncClient(transport=httpx2.MockTransport(entra.handler)),
    )
    return AdminApp(s, data=data, audit=audit, oidc=oidc).build()


async def sign_in(client: httpx2.AsyncClient, entra: FakeEntra) -> httpx2.Response:
    start = await client.get("/admin/login")
    assert start.status_code == 303
    query = parse_qs(urlparse(start.headers["location"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == [f"{BASE}/admin/auth/callback"]
    entra.nonce = query["nonce"][0]
    return await client.get(
        "/admin/auth/callback", params={"code": "c", "state": query["state"][0]}
    )


def client_for(app: Any) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE)


async def test_admin_sign_in_pages_and_audit(data: DataAccess) -> None:
    entra, audit = FakeEntra(), MemoryAuditSink()
    async with client_for(make_app(entra, data, audit)) as client:
        assert (await client.get("/admin/")).status_code == 303  # not signed in
        assert (await client.get("/admin/auth/check")).status_code == 302
        done = await sign_in(client, entra)
        assert done.status_code == 303
        assert SESSION_COOKIE in client.cookies
        for path, text in [
            ("/admin/", "Catalog"),
            ("/admin/catalog?q=teplota", "sensor.temp"),
            ("/admin/drift", "Drift"),
            ("/admin/retention", "XOMTRA_STATES_RETENTION_DAYS"),
            ("/admin/audit", "not configured"),
        ]:
            resp = await client.get(path)
            assert resp.status_code == 200, path
            assert text in resp.text, path
            assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]
        check = await client.get("/admin/auth/check")
        assert check.status_code == 200
        assert check.headers["x-xomtra-user"] == "admin@example.org"
        out = await client.post("/admin/logout", headers={"origin": BASE})
        assert out.status_code == 303
        assert (await client.get("/admin/")).status_code == 303
    tools = [r.tool for r in audit.records]
    assert tools[:2] == ["admin:login", "admin:overview"]
    assert audit.records[0].username == "admin@example.org"


async def test_catalog_escapes_input(data: DataAccess) -> None:
    entra, audit = FakeEntra(), MemoryAuditSink()
    async with client_for(make_app(entra, data, audit)) as client:
        await sign_in(client, entra)
        resp = await client.get("/admin/catalog", params={"q": "<script>x</script>"})
    assert "<script>x" not in resp.text
    assert "&lt;script&gt;" in resp.text


async def test_sign_in_without_admin_role_is_denied() -> None:
    entra, audit = FakeEntra(), MemoryAuditSink()
    entra.roles = ["Xomtra.Reader"]
    async with client_for(make_app(entra, None, audit)) as client:
        resp = await sign_in(client, entra)
        assert resp.status_code == 403
        assert SESSION_COOKIE not in client.cookies
    assert [(r.tool, r.decision) for r in audit.records] == [("admin:login", "denied")]


async def test_callback_rejects_bad_state_and_tenant() -> None:
    entra, audit = FakeEntra(), MemoryAuditSink()
    async with client_for(make_app(entra, None, audit)) as client:
        await client.get("/admin/login")
        bad = await client.get("/admin/auth/callback", params={"code": "c", "state": "x"})
        assert bad.status_code == 400
        entra.tenant = "other-tenant"
        resp = await sign_in(client, entra)
        assert resp.status_code == 400
        assert "not valid" in resp.text


async def test_forged_session_cookie_is_ignored() -> None:
    forged = Signer("another-key" * 4, "session").dumps(
        {"sub": "x", "roles": ["Xomtra.Admin"]}, 3600
    )
    async with client_for(make_app(FakeEntra(), None, MemoryAuditSink())) as client:
        client.cookies.set(SESSION_COOKIE, forged, path="/admin")
        assert (await client.get("/admin/")).status_code == 303


async def test_logout_refuses_cross_site() -> None:
    async with client_for(make_app(FakeEntra(), None, MemoryAuditSink())) as client:
        resp = await client.post("/admin/logout", headers={"origin": "https://evil.example"})
    assert resp.status_code == 403


def test_settings_require_entra_and_key() -> None:
    with pytest.raises(ValueError, match="XOMTRA_ADMIN_SESSION_KEY"):
        settings(admin_session_key=None)
    with pytest.raises(ValueError, match="32 characters"):
        settings(admin_session_key=SecretStr("short"))
    s = settings(audit_pg_host="postgres", audit_admin_password=SecretStr("p"))
    assert s.audit_dsn is not None
    assert "user='xomtra_audit_admin'" in s.audit_dsn


async def test_ha_status() -> None:
    assert await status.ha_status(None, None) == {"configured": False}
    async with running_fake_ha() as (fake, url):
        fake.results.update(
            {
                "config/floor_registry/list": [],
                "config/area_registry/list": [{"area_id": "a", "name": "A"}],
                "config/label_registry/list": [],
                "config/device_registry/list": [],
                "config/entity_registry/list": [{"entity_id": "sensor.x"}],
            }
        )
        ok = await status.ha_status(url, VALID_TOKEN)
        bad = await status.ha_status(url, "wrong-token")
    assert ok["ok"] is True
    assert ok["entities"] == 1
    assert bad["ok"] is False
