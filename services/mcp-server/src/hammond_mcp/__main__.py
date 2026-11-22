"""Entry point: ``hammond-mcp`` / ``python -m hammond_mcp``."""

from __future__ import annotations

import logging

from hammond_mcp.config import Settings
from hammond_mcp.server import create_server


def main() -> None:
    settings = Settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    server = create_server(settings)
    server.run(transport="http", host=settings.host, port=settings.port, show_banner=False)


if __name__ == "__main__":
    main()
