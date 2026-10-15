"""Notifier interface.

The scanner only knows about this one method, which is what keeps the Discord
bot an *optional* dependency: the default implementation posts to a webhook and
needs nothing but a URL. ``notify/bot.py`` offers the same interface with slash
commands for anyone who wants to drive the radar from inside Discord.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import Finding


@runtime_checkable
class Notifier(Protocol):
    """Anything that can tell a human about a finding."""

    async def notify(self, finding: Finding) -> None:
        """Deliver *finding*. Raise on failure; the scanner will log it."""
        ...


class NullNotifier:
    """Drops everything. Useful for dry runs and tests."""

    delivered: list[Finding]

    def __init__(self) -> None:
        self.delivered = []

    async def notify(self, finding: Finding) -> None:
        self.delivered.append(finding)

    async def close(self) -> None:
        return None
