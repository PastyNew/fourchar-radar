"""Embed rendering and webhook token redaction."""

from __future__ import annotations

from fourchar_radar.models import Finding, Quote, ScanStats, Tier, Verdict, utcnow
from fourchar_radar.notify.embed import (
    build_finding_embed,
    build_scan_report_embed,
    build_scan_started_embed,
    build_throttle_embed,
)
from fourchar_radar.notify.webhook import redact_webhook


def test_finding_embed_shape(make_finding) -> None:  # type: ignore[no-untyped-def]
    embed = build_finding_embed(make_finding("cool", price=5000))

    assert "cool" in embed["title"]
    assert embed["description"]
    assert embed["color"] == 0x5865F2  # RARE colour
    assert {f["name"] for f in embed["fields"]} >= {"Estimated value", "Tier", "Claim"}
    assert embed["timestamp"]


def test_finding_embed_colour_matches_tier() -> None:
    from fourchar_radar.models import TIER_COLORS

    for tier in Tier:
        verdict = Verdict(name="ab", available=True, checked_at=utcnow())
        quote = Quote(name="ab", price_usd=1, score=1.0, tier=tier, reasons=("x",))
        assert build_finding_embed(Finding(verdict, quote))["color"] == TIER_COLORS[tier]


def test_taken_finding_still_renders(make_finding) -> None:  # type: ignore[no-untyped-def]
    embed = build_finding_embed(make_finding("taken", available=False))
    assert embed["title"]


def test_throttle_embed_states_the_wait() -> None:
    embed = build_throttle_embed(2100.0, 0.25)
    assert "35 min" in embed["description"]
    assert "2,100" in str(embed["fields"])


def test_started_and_report_embeds() -> None:
    started = build_scan_started_embed("wordlist:words4.txt(4)", 2418, 0.5)
    assert "2,418" in str(started["fields"])

    stats = ScanStats(checked=10, taken=6, found=3, errors=1, alerts_sent=2)
    report = build_scan_report_embed(stats, stopped_early=True)
    assert "stopped early" in report["title"]
    assert "2" in str(report["fields"])


def test_redact_webhook_hides_the_token() -> None:
    url = "https://discord.com/api/webhooks/123456789/super-secret-token"
    redacted = redact_webhook(url)

    assert "super-secret-token" not in redacted
    assert "123456789" not in redacted  # both the id and the token are hidden
    assert redacted == "https://discord.com/api/webhooks/***"
    assert url != redacted


def test_redact_webhook_handles_missing_url() -> None:
    assert redact_webhook("") == "(unset)"


def test_redact_keeps_discordapp_hosts_readable() -> None:
    redacted = redact_webhook("https://discordapp.com/api/webhooks/42/tok")
    assert "tok" not in redacted
