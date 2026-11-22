"""FastMCP application: minimal PoC tools behind the Entra OAuth proxy."""

from __future__ import annotations

import logging
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.auth.providers.azure import AzureProvider
from starlette.requests import Request
from starlette.responses import JSONResponse

from hacore import HAClient, fetch_registries
from hammond_mcp import __version__
from hammond_mcp.config import Settings
from hammond_mcp.identity import current_identity

logger = logging.getLogger(__name__)

INSTRUCTIONS = """\
HAmmond exposes the history and catalog of a Home Assistant installation.
This is an early proof of concept: only identity and overview tools are available.
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


def create_server(settings: Settings) -> FastMCP:
    mcp = FastMCP(name="HAmmond", instructions=INSTRUCTIONS, auth=build_auth(settings))

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

    return mcp
