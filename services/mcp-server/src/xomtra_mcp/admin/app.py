"""Admin GUI web app (Starlette), mounted by Caddy under ``/admin`` (ADR 041).

Access: Caddy lets ``/admin`` through only from ``ADMIN_CIDR``; the app then requires
a sign-in (Entra ID, or a Home Assistant account with ``XOMTRA_AUTH_MODE=ha``, ADR 053)
with the role ``xomtra.Admin``. In the local access mode it also issues and revokes the
personal tokens of MCP clients and offers the Claude Desktop extension (ADR 056/059).
``/admin/auth/check`` answers Caddy's ``forward_auth`` for the Dagster UI under
``/admin/dagster/``. Every page view, sign-in
and refused sign-in is written to the audit table.

In the Home Assistant add-on (``XOMTRA_AUTH_MODE=ingress``, ADR 070) the GUI sits behind
HA Ingress instead: Home Assistant signs the user in, see :class:`RequestView`.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import shutil
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import httpx2
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from hacore.analysis_settings import BY_KEY, RETENTION_BY_KEY, RETENTION_SPECS, SPECS, Spec, Value
from hacore.ha_connection import HAConnection, StoreError, normalize_url
from hacore.ha_connection import save as save_connection
from xomtra_mcp.access import AccessStore, AccessStoreError, PostgresAccessStore
from xomtra_mcp.admin import i18n, setup, status, view
from xomtra_mcp.admin.analysis import (
    FINDING_TYPES,
    PREVIEW_KEYS,
    Change,
    PostgresSettingsStore,
    SettingsStore,
    SettingsStoreError,
    current_findings,
    preview,
    run_confidence,
)
from xomtra_mcp.admin.answers import AnswersPage
from xomtra_mcp.admin.config import PREFIX, AdminSettings
from xomtra_mcp.admin.html import CSS, cards, e, fmt, page, table, token_flow
from xomtra_mcp.admin.oidc import EntraOIDC, LoginError, LoginTransaction
from xomtra_mcp.admin.session import Signer
from xomtra_mcp.admin.view import View, prefix
from xomtra_mcp.answer_sets import AnswerSetStore, PostgresAnswerSetStore
from xomtra_mcp.audit import AuditError, AuditRecord, AuditSink, LogAuditSink
from xomtra_mcp.data import DataAccess
from xomtra_mcp.desktop import FILENAME as MCPB_FILENAME
from xomtra_mcp.desktop import build_mcpb, is_ca_pem
from xomtra_mcp.ha_auth import HomeAssistantLogin, RoleMap, roles_for
from xomtra_mcp.ha_auth import LoginError as HALoginError
from xomtra_mcp.tokens import PostgresTokenStore, TokenStore, TokenStoreError, expiry

logger = logging.getLogger(__name__)

SESSION_COOKIE = "xomtra_admin"
TX_COOKIE = "xomtra_admin_tx"
KINDS = ("measurement", "counter", "binary", "enum", "event", "text", "unknown")
HEADERS = {
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; form-action "
    "'self' https://login.microsoftonline.com",
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
}
# Fonts, icon and theme script of the GUI (xomtra.com look); a fixed allowlist.
STATIC = Path(__file__).parent / "static"
STATIC_TYPES = {
    "Geist-Variable.woff2": "font/woff2",
    "GeistMono-Variable.woff2": "font/woff2",
    "favicon.svg": "image/svg+xml",
    "tips.json": "application/json",
    "ui.js": "text/javascript",
}
TOKEN_ROLES = ("xomtra.Reader", "xomtra.Admin")
# No token without expiry: at most a year, 90 days by default.
TOKEN_DAYS = (30, 90, 365)
DISMISS_DAYS = (0, 30, 90, 365)
# X-Ingress-Path as Home Assistant sets it: /api/hassio_ingress/<token>
INGRESS_PATH = re.compile(r"/api/hassio_ingress/[A-Za-z0-9_-]{1,128}")
ADMIN_GROUP = "system-admin"
MCP_PORT = 8000  # the add-on's MCP port (config.yaml), when no mcp_url is set


def _same_address(peer: str, expected: str) -> bool:
    """Compares IP addresses, also an IPv4-mapped IPv6 peer (``::ffff:a.b.c.d``)."""
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return peer == expected
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    try:
        return address == ipaddress.ip_address(expected)
    except ValueError:
        return False


def connection_error(ha: dict[str, Any]) -> str:
    """Why Home Assistant cannot be reached: a sentence, then the technical detail."""
    detail, hint = ha.get("detail"), ha.get("hint")
    return (f"<p class=bad>{e(ha['error'])}</p>" + (f"<p>{e(hint)}</p>" if hint else "")) + (
        f"<p class=muted>Details: <code>{e(detail)}</code></p>" if detail else ""
    )


def _default_ha_port(url: str) -> str | None:
    """The URL with Home Assistant's port 8123 if it names no port, else None."""
    parts = urlparse(url)
    if parts.port is not None or not parts.hostname:
        return None
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return parts._replace(netloc=f"{host}:8123").geturl()


def ha_host(url: str | None) -> str:
    """The FQDN or IP of the connected Home Assistant, shown instead of its version."""
    parts = urlparse(url or "")
    if parts.hostname == "supervisor":  # the add-on reaches Core through the Supervisor
        return "this Home Assistant"
    return parts.hostname or "?"


def lake_size(n: int | None) -> str | None:
    """Size of the lake's data files (DuckLake catalog), in MiB or GiB."""
    if n is None:
        return None
    return f"{n / 2**30:,.1f} GiB" if n >= 2**30 else f"{n / 2**20:,.1f} MiB"


def _url_host(hostname: str) -> str:
    return f"[{hostname}]" if ":" in hostname else hostname


type Handler = Callable[[Request, dict[str, Any]], Awaitable[Response]]


# The add-on keeps the lake out of Home Assistant backups (config.yaml backup_exclude).
# Shown on the page Retention, in the section Data Lake, above the lake's tables.
BACKUP_NOTE = (
    "<p id=backups class='warn loud'>Home Assistant backups do not include xomtra's "
    "analytics. They keep the tokens, the settings and the dismissed findings, but not "
    "the lake: the history, the findings and the weekly summaries, which take tens of "
    "GB. After a restore xomtra starts a new lake and loads the history again from "
    "Home Assistant, only as far back as Home Assistant still keeps it, and computes "
    "the findings again.<span class=more>For a robust setup with backups of the whole "
    "history, run xomtra on its own virtual machine (Proxmox, VMware or Docker on "
    "Linux) and back up that machine.</span></p>"
)


# Shown on the page Connect clients: the local access mode only serves clients in the LAN.
CLIENTS_NOTE = (
    "<p class=warn><strong>Which AI assistants can connect</strong>"
    "<span class=more>"
    "This server runs in your local network and accepts personal tokens. "
    "Assistants that run on a computer in this network can use it: Claude Desktop"
    " (the extension below), Claude Code and other MCP clients that send a bearer"
    " token, such as VS Code or Cursor."
    "</span><span class=more>"
    "Assistants that connect from the cloud cannot reach it: claude.ai on the web "
    "and on phones, or ChatGPT. claude.ai can connect only to an installation on "
    "its own virtual machine with a public https:// address and sign-in. ChatGPT "
    "and other cloud assistants are not supported yet."
    "</span></p>"
)

# Opening tag of a POST form in the GUI's HTML (the CSRF field goes right after it).
_POST_FORM = re.compile(rb"<form\b[^>]*\bmethod=[\"']?post\b[^>]*>", re.IGNORECASE)


