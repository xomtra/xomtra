"""FastMCP application: catalog and data tools behind the Entra OAuth proxy.

Every request passes the policy middleware (roles -> tools, views, entities) and is
audited (ADR 038-040). Data tools are registered when a lake connection is configured.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
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
from hammond_mcp.identity import Identity, current_identity
from hammond_mcp.policy import Policy, current_access

logger = logging.getLogger(__name__)

INSTRUCTIONS = """\
HAmmond exposes the history and catalog of a Home Assistant installation.

Start with `search_catalog` to find entities (by words, area, domain or kind), then
`describe_entity` for details and data quality, and `get_timeseries` for values over
time. `list_views` shows the SQL views you may query with `query_sql` (DuckDB SQL, one
SELECT, view names without schema). All times are UTC unless stated otherwise. What you
can see depends on your role; entities outside it are not returned.
"""

READ_ONLY = {"readOnlyHint": True, "openWorldHint": False}


def build_auth(settings: Settings) -> AzureProvider | None:
    if settings.auth_mode == "none":
        logger.warning("Authentication is DISABLED (local development only)")
        return None
    client_id, tenant_id = settings.entra_client_id, settings.entra_tenant_id
    secret, signing_key = settings.entra_client_secret, settings.jwt_signing_key
    if not (client_id and tenant_id and secret and signing_key and settings.base_url):
        raise RuntimeError("Entra configuration is incomplete")  # guarded by Settings
    return AzureProvider(
        client_id=client_id,
        client_secret=secret.get_secret_value(),
        tenant_id=tenant_id,
        base_url=str(settings.base_url).rstrip("/"),
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
) -> FastMCP:
    mcp = FastMCP(
        name="HAmmond",
        instructions=INSTRUCTIONS,
        auth=build_auth(settings),
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
        }

    @mcp.tool(annotations=READ_ONLY)
    def server_info() -> dict[str, Any]:
        """Return HAmmond server version and which back-ends are configured."""
        return {
            "name": "HAmmond",
            "version": __version__,
            "auth_mode": settings.auth_mode,
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
