"""Entry point: ``xomtra-admin`` (admin GUI, behind Caddy under /admin or HA Ingress).

``xomtra-admin setup-link`` prints the one-time setup link to the terminal and exits
(ADR 077): ``docker compose up -d && docker compose exec admin xomtra-admin setup-link``.
"""

from __future__ import annotations

import logging
import sys

import uvicorn

from hacore.lake import LakeSettings
from xomtra_mcp.admin.app import AdminApp
from xomtra_mcp.admin.config import PREFIX, AdminSettings
from xomtra_mcp.admin.setup import log_setup_link, setup_url
from xomtra_mcp.audit import AuditSink, LogAuditSink, PostgresAuditSink
from xomtra_mcp.data import DataAccess

logger = logging.getLogger("xomtra_mcp.admin")


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if args == ["setup-link"]:
        raise SystemExit(print_setup_link(AdminSettings()))
    if args:
        raise SystemExit("usage: xomtra-admin [setup-link]")
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


def _setup_link(settings: AdminSettings) -> str | None:
    """The one-time setup link while Home Assistant is not connected, else None."""
    resolver = settings.ha_resolver()
    key = settings.admin_session_key
    if not resolver.storable or key is None or resolver.get() is not None:
        return None
    return setup_url(_base(settings), PREFIX, key.get_secret_value())


def _base(settings: AdminSettings) -> str:
    return str(settings.base_url or "https://<address-of-this-server>")


def _announce_setup(settings: AdminSettings) -> None:
    """Logs the one-time setup link while Home Assistant is not connected (ADR 077)."""
    url = _setup_link(settings)
    if url is not None:
        log_setup_link(url)


def print_setup_link(settings: AdminSettings) -> int:
    """``xomtra-admin setup-link``: the link on stdout, for the end of the installation."""
    url = _setup_link(settings)
    if url is None:
        print(
            "xomtra is connected to Home Assistant. Sign in to the admin GUI at "
            f"{_base(settings).rstrip('/')}{PREFIX}/ with your Home Assistant account."
        )
        return 0
    print(
        "Finish the setup of xomtra in a browser on your network:\n\n"
        f"    {url}\n\n"
        "The browser warns about the certificate of xomtra's own certificate authority;"
        " continue to the page (see docs/setup/docker.md)."
    )
    return 0


if __name__ == "__main__":
    main()
