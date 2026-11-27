"""Per-request view of the admin GUI: URL prefix and navigation.

Behind Caddy the GUI lives under ``/admin``. Behind Home Assistant Ingress (add-on,
ADR 070) the browser sees it under ``/api/hassio_ingress/<token>``, which only the
request tells (``X-Ingress-Path``); links, form actions and redirects use
:func:`prefix`, which the request middleware sets.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass

from xomtra_mcp.admin.config import PREFIX


@dataclass(frozen=True, slots=True)
class View:
    prefix: str = PREFIX
    access: bool = False  # the page Access (Claude connector, public mode with HA sign-in)
    sign_out: bool = True  # no own session behind Ingress, HA signs the user in
    endpoint: str | None = None  # MCP endpoint guessed from the request (Ingress)
    lang: str = "en"  # GUI language (cs, de, en, nl, pl), see i18n.py


current: ContextVar[View] = ContextVar("xomtra_admin_view", default=View())  # noqa: B039


def prefix() -> str:
    return current.get().prefix
