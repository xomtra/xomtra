"""FastMCP application: catalog and data tools behind authentication.

Public access: OAuth proxy to Entra ID (ADR 026) or Home Assistant (ADR 053). Local
access: personal tokens from the admin GUI (ADR 056/059).

Every request passes the policy middleware (roles -> tools, views, entities) and is
audited (ADR 038-040). Data tools are registered when a lake connection is configured.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable
from datetime import date, datetime
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider
from fastmcp.server.auth.providers.azure import AzureProvider
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from hacore import HAClient, fetch_registries
from xomtra_mcp import __version__
from xomtra_mcp.access import AccessStore, PostgresAccessStore, expiry_warning
from xomtra_mcp.audit import AuditSink, LogAuditSink, PostgresAuditSink
from xomtra_mcp.config import Settings
from xomtra_mcp.data import DataAccess, parse_time
from xomtra_mcp.guard import PolicyMiddleware
from xomtra_mcp.guidelines import CORE_GUIDELINES, DEFAULT_GUIDELINES, Guidelines, ThemeSource
from xomtra_mcp.ha_auth import HAUserDirectory, HomeAssistantProvider
from xomtra_mcp.identity import Identity, current_identity
from xomtra_mcp.policy import Policy, current_access
from xomtra_mcp.tokens import PersonalTokenVerifier, PostgresTokenStore, TokenStore

logger = logging.getLogger(__name__)

OVERVIEW = """\
xomtra exposes the history and catalog of a Home Assistant installation.

Start with `search_catalog` to find entities (by words, area, domain or kind), then
`describe_entity` for details and data quality, and `get_timeseries` for values over
time. `get_findings` lists what the daily analysis found (correlations between
entities, anomalies, lasting level changes); `get_weekly_summary` what changed in a
week. `list_views` shows the SQL views you may query with `query_sql` (DuckDB SQL, one
SELECT, view names without schema). All times are UTC unless stated otherwise. What you
can see depends on your role; entities outside it are not returned.
"""


def build_instructions(guidelines: Guidelines) -> str:
    """Server instructions; the central theme changes during the run, so only the
    default guidelines go here, and results carry the current text (ADR 080)."""
    text = f"{OVERVIEW}\nCore rules (always apply):\n{CORE_GUIDELINES}"
    if not guidelines.defaults:
        return text
    return f"{text}\nHow to answer with this data (ADR 073):\n{DEFAULT_GUIDELINES}"


def rules_decorator(guidelines: Guidelines) -> Callable[[Any], Any]:
    """Appends the short answering rules to a tool's description (its docstring)."""
    rules = guidelines.tool_rules

    def apply(fn: Any) -> Any:
        fn.__doc__ = f"{inspect.cleandoc(fn.__doc__ or '')}\n\n{rules}"
        return fn

    return apply


