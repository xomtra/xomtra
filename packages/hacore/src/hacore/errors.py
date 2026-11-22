"""Exceptions raised by hacore."""


class HAError(Exception):
    """Base class for Home Assistant client errors."""


class HAConnectionError(HAError):
    """The connection could not be established or was lost."""


class HAAuthError(HAError):
    """Home Assistant rejected the access token."""


class HAProtocolError(HAError):
    """Home Assistant sent an unexpected message."""


class HACommandError(HAError):
    """A command returned ``success: false``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
