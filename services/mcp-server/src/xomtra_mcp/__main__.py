"""Entry point: ``xomtra-mcp`` / ``python -m xomtra_mcp``."""

from __future__ import annotations

import logging

from hacore.lake import LakeSettings
from xomtra_mcp.config import Settings
from xomtra_mcp.data import DataAccess
from xomtra_mcp.server import create_server

logger = logging.getLogger("xomtra_mcp")


def build_data(settings: Settings) -> DataAccess | None:
    if not settings.data_enabled:
        return None
    try:
        lake = LakeSettings.from_env()
    except ValueError as err:
        logger.warning("Data tools disabled: %s", err)
        return None
    return DataAccess.from_settings(
        lake, timezone=settings.timezone, query_timeout=settings.query_timeout_seconds
    )


def main() -> None:
    settings = Settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    server = create_server(settings, data=build_data(settings))
    server.run(transport="http", host=settings.host, port=settings.port, show_banner=False)


if __name__ == "__main__":
    main()
