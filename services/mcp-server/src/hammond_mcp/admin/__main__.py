"""Entry point: ``hammond-admin`` (admin GUI, behind Caddy under /admin or HA Ingress)."""

from __future__ import annotations

import logging

import uvicorn

from hacore.lake import LakeSettings
from hammond_mcp.admin.app import AdminApp
from hammond_mcp.admin.config import AdminSettings
from hammond_mcp.audit import AuditSink, LogAuditSink, PostgresAuditSink
from hammond_mcp.data import DataAccess

logger = logging.getLogger("hammond_mcp.admin")


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


if __name__ == "__main__":
    main()
