"""Admin GUI web app (Starlette), mounted by Caddy under ``/admin`` (ADR 041).

Access: Caddy lets ``/admin`` through only from ``ADMIN_CIDR``; the app then requires
an Entra sign-in with the role ``HAmmond.Admin``. ``/admin/auth/check`` answers Caddy's
``forward_auth`` for the Dagster UI under ``/admin/dagster/``. Every page view, sign-in
and refused sign-in is written to the audit table.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from hammond_mcp.admin import status
from hammond_mcp.admin.config import PREFIX, AdminSettings
from hammond_mcp.admin.html import CSS, cards, e, page, table
from hammond_mcp.admin.oidc import EntraOIDC, LoginError, LoginTransaction
from hammond_mcp.admin.session import Signer
from hammond_mcp.audit import AuditError, AuditRecord, AuditSink, LogAuditSink
from hammond_mcp.data import DataAccess

logger = logging.getLogger(__name__)

SESSION_COOKIE = "hammond_admin"
TX_COOKIE = "hammond_admin_tx"
KINDS = ("measurement", "counter", "binary", "enum", "event", "text", "unknown")
HEADERS = {
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; form-action "
    "'self' https://login.microsoftonline.com",
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
}

type Handler = Callable[[Request, dict[str, Any]], Awaitable[Response]]


class AdminApp:
    def __init__(
        self,
        settings: AdminSettings,
        *,
        data: DataAccess | None,
        audit: AuditSink | None = None,
        oidc: EntraOIDC | None = None,
    ) -> None:
        self.settings = settings
        self.data = data
        self.audit = audit or LogAuditSink()
        key = settings.admin_session_key
        secret = key.get_secret_value() if key else "dev-only-session-key-not-for-production"
        self.sessions = Signer(secret, "session")
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

    # ---- session -------------------------------------------------------------------

    def user(self, request: Request) -> dict[str, Any] | None:
        if self.settings.auth_mode == "none":
            return {"sub": "dev", "name": "Developer", "roles": [self.settings.admin_role]}
        data = self.sessions.loads(request.cookies.get(SESSION_COOKIE))
        if data is None or self.settings.admin_role not in data.get("roles", []):
            return None
        return data

    def _cookie(self, resp: Response, name: str, value: str, max_age: int) -> None:
        resp.set_cookie(
            name,
            value,
            max_age=max_age,
            path=PREFIX,
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
            error=error,
        )
        await self.audit.write(record)

    def protected(self, name: str, handler: Handler) -> Callable[[Request], Awaitable[Response]]:
        async def endpoint(request: Request) -> Response:
            user = self.user(request)
            if user is None:
                return RedirectResponse(f"{PREFIX}/login", status_code=303)
            try:
                await self._audit(name, user, arguments=dict(request.query_params))
            except AuditError:
                return HTMLResponse("Audit is unavailable.", status_code=503, headers=HEADERS)
            resp = await handler(request, user)
            resp.headers.update(HEADERS)
            return resp

        return endpoint

    # ---- auth routes ---------------------------------------------------------------

    async def login(self, request: Request) -> Response:
        if self.oidc is None:
            return RedirectResponse(f"{PREFIX}/", status_code=303)
        tx = LoginTransaction.new()
        resp = RedirectResponse(self.oidc.authorize_url(tx), status_code=303)
        value = self.transactions.dumps(
            {"state": tx.state, "nonce": tx.nonce, "verifier": tx.verifier}, 600
        )
        self._cookie(resp, TX_COOKIE, value, 600)
        return resp

    async def callback(self, request: Request) -> Response:
        if self.oidc is None:
            return RedirectResponse(f"{PREFIX}/", status_code=303)
        tx_data = self.transactions.loads(request.cookies.get(TX_COOKIE))
        state, code = request.query_params.get("state"), request.query_params.get("code")
        if tx_data is None or not state or state != tx_data.get("state") or not code:
            return self._login_failed("The sign-in expired or was not started here.")
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
        }
        if self.settings.admin_role not in roles:
            await self._audit(
                "login", user, decision="denied", outcome="denied", error="missing admin role"
            )
            return self._login_failed(
                f"Your account does not have the role {self.settings.admin_role}.", status_code=403
            )
        await self._audit("login", user)
        hours = self.settings.admin_session_hours
        resp = RedirectResponse(f"{PREFIX}/", status_code=303)
        self._cookie(resp, SESSION_COOKIE, self.sessions.dumps(user, hours * 3600), hours * 3600)
        resp.delete_cookie(TX_COOKIE, path=PREFIX)
        return resp

    def _login_failed(self, message: str, status_code: int = 400) -> Response:
        body = f"<p class=bad>{e(message)}</p><p><a href='{PREFIX}/login'>Sign in again</a></p>"
        return HTMLResponse(page("Sign-in failed", body, None), status_code, headers=HEADERS)

    async def logout(self, request: Request) -> Response:
        origin = request.headers.get("origin")
        expected = urlparse(str(self.settings.base_url or request.base_url))
        if origin and urlparse(origin).netloc != expected.netloc:
            return Response("Cross-site request refused", status_code=403)
        resp = RedirectResponse(f"{PREFIX}/login", status_code=303)
        resp.delete_cookie(SESSION_COOKIE, path=PREFIX)
        return resp

    async def check(self, request: Request) -> Response:
        """Caddy forward_auth for the Dagster UI: 200 with the user, else to sign-in."""
        user = self.user(request)
        if user is None:
            return RedirectResponse(f"{PREFIX}/login", status_code=302)
        return Response(status_code=200, headers={"X-Hammond-User": str(user.get("username"))})

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
        body = cards(
            [
                ha_card,
                ("Entities in HA", ha.get("entities"), ""),
                ("Last state in lake (UTC)", last_state, fresh_cls),
                ("Last statistic (UTC)", lake.get("last_statistic"), ""),
            ]
        )
        if ha.get("error"):
            body += f"<p class=bad>{e(ha['error'])}</p>"
        kinds = lake.get("kinds") or []
        body += "<h2>Catalog</h2>"
        if kinds:
            body += f"<p class=muted>Profiled at {e(kinds[0]['profiled_at'])}</p>" + table(
                ["kind", "entities", "stale"],
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
        return HTMLResponse(page("Overview", body, user.get("username")))

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
            ("States", "HAMMOND_STATES_RETENTION_DAYS", s.states_retention_days),
            (
                "5-minute statistics",
                "HAMMOND_STATISTICS_5MIN_RETENTION_DAYS",
                s.statistics_5min_retention_days,
            ),
            (
                "Hourly statistics",
                "HAMMOND_STATISTICS_HOUR_RETENTION_DAYS",
                s.statistics_hour_retention_days,
            ),
        ]
        body = (
            "<p class=muted>Applied daily at 03:30 by the Dagster job retention_daily; data "
            "files disappear after the snapshot retention (7 days). Change the values in "
            "<code>.env</code> and restart the ingest containers.</p>"
        )
        body += table(
            ["data", "variable", "days", "deleted before (UTC)"],
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
            body += table(cols, [[r.get(c) for c in cols] for r in rows], {5, 6})
        return HTMLResponse(page("Audit", body, user.get("username")))

    async def recent_audit(self) -> list[dict[str, Any]] | None:
        dsn = self.settings.audit_dsn
        if not dsn:
            return None

        def query() -> list[dict[str, Any]]:
            import psycopg2  # noqa: PLC0415

            with psycopg2.connect(dsn, connect_timeout=5) as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT ts, username, tool, decision, outcome, row_count, duration_ms, "
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

    # ---- app -----------------------------------------------------------------------

    def build(self) -> Starlette:
        async def css(_: Request) -> Response:
            return Response(CSS, media_type="text/css", headers={"Cache-Control": "max-age=3600"})

        async def healthz(_: Request) -> Response:
            return JSONResponse({"status": "ok"})

        async def root(_: Request) -> Response:
            return RedirectResponse(f"{PREFIX}/", status_code=308)

        p = self.protected
        routes = [
            Route("/healthz", healthz),
            Route(PREFIX, root),
            Route(f"{PREFIX}/", p("overview", self.overview)),
            Route(f"{PREFIX}/catalog", p("catalog", self.catalog)),
            Route(f"{PREFIX}/drift", p("drift", self.drift)),
            Route(f"{PREFIX}/retention", p("retention", self.retention)),
            Route(f"{PREFIX}/audit", p("audit", self.audit_page)),
            Route(f"{PREFIX}/static.css", css),
            Route(f"{PREFIX}/login", self.login),
            Route(f"{PREFIX}/auth/callback", self.callback),
            Route(f"{PREFIX}/auth/check", self.check),
            Route(f"{PREFIX}/logout", self.logout, methods=["POST"]),
        ]
        return Starlette(routes=routes)