def guidelines_attacher(guidelines: Guidelines) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Attaches the current answering guidelines to a result the model interprets."""

    def attach(result: dict[str, Any]) -> dict[str, Any]:
        return {**result, "answer_guidelines": guidelines.text()}

    return attach


READ_ONLY = {"readOnlyHint": True, "openWorldHint": False}


def build_auth(
    settings: Settings,
    tokens: TokenStore | None = None,
    access: AccessStore | None = None,
    audit: AuditSink | None = None,
) -> AuthProvider | None:
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
        dsn = settings.audit_dsn
        resolver = settings.ha_resolver()

        async def connection() -> tuple[str, str] | None:
            conn = await asyncio.to_thread(resolver.get)
            return (conn.url, conn.token) if conn is not None else None

        return HomeAssistantProvider(
            base_url=str(base_url),
            ha_url=ha_url,
            ha_auth_url=auth_url,
            jwt_signing_key=signing_key.get_secret_value(),
            role_map=settings.role_map,
            allowed_users=settings.ha_allowed_users,
            allowed_client_redirect_uris=settings.allowed_client_redirect_uris,
            store=access or (PostgresAccessStore(dsn) if dsn else None),
            require_mfa=settings.ha_require_mfa,
            session_days=settings.session_days,
            directory=HAUserDirectory(connection),
            audit=audit or build_audit(settings),
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
        # Used when Entra sends no refresh token lifetime (ADR 081).
        fallback_refresh_token_expiry_seconds=settings.session_days * 86400,
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
    theme: ThemeSource | None = None,
    access: AccessStore | None = None,
) -> FastMCP:
    guidelines = Guidelines(defaults=settings.default_guidelines, theme=theme)
    with_guidelines = guidelines_attacher(guidelines)
    mcp = FastMCP(
        name="xomtra",
        instructions=build_instructions(guidelines),
        auth=build_auth(settings, tokens, access, audit),
        mask_error_details=True,
    )
    mcp.add_middleware(
        PolicyMiddleware(
            policy or Policy.load(settings.policy_file),
            audit or build_audit(settings),
            build_identity(settings),
        )
    )

    resolver = settings.ha_resolver()

    @mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @mcp.tool(annotations=READ_ONLY)
    def whoami() -> dict[str, Any]:
        """Return the identity and app roles of the signed-in user, as xomtra sees them."""
        ident = current_identity()
        return {
            "authenticated": ident.authenticated,
            "name": ident.name,
            "username": ident.username,
            "roles": list(ident.roles),
            "scopes": list(ident.scopes),
            "identity_provider": ident.idp,
            "sign_in_ends_at": ident.session_expires_at,
        }

    @mcp.tool(annotations=READ_ONLY)
    def server_info() -> dict[str, Any]:
        """Return xomtra server version and which back-ends are configured. Call it once
        before answering about the house: it carries the guidelines for answering with
        xomtra data (answer_guidelines)."""
        info: dict[str, Any] = {
            "name": "xomtra",
            "version": __version__,
            "auth_mode": settings.auth_mode,
            "access_mode": settings.access_mode,
            "home_assistant_configured": resolver.get() is not None,
        }
        ends = current_identity().session_expires_at
        if ends and (warning := expiry_warning(datetime.fromisoformat(ends))):
            info["sign_in_notice"] = warning
        return with_guidelines(info)

    if resolver.from_env or resolver.storable:

        @mcp.tool(annotations=READ_ONLY)
        async def ha_overview() -> dict[str, Any]:
            """Summarise the Home Assistant installation: counts of floors, areas, devices
            and entities per domain (from the registries, no state history)."""
            ha_conn = await asyncio.to_thread(resolver.get)
            if ha_conn is None:
                return {"error": "Home Assistant is not connected yet (admin GUI)."}
            async with HAClient(ha_conn.url, ha_conn.token) as ha:
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
        register_data_tools(mcp, data, guidelines)
    return mcp


def register_data_tools(mcp: FastMCP, data: DataAccess, guidelines: Guidelines) -> None:
    """Catalog and data tools; each runs under the caller's policy (current_access)."""
    with_rules = rules_decorator(guidelines)
    with_guidelines = guidelines_attacher(guidelines)

    async def run(fn: Any, *args: Any, **kwargs: Any) -> Any:
        # The DuckDB work runs in a thread; the access context is copied along.
        return await asyncio.to_thread(fn, current_access(), *args, **kwargs)

    @mcp.tool(annotations=READ_ONLY)
    @with_rules
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
    @with_rules
    async def describe_entity(
        entity_id: Annotated[str, Field(description="e.g. sensor.living_room_temperature")],
    ) -> dict[str, Any]:
        """Everything known about one entity: metadata, 30-day profile (value range,
        missing share, top states, stale), latest state, statistics metadata and drift."""
        result: dict[str, Any] = await run(data.describe_entity, entity_id)
        return result

    @mcp.tool(annotations=READ_ONLY)
    @with_rules
    async def list_views() -> dict[str, Any]:
        """SQL views available to you for query_sql, with their columns."""
        views: list[dict[str, Any]] = await run(data.list_views)
        return {"views": views, "row_count": len(views)}

    @mcp.tool(annotations=READ_ONLY)
    @with_rules
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
    @with_rules
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
                "common_factor, indirect, same_area, house_context, unstable, trivial "
                "(expected relation, see trivial_rule), dismissed"
            ),
        ] = False,
        new_since: Annotated[
            str | None,
            Field(description="Only findings first detected since (ISO 8601 or e.g. 7d)"),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 20,
    ) -> dict[str, Any]:
        """Findings of the daily analysis with entity names, most useful first within
        each type and window (usefulness = sqrt(strength * evidence), ADR 068;
        action_category = what a finding is good for: fault, savings, automation,
        behaviour, none, unclassified). Trivial or explained findings are hidden unless
        include_flagged; counts show how many exist. score: correlation r, or robust z
        (anomaly), or shift in robust deviations (change_point); observed/expected in
        the entity's unit.
        confidence: per correlation window, level 'low' when the installation has few
        series or few entities with an area: the correlations are then hypotheses.
        A correlation is not a cause: present findings as hypotheses to check."""
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
        return with_guidelines(result)

    @mcp.tool(annotations=READ_ONLY)
    @with_rules
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
        return with_guidelines(result)

    @mcp.tool(annotations=READ_ONLY)
    @with_rules
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
