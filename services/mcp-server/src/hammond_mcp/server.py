"""FastMCP application: catalog and data tools behind authentication.

Public access: OAuth proxy to Entra ID (ADR 026) or Home Assistant (ADR 053). Local
access: personal tokens from the admin GUI (ADR 056/059).

Every request passes the policy middleware (roles -> tools, views, entities) and is
audited (ADR 038-040). Data tools are registered when a lake connection is configured.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider
from fastmcp.server.auth.providers.azure import AzureProvider
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from hacore import HAClient, fetch_registries
from hammond_mcp import __version__
from hammond_mcp.audit import AuditSink, LogAuditSink, PostgresAuditSink
from hammond_mcp.config import Settings
from hammond_mcp.data import DataAccess, parse_time
from hammond_mcp.guard import PolicyMiddleware
from hammond_mcp.ha_auth import HomeAssistantProvider
from hammond_mcp.identity import Identity, current_identity
from hammond_mcp.policy import Policy, current_access
from hammond_mcp.tokens import PersonalTokenVerifier, PostgresTokenStore, TokenStore

logger = logging.getLogger(__name__)

INSTRUCTIONS = """\
HAmmond exposes the history and catalog of a Home Assistant installation.

Start with `search_catalog` to find entities (by words, area, domain or kind), then
`describe_entity` for details and data quality, and `get_timeseries` for values over
time. `get_findings` lists what the daily analysis found (correlations between
entities, anomalies, lasting level changes); `get_weekly_summary` what changed in a
week. `list_views` shows the SQL views you may query with `query_sql` (DuckDB SQL, one
SELECT, view names without schema). All times are UTC unless stated otherwise. What you
can see depends on your role; entities outside it are not returned.
"""

READ_ONLY = {"readOnlyHint": True, "openWorldHint": False}


def build_auth(settings: Settings, tokens: TokenStore | None = None) -> AuthProvider | None:
    if settings.auth_mode == "none":
        logger.warning("Authentication is DISABLED (local development only)")
        return None
    if settings.access_mode == "local":
        dsn = settings.audit_dsn
        if tokens is None and dsn is None:
            raise RuntimeError("Local access needs the audit database")  # guarded by Settings
        logger.info("Local access: personal tokens only, no OAuth")
        return PersonalTokenVerifier(tokens or PostgresTokenStore(str(dsn)))
    signing_key, base_url = settings.jwt_signing_key, settings.base_url
    if settings.auth_mode == "ha":
        ha_url, auth_url = settings.ha_url, settings.browser_ha_url
        if not (ha_url and auth_url and signing_key and base_url):
            raise RuntimeError("Home Assistant auth configuration is incomplete")
        return HomeAssistantProvider(
            base_url=str(base_url),
            ha_url=ha_url,
            ha_auth_url=auth_url,
            jwt_signing_key=signing_key.get_secret_value(),
            role_map=settings.role_map,
            allowed_users=settings.ha_allowed_users,
            allowed_client_redirect_uris=settings.allowed_client_redirect_uris,
        )
    client_id, tenant_id = settings.entra_client_id, settings.entra_tenant_id
    secret = settings.entra_client_secret
    if not (client_id and tenant_id and secret and signing_key and base_url):
        raise RuntimeError("Entra configuration is incomplete")  # guarded by Settings
    return AzureProvider(
        client_id=client_id,
        client_secret=secret.get_secret_value(),
        tenant_id=tenant_id,
        base_url=str(base_url).rstrip("/"),
        required_scopes=settings.entra_required_scopes,
        jwt_signing_key=signing_key.get_secret_value(),
        allowed_client_redirect_uris=settings.allowed_client_redirect_uris,
    )


def build_identity(settings: Settings) -> Any:
    if settings.auth_mode == "none":
        dev = Identity(authenticated=True, subject="dev", roles=tuple(settings.dev_roles))
        return lambda: dev
    return current_identity


def build_audit(settings: Settings) -> AuditSink:
    dsn = settings.audit_dsn
    return PostgresAuditSink(dsn) if dsn else LogAuditSink()


def create_server(
    settings: Settings,
    *,
    data: DataAccess | None = None,
    audit: AuditSink | None = None,
    policy: Policy | None = None,
    tokens: TokenStore | None = None,
) -> FastMCP:
    mcp = FastMCP(
        name="HAmmond",
        instructions=INSTRUCTIONS,
        auth=build_auth(settings, tokens),
        mask_error_details=True,
    )
    mcp.add_middleware(
        PolicyMiddleware(
            policy or Policy.load(settings.policy_file),
            audit or build_audit(settings),
            build_identity(settings),
        )
    )

    @mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @mcp.tool(annotations=READ_ONLY)
    def whoami() -> dict[str, Any]:
        """Return the identity and app roles of the signed-in user, as HAmmond sees them."""
        ident = current_identity()
        return {
            "authenticated": ident.authenticated,
            "name": ident.name,
            "username": ident.username,
            "roles": list(ident.roles),
            "scopes": list(ident.scopes),
            "identity_provider": ident.idp,
        }

    @mcp.tool(annotations=READ_ONLY)
    def server_info() -> dict[str, Any]:
        """Return HAmmond server version and which back-ends are configured."""
        return {
            "name": "HAmmond",
            "version": __version__,
            "auth_mode": settings.auth_mode,
            "access_mode": settings.access_mode,
            "home_assistant_configured": settings.ha_configured,
        }

    ha_url, ha_token = settings.ha_url, settings.ha_token
    if ha_url and ha_token:

        @mcp.tool(annotations=READ_ONLY)
        async def ha_overview() -> dict[str, Any]:
            """Summarise the Home Assistant installation: counts of floors, areas, devices
            and entities per domain (from the registries, no state history)."""
            async with HAClient(ha_url, ha_token.get_secret_value()) as ha:
                snapshot = await fetch_registries(ha)
                version = ha.ha_version
            return {
                "home_assistant_version": version,
                "floors": len(snapshot.floors),
                "areas": len(snapshot.areas),
                "devices": len(snapshot.devices),
                "entities_by_domain": snapshot.domain_counts(),
                "fetched_at": snapshot.fetched_at.isoformat(),
            }

    if data is not None:
        register_data_tools(mcp, data)
    return mcp


def register_data_tools(mcp: FastMCP, data: DataAccess) -> None:
    """Catalog and data tools; each runs under the caller's policy (current_access)."""

    async def run(fn: Any, *args: Any, **kwargs: Any) -> Any:
        # The DuckDB work runs in a thread; the access context is copied along.
        return await asyncio.to_thread(fn, current_access(), *args, **kwargs)

    @mcp.tool(annotations=READ_ONLY)
    async def search_catalog(  # noqa: PLR0917 - MCP tool arguments
        query: Annotated[
            str, Field(description="Words that must all appear (name, id, area, device...)")
        ] = "",
        domain: Annotated[str | None, Field(description="e.g. sensor, binary_sensor")] = None,
        area: Annotated[str | None, Field(description="Area or floor name")] = None,
        kind: Annotated[
            Literal["measurement", "counter", "binary", "enum", "event", "text", "unknown"] | None,
            Field(description="Profile kind of the entity"),
        ] = None,
        include_disabled: bool = False,
        limit: Annotated[int, Field(ge=1, le=500)] = 50,
    ) -> dict[str, Any]:
        """Find entities in the catalog. Returns id, name, area, device, unit, kind and how
        often the entity changes; most active entities first."""
        result: dict[str, Any] = await run(
            data.search_catalog,
            query=query,
            domain=domain,
            area=area,
            kind=kind,
            include_disabled=include_disabled,
            limit=limit,
        )
        return result

    @mcp.tool(annotations=READ_ONLY)
    async def describe_entity(
        entity_id: Annotated[str, Field(description="e.g. sensor.living_room_temperature")],
    ) -> dict[str, Any]:
        """Everything known about one entity: metadata, 30-day profile (value range,
        missing share, top states, stale), latest state, statistics metadata and drift."""
        result: dict[str, Any] = await run(data.describe_entity, entity_id)
        return result

    @mcp.tool(annotations=READ_ONLY)
    async def list_views() -> dict[str, Any]:
        """SQL views available to you for query_sql, with their columns."""
        views: list[dict[str, Any]] = await run(data.list_views)
        return {"views": views, "row_count": len(views)}

    @mcp.tool(annotations=READ_ONLY)
    async def get_timeseries(
        entity_id: str,
        start: Annotated[
            str, Field(description="ISO 8601 (UTC if no offset) or relative, e.g. 7d, 12h")
        ] = "1d",
        end: Annotated[str | None, Field(description="ISO 8601 or relative; default now")] = None,
        resolution: Annotated[
            Literal["auto", "raw", "5minute", "hour", "day"],
            Field(
                description="raw = every state change; buckets use HA statistics when "
                "available (mean/min/max, state/sum), otherwise aggregate state changes; "
                "auto = raw up to 2 days, hourly up to 31 days, else daily"
            ),
        ] = "auto",
    ) -> dict[str, Any]:
        """Values of one entity over time."""
        start_ts, end_ts = parse_time(start), parse_time(end)
        result: dict[str, Any] = await run(
            data.get_timeseries, entity_id, start=start_ts, end=end_ts, resolution=resolution
        )
        return result

    @mcp.tool(annotations=READ_ONLY)
    async def get_findings(  # noqa: PLR0917 - MCP tool arguments
        finding_type: Annotated[
            Literal["correlation", "anomaly", "change_point"] | None,
            Field(
                description="correlation = two entities move together beyond the daily/"
                "weekly cycle and the weather; anomaly = unusual hours of one entity "
                "against the same hour of the week; change_point = lasting level shift. "
                "Default: all."
            ),
        ] = None,
        entity_id: Annotated[
            str | None, Field(description="Only findings involving this entity")
        ] = None,
        window_days: Annotated[
            int | None, Field(description="Only this analysis window (e.g. 365 or 30)")
        ] = None,
        include_flagged: Annotated[
            bool,
            Field(
                description="Also findings flagged as trivial or explained: "
                "duplicate_suspect, same_device, short (one hour), weather, level_shift, "
                "common_factor, indirect, same_area, unstable"
            ),
        ] = False,
        new_since: Annotated[
            str | None,
            Field(description="Only findings first detected since (ISO 8601 or e.g. 7d)"),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 20,
    ) -> dict[str, Any]:
        """Findings of the daily analysis with entity names, strongest first within each
        type and window. Trivial or explained findings are hidden unless include_flagged;
        counts show how many exist. score: correlation r, or robust z (anomaly), or shift
        in robust deviations (change_point); observed/expected in the entity's unit."""
        since = parse_time(new_since) if new_since else None
        result: dict[str, Any] = await run(
            data.get_findings,
            finding_type=finding_type,
            entity_id=entity_id,
            window_days=window_days,
            include_flagged=include_flagged,
            new_since=since,
            limit=limit,
        )
        return result

    @mcp.tool(annotations=READ_ONLY)
    async def get_weekly_summary(
        week: Annotated[
            str | None,
            Field(description="Any date (YYYY-MM-DD) in the week; default the last week"),
        ] = None,
        top: Annotated[int, Field(ge=1, le=50)] = 10,
    ) -> dict[str, Any]:
        """What the analysis found in one week (Monday to Sunday, local time): anomalies
        and change points of the week, correlations new or gone since the week before,
        and counts. Built from weekly snapshots of the findings."""
        try:
            day = date.fromisoformat(week) if week else None
        except ValueError as err:
            raise ValueError(f"Invalid week {week!r}: use YYYY-MM-DD") from err
        result: dict[str, Any] = await run(data.get_weekly_summary, week=day, top=top)
        return result

    @mcp.tool(annotations=READ_ONLY)
    async def query_sql(
        sql: Annotated[
            str,
            Field(
                description="One DuckDB SELECT over the views from list_views (no schema "
                "prefix, no file or table functions). Rows are capped by your role."
            ),
        ],
    ) -> dict[str, Any]:
        """Run a read-only SQL query over the data views."""
        result: dict[str, Any] = await run(data.query_sql, sql)
        return result
