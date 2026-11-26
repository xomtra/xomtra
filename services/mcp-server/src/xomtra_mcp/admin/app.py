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

from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from hacore.analysis_settings import BY_KEY, SPECS, Spec, Value
from xomtra_mcp.admin import i18n, status, view
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
from xomtra_mcp.admin.config import PREFIX, AdminSettings
from xomtra_mcp.admin.html import CSS, cards, e, fmt, page, table, token_flow
from xomtra_mcp.admin.oidc import EntraOIDC, LoginError, LoginTransaction
from xomtra_mcp.admin.session import Signer
from xomtra_mcp.admin.view import View, prefix
from xomtra_mcp.audit import AuditError, AuditRecord, AuditSink, LogAuditSink
from xomtra_mcp.data import DataAccess
from xomtra_mcp.desktop import FILENAME as MCPB_FILENAME
from xomtra_mcp.desktop import build_mcpb
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
TOKEN_DAYS = (30, 90, 365, 0)
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


def _url_host(hostname: str) -> str:
    return f"[{hostname}]" if ":" in hostname else hostname


type Handler = Callable[[Request, dict[str, Any]], Awaitable[Response]]


# The add-on keeps the lake out of Home Assistant backups (config.yaml backup_exclude).
BACKUP_NOTE = (
    "<h2 id=backups>Backups</h2>"
    "<p class=warn>Home Assistant backups do not include xomtra's analytics. They keep "
    "the tokens, the settings and the dismissed findings, but not the lake: the "
    "history, the findings and the weekly summaries, which take tens of GB. After a "
    "restore xomtra starts a new lake and loads the history again from Home "
    "Assistant, only as far back as Home Assistant still keeps it, and computes the "
    "findings again.<span class=more>For a robust setup with backups of the whole "
    "history, run xomtra on its own virtual machine (Proxmox, VMware or Docker on "
    "Linux) and back up that machine.</span></p>"
)


