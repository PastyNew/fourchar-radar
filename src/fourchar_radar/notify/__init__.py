"""Alerting backends.

``WebhookNotifier`` is the default and the only one with a hard dependency-free
implementation. :class:`~fourchar_radar.notify.bot.RadarBot` is loaded lazily so
that ``import fourchar_radar.notify`` never requires ``discord.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .base import Notifier, NullNotifier
from .embed import build_finding_embed, build_scan_report_embed, build_throttle_embed
from .webhook import WebhookNotifier, redact_webhook

if TYPE_CHECKING:  # pragma: no cover
    from .bot import RadarBot

__all__ = [
    "Notifier",
    "NullNotifier",
    "RadarBot",
    "WebhookNotifier",
    "build_finding_embed",
    "build_scan_report_embed",
    "build_throttle_embed",
    "redact_webhook",
]


def __getattr__(name: str) -> Any:
    """Resolve ``RadarBot`` on first access so discord.py stays optional."""
    if name == "RadarBot":
        from .bot import RadarBot

        return RadarBot
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
