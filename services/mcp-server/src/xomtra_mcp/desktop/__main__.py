"""``python -m xomtra_mcp.desktop [--url URL] [--version X.Y.Z] > xomtra.mcpb``

Writes the Claude Desktop extension to stdout (release asset, ADR 056). Without --url
the address is left empty and Claude Desktop asks for it at install.
"""

from __future__ import annotations

import argparse
import sys

from xomtra_mcp import __version__
from xomtra_mcp.desktop import build_mcpb


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m xomtra_mcp.desktop")
    parser.add_argument("--url", help="MCP endpoint filled in as the default address")
    parser.add_argument("--version", default=__version__)
    args = parser.parse_args(argv)
    sys.stdout.buffer.write(build_mcpb(args.url, version=args.version))
    return 0


if __name__ == "__main__":
    sys.exit(main())
