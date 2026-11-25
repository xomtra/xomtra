"""Claude Desktop extension (``.mcpb``) for the local access mode (ADR 056).

The extension is a zip with ``manifest.json`` and a dependency-free Node bridge
(``server/index.js``) that forwards MCP over stdio to the Xomtra endpoint with the
personal token. :func:`build_mcpb` fills in the version and, for downloads from the
admin GUI, the server's endpoint as the default address. The token is never part of
the file: Claude Desktop asks for it at install and keeps it as a sensitive value.
"""

from __future__ import annotations

import io
import json
import zipfile
from importlib.resources import files
from typing import Any

from xomtra_mcp import __version__

FILENAME = "xomtra.mcpb"
# Fixed timestamp: the same inputs give the same bytes (reproducible release artifact).
_ZIP_TIME = (2026, 1, 1, 0, 0, 0)


def manifest(endpoint: str | None = None, version: str = __version__) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(files(__name__).joinpath("manifest.json").read_text())
    data["version"] = version
    if endpoint:
        data["user_config"]["url"]["default"] = endpoint
    return data


def build_mcpb(endpoint: str | None = None, version: str = __version__) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in (
            ("manifest.json", json.dumps(manifest(endpoint, version), indent=2) + "\n"),
            ("server/index.js", files(__name__).joinpath("server/index.js").read_text()),
        ):
            info = zipfile.ZipInfo(name, date_time=_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, content)
    return buffer.getvalue()
