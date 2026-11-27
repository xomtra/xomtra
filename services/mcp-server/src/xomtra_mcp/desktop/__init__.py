"""Claude Desktop extension (``.mcpb``) for the local access mode (ADR 056).

The extension is a zip with ``manifest.json`` and a dependency-free Node bridge
(``server/index.js``) that forwards MCP over stdio to the xomtra endpoint with the
personal token. :func:`build_mcpb` fills in the version and, for downloads from the
admin GUI, the server's endpoint as the default address and, with the local access
mode, the root certificate of the server's own CA (``server/xomtra-ca.crt``). The
bridge then trusts only that certificate for the connection (pinned), so the computer's
trust store needs no change.

The token is never part of the file: Claude Desktop asks for it at install and keeps
it as a sensitive value. A file with the token would be a password lying in the
Downloads folder, in backups and in chat attachments; the address and the certificate
are public.
"""

from __future__ import annotations

import io
import json
import zipfile
from importlib.resources import files
from typing import Any

from xomtra_mcp import __version__

FILENAME = "xomtra.mcpb"
CA_NAME = "server/xomtra-ca.crt"
# Fixed timestamp: the same inputs give the same bytes (reproducible release artifact).
_ZIP_TIME = (2026, 1, 1, 0, 0, 0)


BUNDLED_CA_NOTE = (
    "Not needed: this extension already carries the certificate of the server and "
    "trusts only it. Choose a file only if the server got a new certificate authority; "
    "better download the extension again."
)


def manifest(
    endpoint: str | None = None, version: str = __version__, bundled_ca: bool = False
) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(files(__name__).joinpath("manifest.json").read_text())
    data["version"] = version
    if endpoint:
        data["user_config"]["url"]["default"] = endpoint
        # A server known to answer on plain http (the add-on in the LAN) would otherwise
        # be refused until the user found the checkbox; https stays the default.
        if endpoint.startswith("http://"):
            data["user_config"]["allow_http"]["default"] = True
    if bundled_ca:
        data["user_config"]["ca_file"]["description"] = BUNDLED_CA_NOTE
    return data


def is_ca_pem(data: bytes) -> bool:
    """A single PEM certificate of a sane size (what Caddy's local CA serves)."""
    text = data.strip()
    return (
        0 < len(text) < 16_384
        and text.startswith(b"-----BEGIN CERTIFICATE-----")
        and text.endswith(b"-----END CERTIFICATE-----")
        and text.count(b"-----BEGIN") == 1
    )


def build_mcpb(
    endpoint: str | None = None, version: str = __version__, ca_pem: bytes | None = None
) -> bytes:
    """The extension; ``ca_pem`` (the server's own CA) is pinned by the bridge."""
    entries: list[tuple[str, str | bytes]] = [
        (
            "manifest.json",
            json.dumps(manifest(endpoint, version, ca_pem is not None), indent=2) + "\n",
        ),
        ("server/index.js", files(__name__).joinpath("server/index.js").read_text()),
        ("icon.png", files(__name__).joinpath("icon.png").read_bytes()),
    ]
    if ca_pem is not None:
        if not is_ca_pem(ca_pem):
            raise ValueError("not a single PEM certificate")
        entries.append((CA_NAME, ca_pem.strip() + b"\n"))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries:
            info = zipfile.ZipInfo(name, date_time=_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, content)
    return buffer.getvalue()
