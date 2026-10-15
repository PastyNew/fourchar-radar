"""Value objects shared by every layer of the radar.

Everything here is frozen: findings are facts that were observed at a point in
time, so they must not be mutated in place once recorded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class Tier(StrEnum):
    """Coarse value buckets, ordered from least to most valuable."""

    COMMON = "common"
    UNCOMMON = "uncommon"
    RARE = "rare"
    EPIC = "epic"
    LEGENDARY = "legendary"

    @property
    def rank(self) -> int:
        """Position in the value ladder; higher is more valuable."""
        return TIER_ORDER[self]


TIER_ORDER: dict[Tier, int] = {tier: i for i, tier in enumerate(Tier)}

#: Sidebar/embed colours per tier (Discord renders these as accent bars).
TIER_COLORS: dict[Tier, int] = {
    Tier.COMMON: 0x9BA5B4,
    Tier.UNCOMMON: 0x3BA55C,
    Tier.RARE: 0x5865F2,
    Tier.EPIC: 0x9B59B6,
    Tier.LEGENDARY: 0xF1C40F,
}


def utcnow() -> datetime:
    """Timezone-aware 'now'. Single seam so tests can reason about time."""
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Verdict:
    """The raw answer to 'is this name still free?'."""

    name: str
    available: bool
    checked_at: datetime
    latency_ms: float = 0.0


@dataclass(frozen=True, slots=True)
class Quote:
    """A heuristic valuation of an available name.

    This is an *estimate for triage*, not a market quote. See
    ``pricing.estimator`` for the scoring model and its assumptions.
    """

    name: str
    price_usd: int
    score: float
    tier: Tier
    reasons: tuple[str, ...] = ()

    @property
    def price_label(self) -> str:
        """Human-friendly price, e.g. ``$850`` / ``$12.4k`` / ``$1.2M``."""
        usd = self.price_usd
        if usd >= 1_000_000:
            return f"${usd / 1_000_000:.1f}M"
        if usd >= 10_000:
            return f"${usd / 1_000:.1f}k"
        return f"${usd:,}"


@dataclass(frozen=True, slots=True)
class Finding:
    """An available name plus its valuation. This is what gets alerted on."""

    verdict: Verdict
    quote: Quote

    @property
    def name(self) -> str:
        return self.verdict.name

    @property
    def tier(self) -> Tier:
        return self.quote.tier


@dataclass(slots=True)
class ScanStats:
    """Mutable running counters for a single scan session."""

    started_at: datetime = field(default_factory=utcnow)
    checked: int = 0
    taken: int = 0
    found: int = 0
    errors: int = 0
    skipped: int = 0
    alerts_sent: int = 0

    @property
    def elapsed_s(self) -> float:
        return (utcnow() - self.started_at).total_seconds()

    @property
    def rps(self) -> float:
        elapsed = self.elapsed_s
        return self.checked / elapsed if elapsed > 0 else 0.0

    @property
    def free_rate(self) -> float:
        """Share of checked names that turned out to be free."""
        return self.found / self.checked if self.checked else 0.0

    def summary(self) -> str:
        return (
            f"checked={self.checked} taken={self.taken} free={self.found} "
            f"errors={self.errors} alerts={self.alerts_sent}"
        )
