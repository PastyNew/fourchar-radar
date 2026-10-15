"""Shared fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from fourchar_radar.config import Settings
from fourchar_radar.models import Finding, Quote, Tier, Verdict, utcnow
from fourchar_radar.storage import Store

WEBHOOK = "https://discord.com/api/webhooks/123/secret-token"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """A valid Settings instance with no dependency on the developer's .env."""
    return Settings(
        discord_webhook_url=WEBHOOK,
        db_path=str(tmp_path / "test.db"),
        rate_limit_rps=20.0,  # at the config ceiling; tests build fast limiters directly
        concurrency=1,
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store(tmp_path / "store.db") as s:
        yield s


@pytest.fixture
def make_finding():  # type: ignore[no-untyped-def]
    """Factory for Finding objects with sensible defaults."""

    def _make(name: str = "huhu", available: bool = True, price: int = 1200) -> Finding:
        verdict = Verdict(name=name, available=available, checked_at=utcnow(), latency_ms=42.0)
        quote = Quote(
            name=name,
            price_usd=price,
            score=float(price),
            tier=Tier.RARE,
            reasons=("exact English word", "easy to say out loud"),
        )
        return Finding(verdict=verdict, quote=quote)

    return _make
