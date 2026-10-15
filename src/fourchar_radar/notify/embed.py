"""Discord embed rendering.

Plain dicts rather than ``discord.Embed`` so the webhook path stays free of
the discord.py dependency.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC
from typing import Any

from ..models import TIER_COLORS, Finding, ScanStats, Tier

#: A Discord embed payload. Deliberately loose: Discord adds fields regularly
#: and pinning the exact shape would break on every new one.
EmbedDict = dict[str, Any]


def _duration(seconds: float) -> str:
    """Human duration, escalating to days for multi-day scans."""
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m"), (1, "s")):
        if seconds >= size:
            value = seconds / size
            return f"{value:.0f}{unit}" if value >= 10 or unit == "s" else f"{value:.1f}{unit}"
    return f"{seconds:.0f}s"


BRAND = "fourchar-radar"
EMBED_FOOTER = "Heuristic estimate — not a market valuation."
TIER_EMOJI: dict[Tier, str] = {
    Tier.COMMON: "⚪",
    Tier.UNCOMMON: "🟢",
    Tier.RARE: "🔵",
    Tier.EPIC: "🟣",
    Tier.LEGENDARY: "🟡",
}


def _tier_label(tier: Tier) -> str:
    return f"{TIER_EMOJI[tier]} {tier.value.upper()}"


def build_finding_embed(finding: Finding) -> EmbedDict:
    """The alert a human actually reads."""
    quote, verdict = finding.quote, finding.verdict
    checked = verdict.checked_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")

    return {
        "title": f"🎯 `{quote.name}` is free",
        "description": "\n".join(f"• {reason}" for reason in quote.reasons),
        "color": TIER_COLORS[quote.tier],
        "fields": [
            {"name": "Estimated value", "value": quote.price_label, "inline": True},
            {"name": "Tier", "value": _tier_label(quote.tier), "inline": True},
            {"name": "Score", "value": f"{quote.score:,.0f}", "inline": True},
            {"name": "Checked at", "value": checked, "inline": True},
            {"name": "Latency", "value": f"{verdict.latency_ms:.0f} ms", "inline": True},
            {"name": "Claim", "value": "discord.com/new", "inline": True},
        ],
        "footer": {"text": f"{BRAND} · {EMBED_FOOTER}"},
        "timestamp": verdict.checked_at.astimezone(UTC).isoformat(),
    }


def build_throttle_embed(retry_after: float, rate_rps: float) -> EmbedDict:
    """Shown when Discord hands back a long penalty."""
    minutes = retry_after / 60
    return {
        "title": "⏳ Throttled by Discord",
        "description": (
            f"The public endpoint asked us to wait **{minutes:.0f} min**.\n"
            "The scan is still running — it will resume on its own."
        ),
        "color": 0xF39C12,
        "fields": [
            {"name": "Retry after", "value": f"{retry_after:,.0f}s", "inline": True},
            {"name": "New rate", "value": f"{rate_rps:.3f} req/s", "inline": True},
        ],
        "footer": {"text": f"{BRAND} · this is normal, not an error"},
    }


def build_progress_embed(
    stats: ScanStats,
    *,
    free_names: Sequence[str] = (),
    rate_rps: float | None = None,
    throttles: int = 0,
    remaining: int | None = None,
) -> EmbedDict:
    """Periodic heartbeat.

    A scan runs for days against a throttle that lets through a handful of
    requests at a time. With no heartbeat the channel is silent for hours,
    which is indistinguishable from a broken install — so the radar says what
    it is doing even when it has found nothing at all.
    """
    fields: list[dict[str, Any]] = [
        {"name": "Checked", "value": f"{stats.checked:,}", "inline": True},
        {"name": "Taken", "value": f"{stats.taken:,}", "inline": True},
        {"name": "Free", "value": f"{stats.found:,}", "inline": True},
        {"name": "Skipped", "value": f"{stats.skipped:,}", "inline": True},
        {"name": "Errors", "value": f"{stats.errors:,}", "inline": True},
        {"name": "Alerts", "value": f"{stats.alerts_sent:,}", "inline": True},
        {"name": "Elapsed", "value": f"{stats.elapsed_s / 3600:.1f} h", "inline": True},
    ]
    if rate_rps is not None:
        fields.append({"name": "Rate", "value": f"{rate_rps:.4f} req/s", "inline": True})
    if throttles:
        fields.append({"name": "Throttles", "value": f"{throttles:,}", "inline": True})
    if rate_rps and rate_rps > 0:
        # The single most useful field on this card. One check every two
        # minutes looks exactly like a hang without it.
        fields.append({"name": "Next check in", "value": _duration(1 / rate_rps), "inline": True})
    if remaining and rate_rps and rate_rps > 0:
        fields.append(
            {
                "name": "Estimated left",
                "value": f"~{_duration(remaining / rate_rps)} ({remaining:,} names)",
                "inline": True,
            }
        )

    description = None
    if free_names:
        shown = list(free_names)[:10]
        listed = "\n".join(f"• `{n}`" for n in shown)
        extra = "" if len(free_names) <= 10 else f"\n…and {len(free_names) - 10} more"
        description = f"**Free so far**\n{listed}{extra}"
    elif stats.found == 0 and stats.checked:
        description = "Nothing free yet. Expected — most short names are taken."

    embed: EmbedDict = {
        "title": "📡 Still searching",
        "description": description or "Checking names one at a time.",
        "color": 0x95A5A6,
        "fields": fields,
        "footer": {"text": f"{BRAND} · slow by necessity, not by choice"},
    }
    return embed


def build_result_embed(
    name: str, available: bool, price_label: str = "", tier: str = ""
) -> EmbedDict:
    """One message per check, for the ``--report-every`` mode."""
    if available:
        suffix = f" — {price_label} ({tier})" if price_label else ""
        return {
            "title": f"🟢 `{name}` is free{suffix}",
            "description": "Available to claim right now.",
            "color": 0x3BA55C,
            "footer": {"text": BRAND},
        }
    return {
        "title": f"⚪ `{name}` is taken",
        "description": "Already claimed, moving on.",
        "color": 0x9BA5B4,
        "footer": {"text": BRAND},
    }


def build_scan_started_embed(source: str, candidates: object, rps: float) -> EmbedDict:
    return {
        "title": "📡 Radar online",
        "description": f"Scanning `{source}` for free usernames.",
        "color": 0x5865F2,
        "fields": [
            {
                "name": "Candidates",
                "value": f"{candidates:,}" if isinstance(candidates, int) else "∞",
                "inline": True,
            },
            {"name": "Rate ceiling", "value": f"{rps:.2f} req/s", "inline": True},
        ],
        "footer": {"text": f"{BRAND}"},
    }


def build_scan_report_embed(stats: ScanStats, stopped_early: bool = False) -> EmbedDict:
    reason = " (stopped early)" if stopped_early else ""
    return {
        "title": f"📊 Scan complete{reason}",
        "color": 0x95A5A6,
        "fields": [
            {"name": "Checked", "value": f"{stats.checked:,}", "inline": True},
            {"name": "Taken", "value": f"{stats.taken:,}", "inline": True},
            {"name": "Free", "value": f"{stats.found:,}", "inline": True},
            {"name": "Skipped", "value": f"{stats.skipped:,}", "inline": True},
            {"name": "Errors", "value": f"{stats.errors:,}", "inline": True},
            {"name": "Alerts sent", "value": f"{stats.alerts_sent:,}", "inline": True},
            {
                "name": "Observed rate",
                "value": f"{stats.rps:.4f} req/s over {stats.elapsed_s / 60:.1f} min",
                "inline": False,
            },
        ],
        "footer": {"text": f"{BRAND}"},
    }
