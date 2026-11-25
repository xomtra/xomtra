"""xomtra core library."""

from hacore.client import HAClient, websocket_url
from hacore.errors import (
    HAAuthError,
    HACommandError,
    HAConnectionError,
    HAError,
    HAProtocolError,
)
from hacore.registry import (
    Area,
    Device,
    Entity,
    Floor,
    Label,
    RegistrySnapshot,
    fetch_registries,
)

__all__ = [
    "Area",
    "Device",
    "Entity",
    "Floor",
    "HAAuthError",
    "HAClient",
    "HACommandError",
    "HAConnectionError",
    "HAError",
    "HAProtocolError",
    "Label",
    "RegistrySnapshot",
    "fetch_registries",
    "websocket_url",
]
