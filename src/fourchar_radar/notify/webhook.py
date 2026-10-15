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
            if response.status_code == 429:
                # Respect the server and retry once, later.
                retry_after = 1.0
                with contextlib.suppress(ValueError):
                    retry_after = float(response.json().get("retry_after", 1.0))
                await asyncio.sleep(min(retry_after, 30.0))
                await self._post(payload)
                return

            safe_url = redact_webhook(self.settings.discord_webhook_url)
            if response.status_code in (401, 403, 404) or "Unknown Webhook" in detail:
                # Almost always a stale or mistyped URL rather than a Discord
                # outage, and a bad URL means *every* alert will fail.
                raise InvalidWebhook(
                    f"Discord rejected the webhook: {detail}\n"
                    f"  url: {safe_url}\n"
                    "  The URL looks wrong or the webhook was deleted. Recreate it:\n"
                    "  channel -> Edit Channel -> Integrations -> Webhooks -> "
                    "New Webhook -> Copy Webhook URL,\n"
                    "  then paste it into DISCORD_WEBHOOK_URL in your .env.\n"
                    "  To scan without alerting, run with --dry-run."
                )
            raise RuntimeError(
                f"webhook returned HTTP {response.status_code}: {detail} (url={safe_url})"
            )

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