class AdminApp:
    def __init__(
        self,
        settings: AdminSettings,
        *,
        data: DataAccess | None,
        audit: AuditSink | None = None,
        oidc: EntraOIDC | None = None,
        ha_login: HomeAssistantLogin | None = None,
        tokens: TokenStore | None = None,
        analysis: SettingsStore | None = None,
        access: AccessStore | None = None,
        answer_sets: AnswerSetStore | None = None,
    ) -> None:
        self.settings = settings
        self.data = data
        self.audit = audit or LogAuditSink()
        key = settings.admin_session_key
        secret = key.get_secret_value() if key else "dev-only-session-key-not-for-production"
        self.sessions = Signer(secret, "session")
        self._csrf_key = hashlib.sha256(f"csrf:{secret}".encode()).digest()
        self.transactions = Signer(secret, "login")
        self.secure = str(settings.base_url or "").startswith("https://")
        if oidc is None and settings.auth_mode == "entra":
            client_secret = settings.entra_client_secret
            oidc = EntraOIDC(
                tenant_id=settings.entra_tenant_id or "",
                client_id=settings.entra_client_id or "",
                client_secret=client_secret.get_secret_value() if client_secret else "",
                redirect_uri=settings.redirect_uri,
            )
        self.oidc = oidc
        # Home Assistant: from the environment, or entered on the setup page (ADR 077).
        self.ha = settings.ha_resolver()
        self._session_key = key.get_secret_value() if key else None
        self.ha_login = ha_login  # fixed (tests); else built from the current connection
        self._ha_login_built: tuple[tuple[str, str], HomeAssistantLogin] | None = None
        dsn = settings.audit_dsn
        if tokens is None and settings.access_mode == "local" and dsn:
            tokens = PostgresTokenStore(dsn)
        self.tokens = tokens
        if analysis is None and dsn:
            analysis = PostgresSettingsStore(dsn)
        self.analysis = analysis
        if access is None and dsn and settings.auth_mode == "ha":
            access = PostgresAccessStore(dsn)
        self.access = access
        if answer_sets is None and dsn:
            answer_sets = PostgresAnswerSetStore(dsn)
        self.answers = AnswersPage(answer_sets, self._audit, self._form)
        self.ingress = settings.auth_mode == "ingress"
        self.headers = dict(HEADERS)
        if self.ingress:
            # Home Assistant shows the GUI in an iframe of its own origin.
            self.headers["Content-Security-Policy"] = (
                "default-src 'self'; frame-ancestors 'self'; form-action 'self'"
            )
            self.headers["X-Frame-Options"] = "SAMEORIGIN"
        elif ha_login is not None:
            self._allow_form_action(ha_login.ha_auth_url)

    def _allow_form_action(self, ha_auth_url: str) -> None:
        # Sign-out posts a form whose redirects end at Home Assistant's sign-in page.
        origin = urlparse(ha_auth_url)
        self.headers["Content-Security-Policy"] = (
            "default-src 'self'; frame-ancestors 'none'; form-action 'self' "
            f"{origin.scheme}://{origin.netloc}"
        )

    async def _ha_login(self) -> HomeAssistantLogin | None:
        """Sign-in through Home Assistant with the current connection (None: not yet)."""
        if self.settings.auth_mode != "ha":
            return None
        if self.ha_login is not None:
            return self.ha_login
        conn = await asyncio.to_thread(self.ha.get)
        if conn is None:
            return None
        urls = (conn.url, self.settings.ha_auth_url or conn.url)
        if self._ha_login_built is None or self._ha_login_built[0] != urls:
            login = HomeAssistantLogin(
                ha_url=urls[0],
                ha_auth_url=urls[1],
                client_id=self.settings.client_id,
                redirect_uri=self.settings.redirect_uri,
            )
            self._ha_login_built = (urls, login)
            self._allow_form_action(urls[1])
        return self._ha_login_built[1]

    # ---- session -------------------------------------------------------------------

    def user(self, request: Request) -> dict[str, Any] | None:
        if self.settings.auth_mode == "none":
            return {"sub": "dev", "name": "Developer", "roles": [self.settings.admin_role]}
        if self.ingress:
            return self._ingress_user(request)
        data = self.sessions.loads(request.cookies.get(SESSION_COOKIE))
        if data is None or self.settings.admin_role not in data.get("roles", []):
            return None
        return data

    def _ingress_user(self, request: Request) -> dict[str, Any] | None:
        """The HA user that the Supervisor's Ingress proxy names (ADR 070).

        Only requests from the proxy reach this (see :class:`RequestView`), and the
        Supervisor drops these headers from the browser's request before it sets them.
        The add-on panel is for HA administrators only (``panel_admin``); when the
        Supervisor also sends the user's groups, the admin group is checked here too.
        """
        headers = request.headers
        user_id = headers.get("x-remote-user-id", "").strip()
        if not user_id:
            return None
        groups = headers.get("x-remote-user-groups")
        if groups is not None and ADMIN_GROUP not in {g.strip() for g in groups.split(",")}:
            return None
        username = headers.get("x-remote-user-name", "").strip() or user_id
        allowed = {a.casefold() for a in self.settings.ha_allowed_users}
        if allowed and user_id.casefold() not in allowed and username.casefold() not in allowed:
            return None
        return {
            "sub": user_id,
            "name": headers.get("x-remote-user-display-name", "").strip() or username,
            "username": username,
            "roles": [self.settings.admin_role],
            "idp": "ha-ingress",
        }

    def _cookie(self, resp: Response, name: str, value: str, max_age: int) -> None:
        resp.set_cookie(
            name,
            value,
            max_age=max_age,
            path=prefix(),
            secure=self.secure,
            httponly=True,
            samesite="lax",
        )

    async def _audit(
        self,
        tool: str,
        user: dict[str, Any] | None,
        *,
        decision: str = "allowed",
        outcome: str = "ok",
        error: str | None = None,
        arguments: dict[str, Any] | None = None,
    ) -> None:
        record = AuditRecord(
            tool=f"admin:{tool}",
            decision=decision,  # type: ignore[arg-type]
            outcome=outcome,  # type: ignore[arg-type]
            arguments=arguments or {},
            subject=(user or {}).get("sub"),
            username=(user or {}).get("username"),
            roles=list((user or {}).get("roles", [])),
            client_id="admin-gui",
            idp=(user or {}).get("idp"),
            error=error,
        )
        await self.audit.write(record)

    def protected(
        self, name: str, handler: Handler, *, audit: bool = True
    ) -> Callable[[Request], Awaitable[Response]]:
        """Signed-in admins only; ``audit=False`` for handlers that audit themselves."""

        async def endpoint(request: Request) -> Response:
            user = self.user(request)
            if user is None and self.ingress:
                return self._ingress_refused()
            if user is None:
                return RedirectResponse(f"{prefix()}/login", status_code=303)
            if request.method == "POST" and not await self._post_allowed(request, user):
                logger.warning(
                    "Refused POST %s: Sec-Fetch-Site %r, Origin %r",
                    request.url.path,
                    request.headers.get("sec-fetch-site"),
                    request.headers.get("origin"),
                )
                return Response("Cross-site request refused", status_code=403)
            if audit:
                try:
                    await self._audit(name, user, arguments=dict(request.query_params))
                except AuditError:
                    return HTMLResponse(
                        "Audit is unavailable.", status_code=503, headers=self.headers
                    )
            resp = await handler(request, user)
            resp.headers.update(self.headers)
            if self.ingress and isinstance(resp, HTMLResponse):
                self._add_csrf(resp, self._csrf_token(request, user))
            return resp

        return endpoint

    # ---- cross-site requests ---------------------------------------------------------

    def _csrf_token(self, request: Request, user: dict[str, Any]) -> str:
        """Form token of this HA user in this Ingress session (X-Ingress-Path)."""
        msg = f"{user.get('sub')}|{request.headers.get('x-ingress-path', '')}"
        return hmac.new(self._csrf_key, msg.encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def _add_csrf(resp: HTMLResponse, token: str) -> None:
        field = f"<input type=hidden name=csrf value={token}>".encode()
        body = _POST_FORM.sub(lambda m: m.group(0) + field, bytes(resp.body))
        resp.body = body
        resp.headers["content-length"] = str(len(body))

    async def _post_allowed(self, request: Request, user: dict[str, Any]) -> bool:
        if not self.ingress:
            return self._same_origin(request)
        # Behind HA Ingress neither header is reliable: browsers send Sec-Fetch-* only to
        # https (and localhost) origins, and HA is mostly opened over plain http in the
        # LAN, while the Origin need not match the X-Forwarded-Host that reaches the
        # add-on. Forms carry a token of the user and the Ingress session instead.
        if request.headers.get("sec-fetch-site") in {"cross-site", "same-site"}:
            return False
        sent = (await self._form(request)).get("csrf", "")
        return hmac.compare_digest(sent.encode(), self._csrf_token(request, user).encode())

    def _same_origin(self, request: Request) -> bool:
        # Sec-Fetch-Site is set by the browser and cannot be set by a page. It decides
        # where present: behind proxies (Home Assistant Ingress behind a reverse proxy,
        # Nabu Casa, the companion app) the Origin need not match any forwarded host.
        fetch_site = request.headers.get("sec-fetch-site")
        if fetch_site:
            return fetch_site in {"same-origin", "none"}
        origin = request.headers.get("origin")
        if not origin:
            return True
        if self.ingress:
            # Home Assistant's own host, as its Ingress proxy passes it on
            host = request.headers.get("x-forwarded-host", "").split(",")[0].strip()
            return bool(host) and urlparse(origin).netloc == host
        expected = urlparse(str(self.settings.base_url or request.base_url))
        return urlparse(origin).netloc == expected.netloc

    def _ingress_refused(self) -> Response:
        body = (
            "<p class=bad>Home Assistant did not name an administrator for this request. "
            "Open xomtra from the Home Assistant sidebar as an administrator.</p>"
        )
        return HTMLResponse(page("Access refused", body, None), 403, headers=self.headers)

    # ---- auth routes ---------------------------------------------------------------

    async def login(self, request: Request) -> Response:
        if self.ingress:
            return RedirectResponse(f"{prefix()}/", status_code=303)
        ha_login = await self._ha_login()
        if ha_login is None and self.settings.auth_mode == "ha":
            if self.ha.storable:
                return RedirectResponse(f"{prefix()}{setup.PATH}", status_code=303)
            return self._login_failed("Home Assistant is not configured.", 503)
        if ha_login is not None:
            tx = LoginTransaction.new()
            resp = RedirectResponse(ha_login.authorize_url(tx.state), status_code=303)
            self._cookie(resp, TX_COOKIE, self.transactions.dumps({"state": tx.state}, 600), 600)
            return resp
        if self.oidc is None:
            return RedirectResponse(f"{prefix()}/", status_code=303)
        tx = LoginTransaction.new()
        resp = RedirectResponse(self.oidc.authorize_url(tx), status_code=303)
        value = self.transactions.dumps(
            {"state": tx.state, "nonce": tx.nonce, "verifier": tx.verifier}, 600
        )
        self._cookie(resp, TX_COOKIE, value, 600)
        return resp

    async def callback(self, request: Request) -> Response:
        ha_login = await self._ha_login()
        if self.ingress or (self.oidc is None and ha_login is None):
            return RedirectResponse(f"{prefix()}/", status_code=303)
        tx_data = self.transactions.loads(request.cookies.get(TX_COOKIE))
        state, code = request.query_params.get("state"), request.query_params.get("code")
        if tx_data is None or not state or state != tx_data.get("state") or not code:
            return self._login_failed("The sign-in expired or was not started here.")
        if ha_login is not None or self.oidc is None:
            return await self._ha_callback(code)
        tx = LoginTransaction(tx_data["state"], tx_data["nonce"], tx_data["verifier"])
        try:
            claims = await self.oidc.exchange(code, tx)
        except LoginError as err:
            return self._login_failed(str(err))
        roles = [str(r) for r in claims.get("roles") or []]
        user: dict[str, Any] = {
            "sub": claims.get("oid") or claims.get("sub"),
            "name": claims.get("name"),
            "username": claims.get("preferred_username") or claims.get("email"),
            "roles": roles,
            "idp": "entra",
        }
        if self.settings.admin_role not in roles:
            await self._audit(
                "login", user, decision="denied", outcome="denied", error="missing admin role"
            )
            return self._login_failed(
                f"Your account does not have the role {self.settings.admin_role}.", status_code=403
            )
        return await self._start_session(user)

    async def _ha_callback(self, code: str) -> Response:
        ha_login = await self._ha_login()
        if ha_login is None:
            return self._login_failed("Sign-in is not configured.")
        try:
            ha_user = await ha_login.exchange(code)
        except HALoginError as err:
            return self._login_failed(str(err))
        s = self.settings
        roles = roles_for(ha_user, RoleMap.parse(s.ha_role_map), s.ha_allowed_users)
        user: dict[str, Any] = {
            "sub": ha_user.id,
            "name": ha_user.name,
            "username": ha_user.name,
            "roles": roles,
            "idp": "ha",
        }
        if s.admin_role not in roles:
            await self._audit(
                "login", user, decision="denied", outcome="denied", error="missing admin role"
            )
            return self._login_failed(
                "Only Home Assistant administrators may open the xomtra admin GUI.",
                status_code=403,
            )
        return await self._start_session(user)

    async def _start_session(self, user: dict[str, Any]) -> Response:
        await self._audit("login", user)
        hours = self.settings.admin_session_hours
        resp = RedirectResponse(f"{prefix()}/", status_code=303)
        self._cookie(resp, SESSION_COOKIE, self.sessions.dumps(user, hours * 3600), hours * 3600)
        resp.delete_cookie(TX_COOKIE, path=prefix())
        return resp

    def _login_failed(self, message: str, status_code: int = 400) -> Response:
        body = f"<p class=bad>{e(message)}</p><p><a href='{prefix()}/login'>Sign in again</a></p>"
        return HTMLResponse(page("Sign-in failed", body, None), status_code, headers=self.headers)

    async def logout(self, request: Request) -> Response:
        if self.ingress:
            # Nothing to sign out of behind Ingress (Home Assistant holds the session).
            return RedirectResponse(f"{prefix()}/", status_code=303)
        if not self._same_origin(request):
            return Response("Cross-site request refused", status_code=403)
        resp = RedirectResponse(f"{prefix()}/login", status_code=303)
        resp.delete_cookie(SESSION_COOKIE, path=prefix())
        return resp

    async def check(self, request: Request) -> Response:
        """Caddy forward_auth for the Dagster UI: 200 with the user, else to sign-in."""
        user = self.user(request)
        if user is None:
            return RedirectResponse(f"{prefix()}/login", status_code=302)
        return Response(status_code=200, headers={"X-xomtra-User": str(user.get("username"))})

    # ---- pages ---------------------------------------------------------------------

    def _no_data(self, user: dict[str, Any], title: str) -> Response:
        body = "<p class=warn>The lake is not configured for the admin GUI.</p>"
        return HTMLResponse(page(title, body, user.get("username")))

    async def overview(self, request: Request, user: dict[str, Any]) -> Response:
        conn = await asyncio.to_thread(self.ha.get)
        ha = await status.ha_status(conn.url if conn else None, conn.token if conn else None)
        lake: dict[str, Any] = {}
        if self.data is not None:
            lake = await asyncio.to_thread(status.lake_overview, self.data)
        now = datetime.now(UTC)
        last_state = lake.get("last_state")
        fresh_cls = "bad"
        if last_state and now - datetime.fromisoformat(last_state) < timedelta(minutes=15):
            fresh_cls = "ok"
        if not ha.get("configured"):
            ha_card = ("Home Assistant", "not configured", "warn")
        elif ha.get("ok"):
            ha_card = ("Home Assistant", ha_host(conn.url if conn else None), "ok")
        else:
            ha_card = ("Home Assistant", "unreachable", "bad")
        disk = self._disk()
        body = cards(
            [
                ha_card,
                *([disk[0]] if disk else []),
                ("Data lake size", lake_size(lake.get("lake_bytes")), ""),
                ("Entities in HA", ha.get("entities"), ""),
                ("Last data state change", last_state, fresh_cls),
                ("Last hourly statistic", lake.get("last_statistic"), ""),
            ]
        )
        if ha.get("error"):
            body += connection_error(ha)
        if self.ha.storable:
            body += (
                f"<p class=muted><a href='{prefix()}{setup.PATH}'>Change the Home Assistant "
                "URL or token</a></p>"
            )
        if disk and disk[1]:
            body += disk[1]
        kinds = lake.get("kinds") or []
        body += "<h2>Catalog</h2>"
        if kinds:
            body += f"<p class=muted>Profiled at {fmt(kinds[0]['profiled_at'])}</p>" + table(
                ["entity kind", "entities", "stale entities"],
                [(k["kind"], k["entities"], k["stale"]) for k in kinds],
                {1, 2},
            )
        else:
            body += "<p class=muted>No profile yet; the catalog is profiled once a day.</p>"
        drift = lake.get("drift_7d") or []
        body += "<h2>Entity changes, last 7 days</h2>" + (
            table(["type", "count"], [(d["drift_type"], d["n"]) for d in drift], {1})
            if drift
            else "<p class=muted>None.</p>"
        )
        return HTMLResponse(page("Overview", body, user.get("username")))

    def _disk(self) -> tuple[tuple[str, Any, str], str] | None:
        """Card and note on the free space of the lake's disk (ADR 072)."""
        worker = self.settings.lake_data_path  # the worker pauses / prunes on this disk
        path = worker or self.settings.disk_path
        if not path:
            return None
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            return ("Free disk", "unknown", "warn"), ""
        ratio = usage.free / usage.total if usage.total else 1.0
        value = f"{usage.free / 2**30:,.1f} GiB · {ratio:.0%}"
        stop, low = self.settings.worker_stop_free_ratio, self.settings.worker_min_free_ratio
        if not worker:
            cls = "bad" if ratio < stop else "warn" if ratio < low else "ok"
            note = (
                "<p class=warn>Little free space on the disk with the data. Free some space "
                "or shorten the retention, before Postgres and SeaweedFS stop writing.</p>"
                if cls != "ok"
                else ""
            )
            return ("Free disk", value, cls), note
        if ratio < stop:
            note = (
                f"<p class=bad>Less than {stop:.0%} free on the disk: collecting is paused "
                "and only the retention runs. Free some space or shorten the retention; "
                "xomtra then resumes and catches up from Home Assistant's history.</p>"
            )
            return ("Free disk", value, "bad"), note
        if ratio < low:
            note = (
                f"<p class=warn>Less than {low:.0%} free on the disk: the retention runs "
                f"early. Below {stop:.0%} collecting pauses.</p>"
            )
            return ("Free disk", value, "warn"), note
        return ("Free disk", value, "ok"), ""

    async def catalog(self, request: Request, user: dict[str, Any]) -> Response:
        data = self.data
        if data is None:
            return self._no_data(user, "Catalog")
        q = request.query_params.get("q", "")[:200]
        kind = request.query_params.get("kind", "")
        kind = kind if kind in KINDS else ""
        stale = request.query_params.get("stale") == "1"
        rows = await asyncio.to_thread(status.catalog_rows, data, q, kind, stale)
        options = "".join(
            f"<option value='{k}'{' selected' if k == kind else ''}>{k or 'all kinds'}</option>"
            for k in ("", *KINDS)
        )
        form = (
            "<form class=filters method=get>"
            f"<input name=q value='{e(q)}' placeholder='search'>"
            f"<select name=kind>{options}</select>"
            f"<label><input type=checkbox name=stale value=1{' checked' if stale else ''}> "
            "stale only</label><button>Filter</button></form>"
        )
        cols = list(rows[0]) if rows else ["entity_id"]
        numeric = {
            i
            for i, c in enumerate(cols)
            if c.startswith(("value_", "changes", "numeric", "missing"))
        }
        body = form + f"<p class=muted>{len(rows)} entities (max 500)</p>"
        if not rows and not await asyncio.to_thread(status.catalog_profiled, data):
            # A new installation: the daily profile fills the catalog, not the ingest,
            # so explain the empty page instead of showing an empty table.
            body += (
                "<p class=muted>No profile yet. The catalog is profiled once a day at "
                "04:15; a new installation shows its entities after the first night.</p>"
                f"<p class=muted>Time zone: <bdi>{e(self.settings.timezone)}</bdi></p>"
            )
            return HTMLResponse(page("Catalog", body, user.get("username")))
        body += table(cols, [list(r.values()) for r in rows], numeric)
        return HTMLResponse(page("Catalog", body, user.get("username")))

    async def drift(self, request: Request, user: dict[str, Any]) -> Response:
        data = self.data
        if data is None:
            return self._no_data(user, "Entity changes")
        rows = await asyncio.to_thread(status.drift_rows, data)
        cols = ["detected_at", "entity_id", "drift_type", "field", "old_value", "new_value"]
        body = "<p class=muted>Last 30 days</p>" + table(cols, [[r[c] for c in cols] for r in rows])
        return HTMLResponse(page("Entity changes", body, user.get("username")))

    def _configured_retention(self) -> dict[str, int]:
        """Retention days of the installation (environment, the add-on's options)."""
        return {spec.key: int(getattr(self.settings, spec.key)) for spec in RETENTION_SPECS}

    async def _stored_retention(self) -> tuple[dict[str, Change], list[Change]] | None:
        """Retention periods stored in the admin GUI and their history (ADR 094);
        None without a settings store."""
        if self.analysis is None:
            return None
        current = await asyncio.to_thread(self.analysis.current)
        history = await asyncio.to_thread(self.analysis.history, 500)
        return (
            {k: c for k, c in current.items() if k in RETENTION_BY_KEY},
            [c for c in history if c.key in RETENTION_BY_KEY][:50],
        )

    def _retention_row(
        self, spec: Spec, configured: int, stored: Change | None, effective: int, now: datetime
    ) -> list[Any]:
        buttons = "<button name=action value=save>Save</button>"
        if stored is not None:
            buttons += " <button name=action value=reset>Reset</button>"
        form = (
            f"<form class=filters method=post action='{prefix()}/retention'>"
            f"<input type=hidden name=key value='{e(spec.key)}'>"
            f"<input name=value type=number inputmode=numeric min={spec.lo:g} max={spec.hi:g} "
            f"step=1 required value='{effective}' aria-label='days'>"
            "<input name=reason size=18 maxlength=200 placeholder='reason (optional)'>"
            f"{buttons}</form>"
        )
        who = f"{e(stored.changed_by)}, {fmt(stored.changed_at)}" if stored else ""
        return [
            spec.label,
            configured,
            stored.value if stored else None,
            effective,
            (now - timedelta(days=effective)).date(),
            who,
            form,
        ]

    async def retention(
        self, request: Request, user: dict[str, Any], *, note: str = "", status_code: int = 200
    ) -> Response:
        title = "Retention"
        now = datetime.now(UTC)
        configured = self._configured_retention()
        try:
            stored_all = await self._stored_retention()
        except SettingsStoreError:
            stored_all = None
            note += "<p class=bad>The settings store is unavailable.</p>"
            status_code = max(status_code, 503)
        stored, history = stored_all or ({}, [])
        body = note + (
            "<p><strong>These are the retention periods of the xomtra Data Lake, not of "
            "Home Assistant.</strong></p>"
            "<p class=muted>Applied daily at 03:30; the data files disappear 7 days later. "
            "A value saved here wins over the installation's configuration and is used by "
            "the next run, without a restart. Reset hands the period back to the "
            "installation's configuration.</p>"
        )
        if self.analysis is None:
            body += (
                "<p class=warn>The settings store is not configured for the admin GUI; the "
                "periods cannot be changed here.</p>"
            )
            rows = [
                [
                    spec.label,
                    configured[spec.key],
                    configured[spec.key],
                    (now - timedelta(days=configured[spec.key])).date(),
                ]
                for spec in RETENTION_SPECS
            ]
            body += table(["data", "configured", "days", "deleted before"], rows, {1, 2})
        else:
            rows = []
            for spec in RETENTION_SPECS:
                change = stored.get(spec.key)
                effective = int(change.value) if change else configured[spec.key]  # type: ignore[arg-type]
                rows.append(self._retention_row(spec, configured[spec.key], change, effective, now))
            body += table(
                ["data", "configured", "stored", "days", "deleted before", "changed", ""],
                rows,
                {1, 2, 3},
                trusted_html={5, 6},
            )
            body += "<p class=muted>Allowed: 1 to 3650 days.</p>"
        window = e(self.settings.catalog_window_days)
        body += f"<p class=muted>Catalog profile window: {window} days.</p>"
        if history:
            body += "<h2>History</h2>" + table(
                ["when", "data", "value", "by", "reason"],
                [
                    [
                        c.changed_at,
                        RETENTION_BY_KEY[c.key].label,
                        "back to configuration" if c.value is None else c.value,
                        c.changed_by,
                        c.reason,
                    ]
                    for c in history
                ],
            )
        if self.ingress or self.data is not None:
            body += "<h2>Data Lake</h2>"
        if self.ingress:
            body += BACKUP_NOTE
        if self.data is not None:
            sizes = await asyncio.to_thread(status.table_sizes, self.data)
            body += "<h2>Tables in the lake</h2>" + table(
                ["schema", "table", "rows (est.)", "files", "MiB", "delete files"],
                [list(r.values()) for r in sizes],
                {2, 3, 4, 5},
            )
        return HTMLResponse(page(title, body, user.get("username")), status_code)

    async def retention_post(self, request: Request, user: dict[str, Any]) -> Response:
        """Store a retention period (ADR 094); shortening needs a confirmation."""
        form = await self._form(request)
        spec = RETENTION_BY_KEY.get(form.get("key", ""))
        action = form.get("action", "save")
        reason = form.get("reason", "")[:200] or None
        if spec is None or action not in ("save", "reset") or self.analysis is None:
            return await self.retention(
                request, user, note="<p class=bad>Unknown setting.</p>", status_code=400
            )
        configured = self._configured_retention()[spec.key]
        value: int | None = None
        try:
            current = await asyncio.to_thread(self.analysis.current)
            change = current.get(spec.key)
            effective = int(change.value) if change else configured  # type: ignore[arg-type]
            if action == "save":
                try:
                    value = int(spec.parse(form.get("value", "")))
                except ValueError:
                    return await self.retention(
                        request,
                        user,
                        note=(
                            f"<p class=bad><b>{e(spec.label)}</b> "
                            "Enter a whole number of days from 1 to 3650.</p>"
                        ),
                        status_code=400,
                    )
            new = configured if value is None else value
            if new < effective and form.get("confirm") != "1":
                cutoff = (datetime.now(UTC) - timedelta(days=new)).date()
                keep = (
                    f"<input type=hidden name=key value='{e(spec.key)}'>"
                    f"<input type=hidden name=action value='{e(action)}'>"
                    f"<input type=hidden name=value value='{e(new)}'>"
                    f"<input type=hidden name=reason value='{e(reason or '')}'>"
                )
                note = (
                    f"<h2>{e(spec.label)}</h2>"
                    + cards(
                        [
                            ("current days", effective, ""),
                            ("new days", new, "warn"),
                            ("deleted before", cutoff, "bad"),
                        ]
                    )
                    + "<p class=warn>Shortening the period deletes the older data at the next "
                    "run. It cannot be undone; xomtra can only load again what Home Assistant "
                    "still keeps in its own history.</p>"
                    f"<form class=filters method=post action='{prefix()}/retention'>{keep}"
                    "<label><input type=checkbox name=confirm value=1 required> "
                    "delete the older data</label>"
                    "<button class=danger>Shorten</button></form>"
                )
                return await self.retention(request, user, note=note, status_code=409)
            await asyncio.to_thread(
                self.analysis.set, spec.key, value, by=user.get("username"), reason=reason
            )
            await self._audit(
                "retention_setting",
                user,
                arguments={
                    "key": spec.key,
                    "value": value,
                    "previous": effective,
                    "reason": reason,
                },
            )
        except (SettingsStoreError, AuditError):
            return await self.retention(
                request,
                user,
                note="<p class=bad>Not saved: the settings store or audit is unavailable.</p>",
                status_code=503,
            )
        return RedirectResponse(f"{prefix()}/retention", status_code=303)

    async def audit_page(self, request: Request, user: dict[str, Any]) -> Response:
        rows = await self.recent_audit()
        cols = [
            "ts",
            "username",
            "idp",
            "tool",
            "decision",
            "outcome",
            "row_count",
            "duration_ms",
            "arguments",
            "error",
        ]
        body = "<p class=muted>Last 200 calls (MCP tools and this admin)</p>"
        if rows is None:
            body += "<p class=warn>The audit database is not configured for the admin GUI.</p>"
        else:
            body += table(cols, [[r.get(c) for c in cols] for r in rows], {6, 7})
        return HTMLResponse(page("Audit", body, user.get("username")))

    async def recent_audit(self) -> list[dict[str, Any]] | None:
        dsn = self.settings.audit_dsn
        if not dsn:
            return None

        def query() -> list[dict[str, Any]]:
            import psycopg2  # noqa: PLC0415

            with psycopg2.connect(dsn, connect_timeout=5) as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT ts, username, idp, tool, decision, outcome, row_count, duration_ms, "
                    "arguments, error FROM audit.tool_call ORDER BY id DESC LIMIT 200"
                )
                cols = [d[0] for d in cur.description or []]
                out = []
                for r in cur.fetchall():
                    row = dict(zip(cols, r, strict=True))
                    row["arguments"] = json.dumps(row["arguments"], ensure_ascii=False)[:300]
                    out.append(row)
                return out

        return await asyncio.to_thread(query)

    # ---- analysis settings (ADR 060) -----------------------------------------------

    @staticmethod
    def _shown(spec: Spec, value: Value | None) -> str:
        if value is None:
            return ""
        if spec.kind == "bool":
            return "on" if value else "off"
        return f"{value:g}" if isinstance(value, float) else str(value)

    def _setting_row(self, spec: Spec, stored: Change | None) -> list[Any]:
        value = self._shown(spec, stored.value if stored else None)
        if spec.kind in ("bool", "choice"):
            options = ("on", "off") if spec.kind == "bool" else spec.choices
            current = value or self._shown(spec, spec.default)
            field = (
                "<select name=value>"
                + "".join(
                    f"<option{' selected' if o == current else ''}>{e(o)}</option>" for o in options
                )
                + "</select>"
            )
        else:
            field = (
                f"<input name=value size=8 value='{e(value)}' "
                f"placeholder='{e(self._shown(spec, spec.default))}'>"
            )
        buttons = "<button name=action value=save>Save</button>"
        if spec.key in PREVIEW_KEYS:
            buttons += " <button name=action value=preview>Preview</button>"
        if stored is not None:
            buttons += " <button name=action value=reset>Reset</button>"
        form = (
            f"<form class=filters method=post action='{prefix()}/analysis'>"
            f"<input type=hidden name=key value='{e(spec.key)}'>{field}"
            "<input name=reason size=18 maxlength=200 placeholder='reason (optional)'>"
            f"{buttons}</form>"
        )
        label = f"{e(spec.label)}<br><code>{e(spec.env)}</code>"
        if spec.help:
            label += f"<br><span class=muted>{e(spec.help)}</span>"
        who = f"{e(stored.changed_by)}, {fmt(stored.changed_at)}" if stored else ""
        return [label, self._shown(spec, spec.default), spec.bounds(), value or None, who, form]

    async def analysis_page(
        self, request: Request, user: dict[str, Any], *, note: str = "", status_code: int = 200
    ) -> Response:
        title = "Analysis parameters"
        if self.analysis is None:
            body = "<p class=warn>The audit database is not configured for the admin GUI.</p>"
            return HTMLResponse(page(title, body, user.get("username")))
        try:
            current = await asyncio.to_thread(self.analysis.current)
            history = await asyncio.to_thread(self.analysis.history, 500)
        except SettingsStoreError:
            body = "<p class=bad>The settings store is unavailable.</p>"
            return HTMLResponse(page(title, body, user.get("username")), 503)
        history = [c for c in history if c.key not in RETENTION_BY_KEY][:50]
        body = note + (
            "<p class=warn>If the findings work for you, leave these settings as they are."
            "</p>"
            "<p class=muted>A stored value wins over the ingest's environment variable, which "
            "wins over the default. The next analysis run (daily) uses it, without a "
            "restart. The thresholds set the share of false findings, not their number: a "
            "larger home rightly finds more.</p>"
        )
        if any(k in BY_KEY for k in current):
            body += (
                f"<form class=filters method=post action='{prefix()}/analysis'>"
                "<input type=hidden name=action value=reset_all>"
                "<label><input type=checkbox name=confirm value=1 required> "
                "set every changed setting back to its default</label>"
                "<input name=reason size=18 maxlength=200 placeholder='reason (optional)'>"
                "<button class=danger>Reset all to defaults</button></form>"
            )
        for group in dict.fromkeys(s.group for s in SPECS):
            rows = [self._setting_row(s, current.get(s.key)) for s in SPECS if s.group == group]
            body += f"<h2>{e(group)}</h2>" + table(
                ["setting", "default", "allowed", "stored", "changed", ""],
                rows,
                trusted_html={0, 5},
            )
        body += "<h2>History</h2>" + table(
            ["when", "setting", "value", "by", "reason"],
            [
                [
                    c.changed_at,
                    c.key,
                    "back to default"
                    if c.value is None
                    else self._shown(BY_KEY[c.key], c.value)
                    if c.key in BY_KEY
                    else c.value,
                    c.changed_by,
                    c.reason,
                ]
                for c in history
            ],
        )
        return HTMLResponse(page(title, body, user.get("username")), status_code)

    async def analysis_post(self, request: Request, user: dict[str, Any]) -> Response:
        form = await self._form(request)
        spec = BY_KEY.get(form.get("key", ""))
        action = form.get("action", "save")
        reason = form.get("reason", "")[:200] or None
        if action == "reset_all" and self.analysis is not None:
            return await self._reset_all(request, user, form.get("confirm") == "1", reason)
        if spec is None or action not in ("save", "reset", "preview") or self.analysis is None:
            return await self.analysis_page(
                request, user, note="<p class=bad>Unknown setting.</p>", status_code=400
            )
        value: Value | None = None
        if action != "reset":
            try:
                value = spec.parse(form.get("value", ""))
            except ValueError as err:
                return await self.analysis_page(
                    request, user, note=f"<p class=bad>{e(err)}</p>", status_code=400
                )
        if action == "preview":
            assert value is not None  # noqa: S101 - parsed above
            result = (
                await asyncio.to_thread(preview, self.data, spec.key, value)
                if self.data is not None
                else None
            )
            if result is None:
                note = "<p class=warn>No findings to preview yet.</p>"
            else:
                note = (
                    f"<p class=ok>With {e(spec.key)} = {e(self._shown(spec, value))}: "
                    f"{result['kept']} of {result['now']} current findings remain "
                    f"({result['kept_unflagged']} of {result['unflagged']} without flags). "
                    "Exact when tightening; when loosening, weaker candidates that were not "
                    "stored may also appear. Nothing was saved.</p>"
                )
            return await self.analysis_page(request, user, note=note)
        try:
            await asyncio.to_thread(
                self.analysis.set, spec.key, value, by=user.get("username"), reason=reason
            )
            await self._audit(
                "analysis_setting",
                user,
                arguments={"key": spec.key, "value": value, "reason": reason},
            )
        except (SettingsStoreError, AuditError):
            return await self.analysis_page(
                request,
                user,
                note="<p class=bad>Not saved: the settings store or audit is unavailable.</p>",
                status_code=503,
            )
        return RedirectResponse(f"{prefix()}/analysis", status_code=303)

    async def _reset_all(
        self, request: Request, user: dict[str, Any], confirmed: bool, reason: str | None
    ) -> Response:
        """Every stored analysis setting back to its default (one history row each)."""
        assert self.analysis is not None  # noqa: S101 - checked by the caller
        if not confirmed:
            return await self.analysis_page(
                request, user, note="<p class=bad>Tick the box to confirm.</p>", status_code=400
            )
        try:
            stored = await asyncio.to_thread(self.analysis.current)
            keys = sorted(k for k, c in stored.items() if k in BY_KEY and c.value is not None)
            for key in keys:
                await asyncio.to_thread(
                    self.analysis.set, key, None, by=user.get("username"), reason=reason
                )
            await self._audit(
                "analysis_setting_reset_all", user, arguments={"keys": keys, "reason": reason}
            )
        except (SettingsStoreError, AuditError):
            return await self.analysis_page(
                request,
                user,
                note="<p class=bad>Not reset: the settings store or audit is unavailable.</p>",
                status_code=503,
            )
        return RedirectResponse(f"{prefix()}/analysis", status_code=303)

    # ---- dismissed findings (ADR 060 point 6) ---------------------------------------

    async def findings_page(
        self, request: Request, user: dict[str, Any], *, note: str = "", status_code: int = 200
    ) -> Response:
        title = "Findings"
        if self.analysis is None:
            body = "<p class=warn>The audit database is not configured for the admin GUI.</p>"
            return HTMLResponse(page(title, body, user.get("username")))
        ftype = request.query_params.get("type", "")
        ftype = ftype if ftype in FINDING_TYPES else ""
        flagged = request.query_params.get("flagged") == "1"
        try:
            dismissed = await asyncio.to_thread(self.analysis.dismissals, 200)
        except SettingsStoreError:
            body = "<p class=bad>The settings store is unavailable.</p>"
            return HTMLResponse(page(title, body, user.get("username")), 503)
        rows = (
            await asyncio.to_thread(current_findings, self.data, ftype, flagged)
            if self.data is not None
            else []
        )
        options = "".join(
            f"<option value='{t}'{' selected' if t == ftype else ''}>{t or 'all types'}</option>"
            for t in ("", *FINDING_TYPES)
        )
        body = note + (
            "<p class=muted>A dismissed finding is hidden from the next analysis run on, in "
            "every window, until you undo it or until the chosen date: for a correlation the "
            "pair of entities, for an anomaly or a change point the entity. Claude sees it "
            "only when it asks for flagged findings too.</p>"
            "<form class=filters method=get>"
            f"<select name=type>{options}</select>"
            f"<label><input type=checkbox name=flagged value=1{' checked' if flagged else ''}>"
            " including flagged</label><button>Filter</button></form>"
        )
        until = "".join(
            f"<option value={d}>{f'{d} days' if d else 'for good'}</option>" for d in DISMISS_DAYS
        )

        def form(r: dict[str, Any]) -> str:
            if "dismissed" in (r.get("flags") or []):
                return "dismissed"
            related = r.get("related_entity_id") or ""
            return (
                f"<form class=filters method=post action='{prefix()}/findings/dismiss'>"
                f"<input type=hidden name=type value='{e(r['finding_type'])}'>"
                f"<input type=hidden name=entity value='{e(r['entity_id'])}'>"
                f"<input type=hidden name=related value='{e(related)}'>"
                "<input name=reason size=14 maxlength=200 placeholder='reason'>"
                f"<select name=days>{until}</select><button>Dismiss</button></form>"
            )

        body += self._confidence_html(
            await asyncio.to_thread(run_confidence, self.data) if self.data is not None else []
        )
        body += f"<h2>Current findings</h2><p class=muted>{len(rows)} (max 300)</p>" + table(
            [
                "type",
                "entity",
                "related",
                "window",
                "score",
                "q",
                "usefulness",
                "action",
                "flags",
                "period start",
                "",
            ],
            [
                [
                    r["finding_type"],
                    r["entity_id"],
                    r["related_entity_id"],
                    r["window_days"],
                    r["score"],
                    r["q_value"],
                    r.get("usefulness"),
                    r.get("action_category"),
                    r["flags"],
                    r["period_start"],
                    form(r),
                ]
                for r in rows
            ],
            {3, 4, 5, 6},
            trusted_html={10},
        )
        body += "<h2>Dismissed</h2>" + table(
            ["type", "entity", "related", "reason", "until", "by", "when", ""],
            [
                [
                    d.finding_type,
                    d.entity_id,
                    d.related_entity_id,
                    d.reason,
                    d.until,
                    d.dismissed_by,
                    d.dismissed_at,
                    (
                        f"<form method=post action='{prefix()}/findings/undo'>"
                        f"<input type=hidden name=id value='{d.id}'><button>Undo</button></form>"
                    )
                    if d.active()
                    else ("undone by " + e(d.revoked_by) if d.revoked_at else "expired"),
                ]
                for d in dismissed
            ],
            trusted_html={7},
        )
        return HTMLResponse(page(title, body, user.get("username")), status_code)

    @staticmethod
    def _confidence_html(runs: list[dict[str, Any]]) -> str:
        """Low-power indicator and area coverage of the last correlation runs (ADR 067)."""
        if not runs:
            return ""
        out = "<h2>Confidence of the correlations</h2>" + table(
            ["window", "run", "confidence", "reasons", "series", "entities", "with area"],
            [
                [
                    r["window_days"],
                    r["run_at"],
                    r["confidence"],
                    r["reasons"],
                    r["series"],
                    r["entities"],
                    r["group_coverage"],
                ]
                for r in runs
            ],
            {0, 4, 5, 6},
        )
        ungrouped = sorted({e for r in runs for e in (r.get("ungrouped") or [])})
        if any(r["confidence"] == "low" for r in runs):
            out += (
                "<p class=warn>Low power: with few series or few entities in an area the "
                "false correlations are strong ones with a shared cause the analysis cannot "
                "see, so a stricter threshold does not help. Assign the entities "
                "below to areas in Home Assistant; that measurably lowers the false "
                "findings.</p>"
            )
        if ungrouped:
            out += (
                f"<details><summary>{len(ungrouped)} analysed entities without an area"
                "</summary><p>"
                + ", ".join(f"<code>{e(x)}</code>" for x in ungrouped[:500])
                + "</p></details>"
            )
        return out

    async def finding_dismiss(self, request: Request, user: dict[str, Any]) -> Response:
        form = await self._form(request)
        ftype = form.get("type", "")
        entity = form.get("entity", "")[:255]
        related = form.get("related", "")[:255] or None
        reason = form.get("reason", "")[:200] or None
        try:
            days = int(form.get("days", "0"))
        except ValueError:
            days = -1
        valid = ftype in FINDING_TYPES and entity and days in DISMISS_DAYS
        if ftype == "correlation" and not related:
            valid = False
        if not valid or self.analysis is None:
            return await self.findings_page(
                request, user, note="<p class=bad>Invalid finding.</p>", status_code=400
            )
        if ftype != "correlation":
            related = None
        until = datetime.now(UTC) + timedelta(days=days) if days else None
        try:
            new_id = await asyncio.to_thread(
                self.analysis.dismiss,
                ftype,
                entity,
                related,
                by=user.get("username"),
                reason=reason,
                until=until,
            )
            await self._audit(
                "finding_dismiss",
                user,
                arguments={
                    "id": new_id,
                    "type": ftype,
                    "entity_id": entity,
                    "related_entity_id": related,
                    "days": days,
                    "reason": reason,
                },
            )
        except (SettingsStoreError, AuditError):
            return await self.findings_page(
                request,
                user,
                note="<p class=bad>Not saved: the settings store or audit is unavailable.</p>",
                status_code=503,
            )
        return RedirectResponse(f"{prefix()}/findings", status_code=303)

    async def finding_undo(self, request: Request, user: dict[str, Any]) -> Response:
        try:
            dismissal_id = int((await self._form(request)).get("id", ""))
        except ValueError:
            dismissal_id = -1
        if self.analysis is None or dismissal_id < 0:
            return await self.findings_page(
                request, user, note="<p class=bad>Invalid request.</p>", status_code=400
            )
        try:
            done = await asyncio.to_thread(self.analysis.undo, dismissal_id, user.get("username"))
            await self._audit(
                "finding_undismiss",
                user,
                arguments={"id": dismissal_id},
                outcome="ok" if done else "error",
                error=None if done else "not found or already undone",
            )
        except (SettingsStoreError, AuditError):
            return await self.findings_page(
                request,
                user,
                note="<p class=bad>The settings store or audit is unavailable.</p>",
                status_code=503,
            )
        return RedirectResponse(f"{prefix()}/findings", status_code=303)

    # ---- personal tokens and client setup (local access, ADR 056/059) ---------------

    def _entra_note(self) -> str:
        """Who may use xomtra with Entra ID (there is no page Access then)."""
        if self.settings.auth_mode != "entra":
            return ""
        return (
            "<p class=muted>With Entra ID, who may use xomtra and with which role is set in "
            "Entra (app roles of the enterprise application). Sign-ins end in Entra too.</p>"
        )

    def _local_only(self, user: dict[str, Any], title: str) -> Response | None:
        if self.settings.access_mode == "local" and self.tokens is not None:
            return None
        body = (
            "<p class=warn>Personal tokens are used only in the local access mode "
            "(<code>XOMTRA_ACCESS_MODE=local</code>, see docs/setup/local-access.md). "
            "In the public mode, Claude connects with OAuth (docs/setup/claude-connector.md)."
            "</p>" + self._entra_note()
        )
        return HTMLResponse(page(title, body, user.get("username")))

    async def tokens_page(self, request: Request, user: dict[str, Any]) -> Response:
        refused = self._local_only(user, "MCP tokens")
        if refused is not None or self.tokens is None:
            return refused or Response(status_code=404)
        try:
            items = await asyncio.to_thread(self.tokens.list)
        except TokenStoreError:
            return HTMLResponse(
                page("MCP tokens", "<p class=bad>The token store is unavailable.</p>", None), 503
            )
        roles = "".join(f"<option>{e(r)}</option>" for r in TOKEN_ROLES)
        days = "".join(
            f"<option value={d}{' selected' if d == 90 else ''}>{d} days</option>"
            for d in TOKEN_DAYS
        )
        form = (
            f"<form id=issue class=filters method=post action='{prefix()}/tokens/issue'>"
            f"<input name=owner required maxlength=100 value='{e(user.get('username') or '')}'"
            " placeholder='for whom'>"
            "<input name=description required maxlength=200 placeholder='device or app, "
            "e.g. Jana laptop, Claude Desktop'>"
            f"<select name=role>{roles}</select>"
            f"<label>expires <select name=days>{days}</select></label>"
            "<button>Issue token</button></form>"
        )
        rows = []
        for t in items:
            action = (
                f"<form method=post action='{prefix()}/tokens/revoke'>"
                f"<input type=hidden name=id value='{e(t.id)}'><button>Revoke</button></form>"
                if t.active()
                else ("revoked" if t.revoked_at else "expired")
            )
            rows.append(
                [
                    t.id,
                    t.owner,
                    t.description,
                    t.role,
                    t.created_at,
                    t.expires_at,
                    t.last_used_at,
                    action,
                ]
            )
        body = (
            "<p class=muted>Each person or device gets its own token. A token is shown once; "
            "only its hash is stored. Revoked tokens stop working within 30 seconds.</p>"
            + self._entra_note()
            + "<p class=muted>Only assistants on a computer in your local network can "
            "connect, such as Claude Desktop and Claude Code; claude.ai on the web and ChatGPT "
            f"cannot. <a href='{prefix()}/connect'>How to connect Claude and other MCP "
            "clients</a></p>"
            + token_flow()
            + form
            + table(
                ["id", "for", "description", "role", "created", "expires", "last used", ""],
                rows,
                trusted_html={7},
            )
        )
        return HTMLResponse(page("MCP tokens", body, user.get("username")))

    async def _form(self, request: Request, limit: int = 10_000) -> dict[str, str]:
        raw = (await request.body())[:limit].decode(errors="replace")
        return {k: v[0].strip() for k, v in parse_qs(raw).items() if v}

    async def token_issue(self, request: Request, user: dict[str, Any]) -> Response:
        refused = self._local_only(user, "MCP tokens")
        if refused is not None or self.tokens is None:
            return refused or Response(status_code=404)
        form = await self._form(request)
        owner, description = form.get("owner", "")[:100], form.get("description", "")[:200]
        role = form.get("role", "")
        try:
            days = int(form.get("days", "365"))
        except ValueError:
            days = -1
        if not owner or not description or role not in TOKEN_ROLES or days not in TOKEN_DAYS:
            return HTMLResponse(
                page("MCP tokens", "<p class=bad>Fill in all fields.</p>", user.get("username")),
                400,
            )
        try:
            info, token = await asyncio.to_thread(
                self.tokens.issue,
                owner=owner,
                description=description,
                role=role,
                created_by=user.get("username"),
                expires_at=expiry(days),
            )
            await self._audit(
                "token_issue",
                user,
                arguments={
                    "id": info.id,
                    "owner": owner,
                    "description": description,
                    "role": role,
                    "days": days,
                },
            )
        except (TokenStoreError, AuditError):
            return HTMLResponse(
                page(
                    "MCP tokens",
                    "<p class=bad>Not issued: the token store or audit is unavailable.</p>",
                    user.get("username"),
                ),
                503,
            )
        body = (
            f"<p>Token <code>{e(info.id)}</code> for {e(owner)} ({e(description)}, {e(role)}). "
            "<strong>Copy it now, it is not shown again.</strong></p>"
            f"<p><input class=token readonly size=80 value='{e(token)}'></p>"
            + self._connect_help(token)
            + "<p class=muted>claude.ai on the web and on phones connects through Anthropic's "
            "servers and needs the public access mode (docs/setup/claude-connector.md).</p>"
            + f"<p><a href='{prefix()}/tokens'>Back to tokens</a></p>"
        )
        return HTMLResponse(page("New token", body, user.get("username")))

    async def token_revoke(self, request: Request, user: dict[str, Any]) -> Response:
        refused = self._local_only(user, "MCP tokens")
        if refused is not None or self.tokens is None:
            return refused or Response(status_code=404)
        token_id = (await self._form(request)).get("id", "")[:40]
        try:
            done = await asyncio.to_thread(self.tokens.revoke, token_id, user.get("username"))
            await self._audit(
                "token_revoke",
                user,
                arguments={"id": token_id},
                outcome="ok" if done else "error",
                error=None if done else "not found or already revoked",
            )
        except (TokenStoreError, AuditError):
            return HTMLResponse(
                page(
                    "MCP tokens",
                    "<p class=bad>The token store or audit is unavailable.</p>",
                    user.get("username"),
                ),
                503,
            )
        return RedirectResponse(f"{prefix()}/tokens", status_code=303)

    # ---- access of the Claude connector (ADR 081) ------------------------------------

    def has_access_page(self) -> bool:
        """The page Access exists for the Claude connector with Home Assistant sign-in
        (public mode, ADR 081); otherwise its few words are on the page Tokens."""
        return (
            self.settings.auth_mode != "entra"
            and self.settings.access_mode == "public"
            and not self.ingress
        )

    def _access_refused(self, user: dict[str, Any]) -> Response | None:
        if not self.has_access_page():
            return RedirectResponse(f"{prefix()}/tokens", status_code=303)
        if self.access is None:
            return HTMLResponse(
                page("Access", "<p class=bad>The access store is unavailable.</p>", None), 503
            )
        return None

    async def access_page(self, request: Request, user: dict[str, Any]) -> Response:
        refused = self._access_refused(user)
        if refused is not None or self.access is None:
            return refused or Response(status_code=404)
        try:
            users = await asyncio.to_thread(self.access.ha_users)
            sessions = await asyncio.to_thread(self.access.sessions)
        except AccessStoreError:
            return HTMLResponse(
                page("Access", "<p class=bad>The access store is unavailable.</p>", None), 503
            )
        user_rows = []
        for u in users:
            if u.is_admin:
                control = "xomtra.Admin (Home Assistant administrator)"
            else:
                options = "".join(
                    f"<option value='{v}'{' selected' if (u.role or '') == v else ''}>"
                    f"{label}</option>"
                    for v, label in (("", "no access"), ("xomtra.Reader", "xomtra.Reader"))
                )
                control = (
                    f"<form class=filters method=post action='{prefix()}/access/role'>"
                    f"<input type=hidden name=id value='{e(u.ha_user_id)}'>"
                    f"<select name=role>{options}</select><button>Save</button></form>"
                )
            user_rows.append([u.name, u.last_seen_at, control])
        now = datetime.now(UTC)
        session_rows = []
        for sess in sessions:
            if sess.active(now):
                action = (
                    f"<form method=post action='{prefix()}/access/disconnect'>"
                    f"<input type=hidden name=id value='{e(sess.id)}'>"
                    "<button>Disconnect</button></form>"
                )
            else:
                action = "disconnected" if sess.revoked_at else "ended"
            session_rows.append(
                [
                    sess.name or sess.subject,
                    sess.created_at,
                    sess.expires_at,
                    sess.last_used_at,
                    action,
                ]
            )
        mfa = (
            "Two-factor authentication in Home Assistant is required for the Claude connector."
            if self.settings.ha_require_mfa
            else "Two-factor authentication in Home Assistant is not required "
            "(XOMTRA_HA_REQUIRE_MFA=false)."
        )
        body = (
            "<h2>Home Assistant users</h2>"
            "<p class=muted>Home Assistant administrators always have the role xomtra.Admin. "
            "Other users get no access until you give them the role xomtra.Reader here. "
            "A user appears in this list after their first attempt to connect Claude.</p>"
            f"<p class=muted>{e(mfa)}</p>"
            + table(["user", "last seen", "access"], user_rows, trusted_html={2})
            + "<h2>Connected Claude connectors</h2>"
            "<p class=muted>Each sign-in of the Claude connector, newest first. xomtra "
            "keeps no Home Assistant token of the user: it is revoked right after the "
            "sign-in. Disconnect stops a sign-in within a minute.</p>"
            "<p class=muted>Sign-in lifetime in days: "
            f"<strong>{self.settings.session_days}</strong></p>"
            "<p class=muted>After it, the user connects again from a network where the "
            "browser reaches Home Assistant.</p>"
            + table(["user", "signed in", "ends", "last used", ""], session_rows, trusted_html={4})
        )
        return HTMLResponse(page("Access", body, user.get("username")))

    async def access_role(self, request: Request, user: dict[str, Any]) -> Response:
        refused = self._access_refused(user)
        if refused is not None or self.access is None:
            return refused or Response(status_code=404)
        form = await self._form(request)
        ha_user_id, role = form.get("id", "")[:128], form.get("role", "")
        if role not in ("", "xomtra.Reader"):
            return HTMLResponse(page("Access", "<p class=bad>Unknown role.</p>", None), 400)
        try:
            done = await asyncio.to_thread(
                self.access.set_ha_user_role, ha_user_id, role or None, user.get("username")
            )
            await self._audit(
                "access_role",
                user,
                arguments={"ha_user_id": ha_user_id, "role": role or None},
                outcome="ok" if done else "error",
                error=None if done else "unknown user",
            )
        except (AccessStoreError, AuditError):
            return HTMLResponse(
                page("Access", "<p class=bad>The access store or audit is unavailable.</p>", None),
                503,
            )
        return RedirectResponse(f"{prefix()}/access", status_code=303)

    async def access_disconnect(self, request: Request, user: dict[str, Any]) -> Response:
        refused = self._access_refused(user)
        if refused is not None or self.access is None:
            return refused or Response(status_code=404)
        session_id = (await self._form(request)).get("id", "")[:128]
        try:
            done = await asyncio.to_thread(
                self.access.revoke_session, session_id, user.get("username")
            )
            await self._audit(
                "access_disconnect",
                user,
                arguments={"session": session_id},
                outcome="ok" if done else "error",
                error=None if done else "not found or already disconnected",
            )
        except (AccessStoreError, AuditError):
            return HTMLResponse(
                page("Access", "<p class=bad>The access store or audit is unavailable.</p>", None),
                503,
            )
        return RedirectResponse(f"{prefix()}/access", status_code=303)

    def _endpoint(self) -> str:
        """MCP endpoint for clients: the setting, else (Ingress) the HA host on :8000."""
        if self.settings.mcp_url is None and (guess := view.current.get().endpoint):
            return guess
        return self.settings.endpoint

    def _bundles_ca(self) -> bool:
        """The extension carries the server's own CA (local access with Caddy's CA)."""
        return self.settings.access_mode == "local" and self.settings.local_ca_url is not None

    async def _local_ca(self) -> bytes | None:
        """Root certificate of the server's own CA, None when it cannot be read."""
        url = self.settings.local_ca_url
        if not self._bundles_ca() or url is None:
            return None
        try:
            async with httpx2.AsyncClient(timeout=5, trust_env=False) as client:
                response = await client.get(str(url))
            response.raise_for_status()
        except httpx2.HTTPError as err:
            logger.warning("Cannot read the local CA certificate from %s: %s", url, err)
            return None
        if not is_ca_pem(response.content):
            logger.warning("The local CA certificate from %s is not a PEM certificate", url)
            return None
        return response.content

    def _connect_help(self, token: str | None = None) -> str:
        url = self._endpoint()
        tok = token or "xmt_..."
        origin = urlparse(url)
        ca = f"{origin.scheme}://{origin.netloc}/xomtra-ca.crt"
        tls = origin.scheme == "https"
        pinned = tls and self._bundles_ca()
        server = json.dumps(
            {
                "mcpServers": {
                    "xomtra": {
                        "type": "http",
                        "url": url,
                        "headers": {"Authorization": f"Bearer {tok}"},
                    }
                }
            },
            indent=2,
        )
        download = (
            f"<p><a class=button href='{prefix()}/connect/{MCPB_FILENAME}' "
            f"download='{MCPB_FILENAME}'>Download for Claude Desktop</a></p>"
        )
        return (
            "<h2>Claude Desktop</h2>"
            + download
            + "<ol>"
            + (
                f"<li>Open the downloaded {MCPB_FILENAME}: Claude Desktop shows the extension. "
                f"The address <code>{e(url)}</code> and the certificate of this server are "
                "filled in; the extension trusts only this certificate, so nothing has to "
                "be imported into the computer.</li>"
                "<li>Paste the token and install. The token is not part of the file, "
                "Claude Desktop keeps it as a secret.</li></ol>"
                if pinned
                else f"<li>Open the downloaded {MCPB_FILENAME}: Claude Desktop shows the "
                f"extension and asks for the token. The address <code>{e(url)}</code> is "
                "filled in.</li>"
            )
            + (
                ""
                if pinned
                else "<li>Paste the token. If your browser warned about the certificate of this "
                "server (xomtra's own certificate), this is required for Claude Desktop: "
                f"also download <a href='{e(ca)}'>xomtra-ca.crt</a> and select it "
                "as <em>Certificate of the server</em>.</li></ol>"
                if tls
                else "<li>Paste the token. The address uses plain http://, so "
                "<em>Allow unencrypted http://</em> is checked; keep it.</li></ol>"
                if origin.scheme == "http"
                else "<li>Paste the token.</li></ol>"
            )
            + "<h2>Claude Code</h2>"
            f"<pre>claude mcp add --transport http xomtra {e(url)} \\\n"
            f'  --header "Authorization: Bearer {e(tok)}"</pre>'
            + (
                "<p class=muted>With the server's own certificate, start Claude Code with "
                "<code>NODE_EXTRA_CA_CERTS=/path/to/xomtra-ca.crt</code>.</p>"
                if tls
                else ""
            )
            + "<h2>Other MCP clients</h2>"
            "<p>Streamable HTTP endpoint with a bearer token, e.g.:</p>"
            f"<pre>{e(server)}</pre>"
        )

    async def connect_page(self, request: Request, user: dict[str, Any]) -> Response:
        refused = self._local_only(user, "Connect clients")
        if refused is not None:
            return refused
        body = (
            f"<p>Issue a personal token on <a href='{prefix()}/tokens'>MCP tokens</a> first, one "
            "per person and device.</p>" + CLIENTS_NOTE + self._connect_help()
        )
        return HTMLResponse(page("Connect clients", body, user.get("username")))

    async def mcpb(self, request: Request, user: dict[str, Any]) -> Response:
        endpoint = self._endpoint()
        ca = await self._local_ca() if urlparse(endpoint).scheme == "https" else None
        data = build_mcpb(endpoint, ca_pem=ca)
        return Response(
            data,
            # Not application/zip: Safari then saves it as .zip.
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{MCPB_FILENAME}"; '
                f"filename*=UTF-8''{quote(MCPB_FILENAME)}"
            },
        )

    # ---- app -----------------------------------------------------------------------

    # ---- first-start setup (ADR 077) ---------------------------------------------------

    def _setup_form(self, code: str, current: HAConnection | None, error: str | None) -> str:
        url = current.url if current else ""
        keep = "<p class=muted>Leave the token empty to keep the stored one.</p>" if current else ""
        return (
            (f"<p class=bad>{e(error)}</p>" if error else "")
            + "<p>xomtra reads your Home Assistant with a long-lived access token of its own"
            " user. In Home Assistant, create a user for xomtra (it does not need to be an"
            " administrator), sign in as that user and create a long-lived access token on"
            " its profile page (Security tab).</p>"
            f"<form class=filters method=post action='{prefix()}{setup.PATH}'>"
            + (f"<input type=hidden name=code value='{e(code)}'>" if code else "")
            + "<label>Home Assistant URL <input name=url required maxlength=300 size=40 "
            f"placeholder='http://192.168.1.10:8123' value='{e(url)}'></label>"
            "<label>Long-lived access token <input name=token type=password maxlength=1000 "
            f"size=40 autocomplete=new-password spellcheck=false{'' if current else ' required'}>"
            "</label>"
            "<button>Connect</button></form>"
            + keep
            + "<p class=muted>The address must be reachable from this server (not only from"
            " your browser). The token is stored encrypted in xomtra's database.</p>"
        )

    def _setup_actor(self, user: dict[str, Any] | None) -> dict[str, Any]:
        return user or {"sub": "setup-code", "username": "setup", "roles": [], "idp": "setup"}

    async def setup_page(self, request: Request) -> Response:  # noqa: PLR0911 - one exit per case
        title = "Connect Home Assistant"
        if self.ingress or not (self.ha.storable or self.ha.from_env):
            return Response("Not found", status_code=404)
        user = self.user(request)
        who = user.get("username") if user else None
        if self.ha.from_env:
            body = (
                "<p>The Home Assistant connection is set in the environment "
                "(<code>XOMTRA_HA_URL</code>, <code>XOMTRA_HA_TOKEN</code> in .env). Change "
                "it there and restart xomtra.</p>"
            )
            return HTMLResponse(page(title, body, who), headers=self.headers)
        current = await asyncio.to_thread(self.ha.get)
        form = await self._form(request) if request.method == "POST" else {}
        code = form.get("code") or request.query_params.get("code", "")
        by_code = bool(
            current is None
            and code
            and self._session_key
            and setup.code_matches(self._session_key, code)
        )
        if user is None and not by_code:
            if current is not None:
                return RedirectResponse(f"{prefix()}/login", status_code=303)
            if code:
                await asyncio.sleep(1)  # slows down guessing
            body = (
                ("<p class=bad>This setup code is not valid.</p>" if code else "")
                + "<p>xomtra is not connected to Home Assistant yet. Open the setup link from "
                "the server, or enter its code here:</p>"
                "<pre>docker compose exec admin xomtra-admin setup-link</pre>"
                f"<form class=filters method=get action='{prefix()}{setup.PATH}'>"
                "<input name=code required maxlength=40 size=24 placeholder='XXXX-XXXX-XXXX-XXXX'"
                " autocomplete=off><button>Continue</button></form>"
            )
            return HTMLResponse(page(title, body, None), 403 if code else 200, headers=self.headers)
        if request.method == "POST":
            if not self._same_origin(request):
                return Response("Cross-site request refused", status_code=403)
            return await self._setup_save(form, user, code if by_code else "", current)
        body = self._setup_form(code if by_code else "", current, None)
        return HTMLResponse(page(title, body, who), headers=self.headers)

    async def _setup_save(
        self,
        form: dict[str, str],
        user: dict[str, Any] | None,
        code: str,
        current: HAConnection | None,
    ) -> Response:
        title = "Connect Home Assistant"
        who = user.get("username") if user else None
        # A token has no whitespace: a line break or space copied with it (a wrapped
        # terminal line, a trailing newline) would make Home Assistant reject it.
        token = "".join(form.get("token", "").split()) or (current.token if current else "")
        try:
            url = normalize_url(form.get("url", ""))
        except ValueError as err:
            body = self._setup_form(code, current, str(err))
            return HTMLResponse(page(title, body, who), 400, headers=self.headers)
        if not token:
            body = self._setup_form(code, current, "Enter the access token.")
            return HTMLResponse(page(title, body, who), 400, headers=self.headers)
        ha = await status.ha_status(url, token)
        if not ha.get("ok") and (with_port := _default_ha_port(url)):
            # Without a port the address means 80/443, where often something else answers
            # (and may even refuse the token); Home Assistant itself listens on 8123.
            retry = await status.ha_status(with_port, token)
            if retry.get("ok"):
                url, ha = with_port, retry
            else:
                ha["hint"] = (
                    "The address has no port. Home Assistant normally listens on the port "
                    "8123, for example http://192.168.1.10:8123."
                )
        if not ha.get("ok"):
            body = connection_error(ha) + self._setup_form(code, current, None)
            return HTMLResponse(page(title, body, who), 400, headers=self.headers)
        actor = self._setup_actor(user)
        dsn, key = self.settings.audit_dsn, self._settings_key()
        try:
            await self._audit("setup", actor, arguments={"url": url, "ha_version": ha["version"]})
            await asyncio.to_thread(
                functools.partial(
                    save_connection,
                    str(dsn),
                    str(key),
                    url,
                    token,
                    ha_version=ha["version"],
                    by=str(actor["username"]),
                )
            )
        except (AuditError, StoreError) as err:
            logger.warning("Setup not saved: %s", err)
            body = self._setup_form(code, current, "Not saved: the database is unavailable.")
            return HTMLResponse(page(title, body, who), 503, headers=self.headers)
        self.ha.invalidate()
        logger.info(
            "Home Assistant %s connected at %s by %s", ha["version"], url, actor["username"]
        )
        next_step = (
            f"<p><a href='{prefix()}/'>Back to the overview</a></p>"
            if user
            else "<p>Now sign in with your Home Assistant administrator account: "
            f"<a href='{prefix()}/login'>Sign in</a></p>"
        )
        body = (
            "<p class=ok>Home Assistant is connected.</p>"
            + cards(
                [
                    ("Home Assistant", ha_host(url), "ok"),
                    ("Entities in HA", ha.get("entities"), ""),
                ]
            )
            + "<p>xomtra starts loading the history now; the first findings come after the "
            "next nightly run.</p>" + next_step
        )
        return HTMLResponse(page(title, body, who), headers=self.headers)

    def _settings_key(self) -> str | None:
        key = self.settings.settings_key
        return key.get_secret_value() if key else None

    def build(self) -> ASGIApp:
        async def css(_: Request) -> Response:
            return Response(CSS, media_type="text/css", headers={"Cache-Control": "max-age=3600"})

        async def asset(request: Request) -> Response:
            name = request.path_params.get("name") or request.url.path.rsplit("/", 1)[-1]
            if name not in STATIC_TYPES:
                return Response("Not found", status_code=404)
            return Response(
                (STATIC / name).read_bytes(),
                media_type=STATIC_TYPES[name],
                headers={"Cache-Control": "max-age=86400"},
            )

        async def healthz(_: Request) -> Response:
            return JSONResponse({"status": "ok"})

        async def root(_: Request) -> Response:
            return RedirectResponse(f"{prefix()}/", status_code=308)

        p = self.protected
        routes = [
            Route("/healthz", healthz),
            Route(PREFIX, root),
            Route(f"{PREFIX}/", p("overview", self.overview)),
            Route(f"{PREFIX}/catalog", p("catalog", self.catalog)),
            Route(f"{PREFIX}/drift", p("drift", self.drift)),
            Route(f"{PREFIX}/retention", p("retention", self.retention)),
            Route(
                f"{PREFIX}/retention",
                p("retention_setting", self.retention_post, audit=False),
                methods=["POST"],
            ),
            Route(f"{PREFIX}/audit", p("audit", self.audit_page)),
            Route(f"{PREFIX}/analysis", p("analysis", self.analysis_page)),
            Route(
                f"{PREFIX}/analysis",
                p("analysis_setting", self.analysis_post, audit=False),
                methods=["POST"],
            ),
            Route(f"{PREFIX}/answers", p("answers", self.answers.show)),
            Route(
                f"{PREFIX}/answers",
                p("answers_change", self.answers.post, audit=False),
                methods=["POST"],
            ),
            Route(f"{PREFIX}/answers/export", p("answers_export", self.answers.export)),
            Route(f"{PREFIX}/findings", p("findings", self.findings_page)),
            Route(
                f"{PREFIX}/findings/dismiss",
                p("finding_dismiss", self.finding_dismiss, audit=False),
                methods=["POST"],
            ),
            Route(
                f"{PREFIX}/findings/undo",
                p("finding_undismiss", self.finding_undo, audit=False),
                methods=["POST"],
            ),
            Route(f"{PREFIX}/tokens", p("tokens", self.tokens_page)),
            Route(
                f"{PREFIX}/tokens/issue",
                p("token_issue", self.token_issue, audit=False),
                methods=["POST"],
            ),
            Route(
                f"{PREFIX}/tokens/revoke",
                p("token_revoke", self.token_revoke, audit=False),
                methods=["POST"],
            ),
            Route(f"{PREFIX}/access", p("access", self.access_page)),
            Route(
                f"{PREFIX}/access/role",
                p("access_role", self.access_role, audit=False),
                methods=["POST"],
            ),
            Route(
                f"{PREFIX}/access/disconnect",
                p("access_disconnect", self.access_disconnect, audit=False),
                methods=["POST"],
            ),
            Route(f"{PREFIX}/connect", p("connect", self.connect_page)),
            Route(f"{PREFIX}/connect/{MCPB_FILENAME}", p("download_mcpb", self.mcpb)),
            Route(f"{PREFIX}/static.css", css),
            Route(f"{PREFIX}/fonts/{{name}}", asset),
            Route(f"{PREFIX}/favicon.svg", asset),
            Route(f"{PREFIX}/ui.js", asset),
            Route(f"{PREFIX}/tips.json", asset),
            Route(f"{PREFIX}/login", self.login),
            Route(f"{PREFIX}{setup.PATH}", self.setup_page, methods=["GET", "POST"]),
            Route(f"{PREFIX}/auth/callback", self.callback),
            Route(f"{PREFIX}/auth/check", self.check),
            Route(f"{PREFIX}/logout", self.logout, methods=["POST"]),
        ]
        return RequestView(Starlette(routes=routes), self.settings, self.has_access_page())


