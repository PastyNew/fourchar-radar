"""Webhook delivery — the zero-setup path.

Paste a webhook URL and the radar posts findings to your channel. No bot
account, no OAuth dance, no token to leak beyond the webhook itself (which is
why every log line here redacts it).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

import httpx

from ..config import Settings
from ..errors import InvalidWebhook
from ..models import Finding, ScanStats
from .embed import (
    EmbedDict,
    build_finding_embed,
    build_scan_report_embed,
    build_scan_started_embed,
    build_throttle_embed,
)

log = logging.getLogger(__name__)

#: Discord allows ~5 posts per 2s per webhook; stay well under it.
MIN_SEND_INTERVAL_S = 0.6


def redact_webhook(url: str) -> str:
    """Strip the token so URLs are safe to log."""
    if not url:
        return "(unset)"
    head, _, _ = url.rpartition("/")
    parent = head.rsplit("/", 1)[0]
    return f"{parent}/***"


class WebhookNotifier:
    """Posts embeds to a Discord webhook, one at a time, politely."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not settings.discord_webhook_url:
            raise ValueError("WebhookNotifier requires DISCORD_WEBHOOK_URL")

        self.settings = settings
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(15.0),
            headers={"User-Agent": settings.user_agent},
        )
        self._lock = asyncio.Lock()
        self._next_send_at = 0.0

    # ---------------------------------------------------------- delivery
    async def _post(self, payload: EmbedDict) -> None:
        """Send one payload, honouring webhook rate limits."""
        async with self._lock:
            delay = self._next_send_at - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                response = await self._client.post(self.settings.discord_webhook_url, json=payload)
            finally:
                self._next_send_at = time.monotonic() + MIN_SEND_INTERVAL_S

        if response.status_code >= 400:
            detail = response.text[:200]
            safe_url = redact_webhook(self.settings.discord_webhook_url)

            if response.status_code == 429:
                # Respect the server and retry once, later.
                retry_after = 1.0
                with contextlib.suppress(ValueError):
                    retry_after = float(response.json().get("retry_after", 1.0))
                await asyncio.sleep(min(retry_after, 30.0))
                await self._post(payload)
                return

            # Every failure here is about the URL, not the payload: a bad
            # webhook means *every* alert would fail, so it has to stop the
            # scan before the first hour-long one is wasted on it.
            raise InvalidWebhook(self._explain_failure(response.status_code, detail, safe_url))

    @staticmethod
    def _explain_failure(status: int, detail: str, safe_url: str) -> str:
        """Turn a webhook rejection into something the user can act on."""
        lines = [f"Discord rejected the webhook: {detail}", f"  url: {safe_url}"]

        if "not snowflake" in detail.lower():
            # The README example is literally ".../ID/TOKEN", and copying it
            # verbatim is by far the most likely mistake.
            lines += [
                "",
                "  That ID is not a real webhook id — this looks like the example URL",
                "  from the README was copied as-is. Replace ID/TOKEN with the values",
                "  from your own webhook.",
            ]
        elif status in (401, 403, 404) or "Unknown Webhook" in detail:
            lines += [
                "",
                "  The webhook was deleted, or the URL is from another server/channel.",
            ]
        else:
            lines += ["", "  The request was rejected; the details above come from Discord."]

        lines += [
            "",
            "  Get a working URL: channel -> Edit Channel -> Integrations ->",
            "  Webhooks -> New Webhook -> Copy Webhook URL.",
            "  To scan without alerting, run with --dry-run.",
        ]
        return "\n".join(lines)

    def _envelope(self, embed: EmbedDict) -> EmbedDict:
        """Wrap an embed in the webhook payload envelope.

        ``allowed_mentions.parse: []`` is deliberate: a finding must never be
        able to ping @here or @everyone, regardless of its content.
        """
        payload: EmbedDict = {
            "username": "fourchar-radar",
            "allowed_mentions": {"parse": []},  # never ping @here/@everyone
            "embeds": [embed],
        }
        if self.settings.alert_embeds:
            payload["embeds"] = [embed]
        return payload

    # ------------------------------------------------------------- public
    async def verify(self) -> None:
        """Confirm the webhook is alive before a long scan begins.

        Sends a real (small) message rather than a HEAD request, because
        Discord answers an unrecognised URL the same way for both, and a probe
        that actually delivers is the only proof that matters. Returns quietly
        on success.
        """
        log.info("verifying webhook...")
        await self._post(
            self._envelope(
                {
                    "title": "fourchar-radar",
                    "description": "Webhook connected. Radar is starting up.",
                    "color": 0x5865F2,
                    "footer": {"text": "remove this test message at your leisure"},
                }
            )
        )
        log.info("webhook ok")

    async def notify(self, finding: Finding) -> None:
        log.info(
            "posting alert",
            extra={
                "candidate": finding.name,
                "price": finding.quote.price_label,
                "tier": str(finding.tier),
            },
        )
        await self._post(self._envelope(build_finding_embed(finding)))

    async def notify_throttle(self, retry_after: float, rate_rps: float) -> None:
        await self._post(self._envelope(build_throttle_embed(retry_after, rate_rps)))

    async def notify_started(self, source: str, candidates: object) -> None:
        await self._post(
            self._envelope(
                build_scan_started_embed(source, candidates, self.settings.rate_limit_rps)
            )
        )

    async def notify_report(self, stats: ScanStats, stopped_early: bool = False) -> None:
        await self._post(self._envelope(build_scan_report_embed(stats, stopped_early)))

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
