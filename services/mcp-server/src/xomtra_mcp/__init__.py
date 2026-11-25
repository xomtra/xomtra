"""Xomtra MCP server."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("xomtra-mcp")
except PackageNotFoundError:  # pragma: no cover - source checkout without install
    __version__ = "0.0.0"
