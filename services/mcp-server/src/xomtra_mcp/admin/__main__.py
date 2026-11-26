"""Entry point: ``xomtra-admin`` (admin GUI, behind Caddy under /admin or HA Ingress)."""

from __future__ import annotations

import logging

import uvicorn

from hacore.lake import LakeSettings
from xomtra_mcp.admin.app import AdminApp
from xomtra_mcp.admin.config import PREFIX, AdminSettings
from xomtra_mcp.admin.setup import log_setup_link
from xomtra_mcp.audit import AuditSink, LogAuditSink, PostgresAuditSink
from xomtra_mcp.data import DataAccess

logger = logging.getLogger("xomtra_mcp.admin")


def main() -> None:
    settings = AdminSettings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    data = None
    try:
        data = DataAccess.from_settings(LakeSettings.from_env(), timezone=settings.timezone)
    except ValueError as err:
        logger.warning("Lake pages disabled: %s", err)
    dsn = settings.audit_dsn
    audit: AuditSink = PostgresAuditSink(dsn) if dsn else LogAuditSink()
    app = AdminApp(settings, data=data, audit=audit).build()
    _announce_setup(settings)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.admin_port,
        # Behind Ingress the peer address itself is checked (only the Supervisor's
        # proxy), so X-Forwarded-For must not replace it.
        proxy_headers=settings.auth_mode != "ingress",
        forwarded_allow_ips="*",
        log_level=settings.log_level.lower(),
        server_header=False,
    )


def _announce_setup(settings: AdminSettings) -> None:
    """Prints the one-time setup link while Home Assistant is not connected (ADR 077)."""
    resolver = settings.ha_resolver()
    key = settings.admin_session_key
    if not resolver.storable or key is None or resolver.get() is not None:
        return
    base = str(settings.base_url or "https://<address-of-this-server>")
    log_setup_link(base, PREFIX, key.get_secret_value())


if __name__ == "__main__":
    main()
