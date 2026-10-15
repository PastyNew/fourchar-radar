"""Exception hierarchy.

Keeping these distinct lets the scanner react appropriately: a throttle means
"wait", a bad name means "skip forever", and a transport error means "retry".
"""

from __future__ import annotations


class RadarError(Exception):
    """Base class for everything this package raises deliberately."""


class InvalidUsername(RadarError):
    """The name is not something Discord will ever accept."""

    def __init__(self, name: str, detail: str = "") -> None:
        self.name = name
        self.detail = detail
        super().__init__(f"invalid username {name!r}{': ' + detail if detail else ''}")


class Throttled(RadarError):
    """Discord kept returning 429 even after honouring ``retry_after``."""

    def __init__(self, name: str, retry_after: float) -> None:
        self.name = name
        self.retry_after = retry_after
        super().__init__(f"still throttled after {retry_after:.0f}s while checking {name!r}")


class InvalidWebhook(RadarError):
    """Discord rejected the webhook URL itself — every alert would fail."""


class CheckFailed(RadarError):
    """A request failed in a way that retries did not fix."""

    def __init__(self, name: str, status: int | None, detail: str = "") -> None:
        self.name = name
        self.status = status
        self.detail = detail
        status_text = f"HTTP {status}" if status else "transport error"
        reason = f": {detail}" if detail else ""
        super().__init__(f"check failed for {name!r}: {status_text}{reason}")


class StorageError(RadarError):
    """The SQLite queue could not be read or written."""


class Shutdown(RadarError):
    """Raised internally when a stop was requested while waiting to send."""


class ConfigError(RadarError):
    """Configuration is missing or inconsistent."""
