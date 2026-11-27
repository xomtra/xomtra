"""First-start setup of the compose edition (ADR 077).

Until Home Assistant is connected nobody can sign in with a Home Assistant account, so
the setup page accepts a one-time code instead. The code is derived from the admin
session key (stable across restarts, nothing to store). The whole link is written to the
log of the admin container (``docker compose logs admin``) and printed on demand by
``docker compose exec admin xomtra-admin setup-link``, which the installation runs
right after ``docker compose up -d``. It stops
working as soon as a connection is stored: from then on the page is for signed-in
administrators only (to change the URL or the token).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging

logger = logging.getLogger(__name__)

PATH = "/setup"


def setup_code(session_key: str) -> str:
    """16 characters [A-Z2-7] (80 bits), grouped for reading off a terminal."""
    digest = hmac.new(session_key.encode(), b"xomtra-setup-code:v1", hashlib.sha256).digest()
    raw = base64.b32encode(digest).decode()[:16]
    return "-".join(raw[i : i + 4] for i in range(0, 16, 4))


def code_matches(session_key: str, offered: str) -> bool:
    normalized = offered.strip().upper().replace(" ", "")
    expected = setup_code(session_key)
    return hmac.compare_digest(normalized.encode(), expected.encode()) or hmac.compare_digest(
        normalized.encode(), expected.replace("-", "").encode()
    )


def setup_url(base_url: str, prefix: str, session_key: str) -> str:
    return f"{base_url.rstrip('/')}{prefix}{PATH}?code={setup_code(session_key)}"


def log_setup_link(url: str) -> None:
    bar = "=" * 78
    logger.warning(
        "\n%s\nxomtra is not connected to Home Assistant yet. Finish the setup in a browser"
        " on your network:\n\n    %s\n\nThe browser warns about the certificate of xomtra's"
        " own certificate authority; continue to the page (see docs/setup/docker.md).\n%s",
        bar,
        url,
        bar,
    )