# Shown on the token pages: the local access mode only serves clients in the LAN.
CLIENTS_NOTE = (
    "<p class=warn><strong>Which AI assistants can connect</strong>"
    "<span class=more>"
    "This server runs in your local network and accepts personal tokens. "
    "Assistants that run on a computer in this network can use it: Claude Desktop"
    " (the extension below), Claude Code and other MCP clients that send a bearer"
    " token, such as VS Code or Cursor."
    "</span><span class=more>"
    "Assistants that connect from the cloud cannot reach it: claude.ai on the web"
    " and on phones, or ChatGPT connectors. They need a public https:// address "
    "with sign-in, which the installation on its own virtual machine provides."
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
        if ha_login is None and settings.auth_mode == "ha":
            ha_login = HomeAssistantLogin(
                ha_url=settings.ha_url or "",
                ha_auth_url=settings.ha_auth_url or settings.ha_url or "",
                client_id=settings.client_id,
                redirect_uri=settings.redirect_uri,
            )
        self.ha_login = ha_login
        dsn = settings.audit_dsn
        if tokens is None and settings.access_mode == "local" and dsn:
            tokens = PostgresTokenStore(dsn)
        self.tokens = tokens
        if analysis is None and dsn:
            analysis = PostgresSettingsStore(dsn)
        self.analysis = analysis
        self.ingress = settings.auth_mode == "ingress"
        self.headers = dict(HEADERS)
        if self.ingress:
            # Home Assistant shows the GUI in an iframe of its own origin.
            self.headers["Content-Security-Policy"] = (
                "default-src 'self'; frame-ancestors 'self'; form-action 'self'"
            )
            self.headers["X-Frame-Options"] = "SAMEORIGIN"
        elif ha_login is not None:
            origin = urlparse(ha_login.ha_auth_url)
            self.headers["Content-Security-Policy"] = (
                "default-src 'self'; frame-ancestors 'none'; form-action 'self' "
                f"{origin.scheme}://{origin.netloc}"
            )

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
        if self.ha_login is not None:
            tx = LoginTransaction.new()
            resp = RedirectResponse(self.ha_login.authorize_url(tx.state), status_code=303)
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
        if self.ingress or (self.oidc is None and self.ha_login is None):
            return RedirectResponse(f"{prefix()}/", status_code=303)
        tx_data = self.transactions.loads(request.cookies.get(TX_COOKIE))
        state, code = request.query_params.get("state"), request.query_params.get("code")
        if tx_data is None or not state or state != tx_data.get("state") or not code:
            return self._login_failed("The sign-in expired or was not started here.")
        if self.ha_login is not None or self.oidc is None:
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
        if self.ha_login is None:
            return self._login_failed("Sign-in is not configured.")
        try:
            ha_user = await self.ha_login.exchange(code)
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
        token = self.settings.ha_token
        ha = await status.ha_status(
            self.settings.ha_url, token.get_secret_value() if token else None
        )
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
            ha_card = ("Home Assistant", f"{ha['version']} · {ha['latency_ms']} ms", "ok")
        else:
            ha_card = ("Home Assistant", "unreachable", "bad")
        disk = self._disk()
        body = cards(
            [
                ha_card,
                *([disk[0]] if disk else []),
                ("Entities in HA", ha.get("entities"), ""),
                ("Last state in lake", last_state, fresh_cls),
                ("Last statistic", lake.get("last_statistic"), ""),
            ]
        )
        if ha.get("error"):
            body += f"<p class=bad>{e(ha['error'])}</p>"
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
            body += "<p class=muted>No profile yet (job catalog_daily).</p>"
        drift = lake.get("drift_7d") or []
        body += "<h2>Drift, last 7 days</h2>" + (
            table(["type", "count"], [(d["drift_type"], d["n"]) for d in drift], {1})
            if drift
            else "<p class=muted>None.</p>"
        )
        if self.ingress:
            body += BACKUP_NOTE
        return HTMLResponse(page("Overview", body, user.get("username")))

    def _disk(self) -> tuple[tuple[str, Any, str], str] | None:
        """Card and note on the free space of the lake's disk (ADR 072)."""
        path = self.settings.lake_data_path
        if not path:
            return None
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            return ("Free disk", "unknown", "warn"), ""
        ratio = usage.free / usage.total if usage.total else 1.0
        value = f"{usage.free / 2**30:,.1f} GiB · {ratio:.0%}"
        stop, low = self.settings.worker_stop_free_ratio, self.settings.worker_min_free_ratio
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
            f"<option value='{k}'{' selected' if k == kind else ''}>{k}</option>"
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
        body += table(cols, [list(r.values()) for r in rows], numeric)
        return HTMLResponse(page("Catalog", body, user.get("username")))

    async def drift(self, request: Request, user: dict[str, Any]) -> Response:
        data = self.data
        if data is None:
            return self._no_data(user, "Drift")
        rows = await asyncio.to_thread(status.drift_rows, data)
        cols = ["detected_at", "entity_id", "drift_type", "field", "old_value", "new_value"]
        body = "<p class=muted>Last 30 days</p>" + table(cols, [[r[c] for c in cols] for r in rows])
        return HTMLResponse(page("Drift", body, user.get("username")))

    async def retention(self, request: Request, user: dict[str, Any]) -> Response:
        s = self.settings
        now = datetime.now(UTC)
        periods = [
            ("States", "XOMTRA_STATES_RETENTION_DAYS", s.states_retention_days),
            (
                "5-minute statistics",
                "XOMTRA_STATISTICS_5MIN_RETENTION_DAYS",
                s.statistics_5min_retention_days,
            ),
            (
                "Hourly statistics",
                "XOMTRA_STATISTICS_HOUR_RETENTION_DAYS",
                s.statistics_hour_retention_days,
            ),
        ]
        body = (
            "<p class=muted>Applied daily at 03:30 by the Dagster job retention_daily; data "
            "files disappear after the snapshot retention (7 days). Change the values in "
            "<code>.env</code> and restart the ingest containers.</p>"
        )
        body += table(
            ["data", "variable", "days", "deleted before"],
            [(n, v, d, (now - timedelta(days=d)).date().isoformat()) for n, v, d in periods],
            {2},
        )
        body += f"<p class=muted>Catalog profile window: {e(s.catalog_window_days)} days.</p>"
        if self.data is not None:
            sizes = await asyncio.to_thread(status.table_sizes, self.data)
            body += "<h2>Tables in the lake</h2>" + table(
                ["schema", "table", "rows (est.)", "files", "MiB", "delete files"],
                [list(r.values()) for r in sizes],
                {2, 3, 4, 5},
            )
        return HTMLResponse(page("Retention", body, user.get("username")))

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
        body = "<p class=muted>Last 200 calls (MCP tools and this GUI)</p>"
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
        title = "Analysis settings"
        if self.analysis is None:
            body = "<p class=warn>The audit database is not configured for the admin GUI.</p>"
            return HTMLResponse(page(title, body, user.get("username")))
        try:
            current = await asyncio.to_thread(self.analysis.current)
            history = await asyncio.to_thread(self.analysis.history, 50)
        except SettingsStoreError:
            body = "<p class=bad>The settings store is unavailable.</p>"
            return HTMLResponse(page(title, body, user.get("username")), 503)
        body = note + (
            "<p class=muted>A stored value wins over the ingest's environment variable, which "
            "wins over the default. The next analysis run (daily) uses it; no restart. "
            "The thresholds set the rate of false findings, not their number (ADR 060, "
            "062): a larger home rightly finds more.</p>"
            "<p class=muted><small>Don't change it, if it works.</small></p>"
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
            "<p class=muted>A dismissed finding is flagged <code>dismissed</code> from the "
            "next analysis run on and hidden like the other flags (Claude sees it only with "
            "<code>include_flagged</code>). It applies to the pair of entities (correlation) "
            "or the entity (anomaly, change point) in every window, until undone or the "
            "chosen date.</p>"
            "<form class=filters method=get>"
            f"<select name=type>{options}</select>"
            f"<label><input type=checkbox name=flagged value=1{' checked' if flagged else ''}>"
            " include flagged</label><button>Filter</button></form>"
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
                "see, so a stricter threshold does not help (ADR 067). Assign the entities "
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

    def _local_only(self, user: dict[str, Any], title: str) -> Response | None:
        if self.settings.access_mode == "local" and self.tokens is not None:
            return None
        body = (
            "<p class=warn>Personal tokens are used only in the local access mode "
            "(<code>XOMTRA_ACCESS_MODE=local</code>, see docs/setup/local-access.md). "
            "In the public mode, Claude connects with OAuth (docs/setup/claude-connector.md)."
            "</p>"
        )
        return HTMLResponse(page(title, body, user.get("username")))

    async def tokens_page(self, request: Request, user: dict[str, Any]) -> Response:
        refused = self._local_only(user, "Tokens")
        if refused is not None or self.tokens is None:
            return refused or Response(status_code=404)
        try:
            items = await asyncio.to_thread(self.tokens.list)
        except TokenStoreError:
            return HTMLResponse(
                page("Tokens", "<p class=bad>The token store is unavailable.</p>", None), 503
            )
        roles = "".join(f"<option>{e(r)}</option>" for r in TOKEN_ROLES)
        days = "".join(
            f"<option value={d}{' selected' if d == 365 else ''}>"
            f"{f'{d} days' if d else 'never'}</option>"
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
            + CLIENTS_NOTE
            + token_flow(prefix(), MCPB_FILENAME)
            + form
            + table(
                ["id", "for", "description", "role", "created", "expires", "last used", ""],
                rows,
                trusted_html={7},
            )
            + f"<p><a href='{prefix()}/connect'>How to connect Claude and other MCP clients</a></p>"
        )
        return HTMLResponse(page("Tokens", body, user.get("username")))

    async def _form(self, request: Request) -> dict[str, str]:
        raw = (await request.body())[:10_000].decode(errors="replace")
        return {k: v[0].strip() for k, v in parse_qs(raw).items() if v}

    async def token_issue(self, request: Request, user: dict[str, Any]) -> Response:
        refused = self._local_only(user, "Tokens")
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
                page("Tokens", "<p class=bad>Fill in all fields.</p>", user.get("username")),
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
                    "Tokens",
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
            + f"<p><a href='{prefix()}/tokens'>Back to tokens</a></p>"
        )
        return HTMLResponse(page("New token", body, user.get("username")))

    async def token_revoke(self, request: Request, user: dict[str, Any]) -> Response:
        refused = self._local_only(user, "Tokens")
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
                    "Tokens",
                    "<p class=bad>The token store or audit is unavailable.</p>",
                    user.get("username"),
                ),
                503,
            )
        return RedirectResponse(f"{prefix()}/tokens", status_code=303)

    def _endpoint(self) -> str:
        """MCP endpoint for clients: the setting, else (Ingress) the HA host on :8000."""
        if self.settings.mcp_url is None and (guess := view.current.get().endpoint):
            return guess
        return self.settings.endpoint

    def _connect_help(self, token: str | None = None) -> str:
        url = self._endpoint()
        tok = token or "xmt_..."
        origin = urlparse(url)
        ca = f"{origin.scheme}://{origin.netloc}/xomtra-ca.crt"
        tls = origin.scheme == "https"
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
        return (
            "<h2>Claude Desktop</h2><ol>"
            f"<li>Download <a href='{prefix()}/connect/{MCPB_FILENAME}' "
            f"download='{MCPB_FILENAME}'>{MCPB_FILENAME}</a> "
            f"(address <code>{e(url)}</code> is filled in) and open it: Claude Desktop "
            "shows the extension and asks for the token.</li>"
            + (
                "<li>Paste the token. If your browser warned about the certificate of this "
                f"server, also download <a href='{e(ca)}'>xomtra-ca.crt</a> and select it "
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
            "<p class=muted>claude.ai on the web and on phones connects through Anthropic's "
            "servers and needs the public access mode (docs/setup/claude-connector.md).</p>"
        )

    async def connect_page(self, request: Request, user: dict[str, Any]) -> Response:
        refused = self._local_only(user, "Connect clients")
        if refused is not None:
            return refused
        body = (
            f"<p>Issue a personal token on <a href='{prefix()}/tokens'>Tokens</a> first, one "
            "per person and device.</p>" + CLIENTS_NOTE + self._connect_help()
        )
        return HTMLResponse(page("Connect clients", body, user.get("username")))

    async def mcpb(self, request: Request, user: dict[str, Any]) -> Response:
        data = build_mcpb(self._endpoint())
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
            Route(f"{PREFIX}/audit", p("audit", self.audit_page)),
            Route(f"{PREFIX}/analysis", p("analysis", self.analysis_page)),
            Route(
                f"{PREFIX}/analysis",
                p("analysis_setting", self.analysis_post, audit=False),
                methods=["POST"],
            ),
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
            Route(f"{PREFIX}/connect", p("connect", self.connect_page)),
            Route(f"{PREFIX}/connect/{MCPB_FILENAME}", p("download_mcpb", self.mcpb)),
            Route(f"{PREFIX}/static.css", css),
            Route(f"{PREFIX}/fonts/{{name}}", asset),
            Route(f"{PREFIX}/favicon.svg", asset),
            Route(f"{PREFIX}/ui.js", asset),
            Route(f"{PREFIX}/tips.json", asset),
            Route(f"{PREFIX}/login", self.login),
            Route(f"{PREFIX}/auth/callback", self.callback),
            Route(f"{PREFIX}/auth/check", self.check),
            Route(f"{PREFIX}/logout", self.logout, methods=["POST"]),
        ]
        return RequestView(Starlette(routes=routes), self.settings)


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

    def __init__(self, app: ASGIApp, settings: AdminSettings) -> None:
        self.app = app
        self.ingress = settings.auth_mode == "ingress"
        self.proxy = settings.ingress_proxy
        self.base = View(dagster=settings.dagster_ui and not self.ingress)

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
                dagster=False,
                sign_out=False,
                endpoint=endpoint,
                lang=lang,
            )
        )
        try:
            await self.app(scope, receive, send)
        finally:
            view.current.reset(token)