def _lang(scope: Scope) -> str:
    if scope["type"] != "http":
        return "en"
    headers = Headers(scope=scope)
    cookies = SimpleCookie()
    with suppress(CookieError):
        cookies.load(headers.get("cookie", ""))
    cookie = cookies.get(i18n.COOKIE)
    return i18n.pick(cookie.value if cookie else None, headers.get("accept-language"))


class RequestView:
    """Sets the per-request :class:`View`; behind Ingress also guards the entry.

    With ``auth_mode=ingress`` only the Supervisor's proxy may call the GUI (its
    X-Remote-User-* headers sign the user in), and the app sees its paths without the
    ``/admin`` prefix, which is put back here so that the routes stay the same.
    """

    def __init__(self, app: ASGIApp, settings: AdminSettings, access_page: bool) -> None:
        self.app = app
        self.ingress = settings.auth_mode == "ingress"
        self.proxy = settings.ingress_proxy
        self.base = View(access=access_page)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        lang = _lang(scope)
        if scope["type"] != "http" or not self.ingress or scope["path"] == "/healthz":
            token = view.current.set(replace(self.base, lang=lang))
            try:
                await self.app(scope, receive, send)
            finally:
                view.current.reset(token)
            return
        client = scope.get("client")
        if not client or not _same_address(client[0], self.proxy):
            await Response("Only Home Assistant Ingress", status_code=403)(scope, receive, send)
            return
        ingress_path = Headers(scope=scope).get("x-ingress-path", "")
        if not INGRESS_PATH.fullmatch(ingress_path):
            await Response("Missing X-Ingress-Path", status_code=400)(scope, receive, send)
            return
        path = scope["path"] if scope["path"].startswith("/") else "/" + scope["path"]
        scope = dict(scope)
        scope["path"] = PREFIX + path
        scope["raw_path"] = PREFIX.encode() + scope.get("raw_path", path.encode())
        host = Headers(scope=scope).get("x-forwarded-host", "").split(",")[0].strip()
        hostname = urlparse(f"//{host}").hostname if host else None
        endpoint = f"http://{_url_host(hostname)}:{MCP_PORT}/mcp" if hostname else None
        token = view.current.set(
            View(
                prefix=ingress_path,
                access=False,
                sign_out=False,
                endpoint=endpoint,
                lang=lang,
            )
        )
        try:
            await self.app(scope, receive, send)
        finally:
            view.current.reset(token)
