"""Optional Discord bot: drive the radar from inside Discord.

This whole module is behind the ``bot`` extra. Importing it without
``discord.py`` installed raises a clear error rather than a stack trace at
import time.

Commands:

    /price <name>   check one name and value it
    /top            best free names we already know about
    /status         live scan + limiter state
    /scan start     begin (or resume) a scan
    /scan stop      stop the running scan

The background scan reuses exactly the same checker, limiter, store and pricer
as the CLI — the bot is a control surface, not a second implementation.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..checking.client import DiscordAvailabilityClient
from ..checking.ratelimit import AdaptiveRateLimiter
from ..checking.scanner import Scanner, ScanReport
from ..config import Settings
from ..models import Finding, Tier
from ..pricing.estimator import estimate
from ..sources import build_source
from .embed import TIER_EMOJI, EmbedDict, build_finding_embed

log = logging.getLogger(__name__)

if TYPE_CHECKING:  # pragma: no cover
    import discord

try:
    import discord
    from discord import app_commands
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "bot mode needs discord.py — install with: pip install 'fourchar-radar[bot]'"
    ) from exc


def _embed_dict_to_discord(data: EmbedDict) -> discord.Embed:
    """Reuse the webhook embed builders inside the bot."""
    stamp = data.get("timestamp")
    embed = discord.Embed(
        title=data.get("title"),
        description=data.get("description"),
        color=data.get("color", 0x5865F2),
        timestamp=datetime.fromisoformat(stamp) if stamp else None,
    )
    for field in data.get("fields", []):
        embed.add_field(
            name=field["name"],
            value=str(field["value"]),
            inline=bool(field.get("inline", False)),
        )
    if footer := data.get("footer", {}).get("text"):
        embed.set_footer(text=footer)
    return embed


class RadarBot:
    """Owns the Discord client, the scan task, and the shared services."""

    def __init__(self, settings: Settings, store: Any) -> None:
        self.settings = settings
        self.store = store

        self.limiter = AdaptiveRateLimiter(
            settings.rate_limit_rps,
            on_throttle=lambda seconds: self._spawn_throttle_notice(seconds),
        )
        self.client = DiscordAvailabilityClient(settings, self.limiter)
        self.scanner = Scanner(settings, store, self.client, self.limiter, self)
        self.scan_report: ScanReport | None = None

        self._scan_task: asyncio.Task[None] | None = None
        self._discord = discord.Client(intents=discord.Intents.none())
        self._tree = app_commands.CommandTree(self._discord)
        self._stop = asyncio.Event()
        self._discord.event(self._on_ready)
        self._register_commands()

    # --------------------------------------------------------- lifecycle
    async def start(self) -> None:
        await self._discord.start(self.settings.discord_bot_token)

    async def close(self) -> None:
        self.scanner.stop()
        if self._scan_task is not None:
            self._scan_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._scan_task
        await self.client.aclose()
        if not self._discord.is_closed():
            await self._discord.close()

    async def _on_ready(self) -> None:
        log.info("bot connected as %s", self._discord.user)
        with contextlib.suppress(Exception):
            await self._tree.sync()
        await self._resolve_channel()  # fail fast if we cannot deliver

    async def _resolve_channel(self) -> discord.abc.Messageable | None:
        """Where alerts go: configured channel, else the first available one."""
        if self._discord.is_closed():
            return None
        if self.settings.discord_command_channel_id:
            channel = self._discord.get_channel(self.settings.discord_command_channel_id)
            if channel is not None:
                return channel
            log.warning(
                "configured alert channel %s not found",
                self.settings.discord_command_channel_id,
            )
        for guild in self._discord.guilds:
            for channel in guild.text_channels:
                if channel.permissions_for(guild.me).send_messages:
                    log.info("using fallback channel #%s", channel)
                    return channel
        log.warning("no writable text channel found; alerts will fail")
        return None

    def _spawn_throttle_notice(self, seconds: float) -> None:
        """Fire-and-forget throttle notice; the limiter callback is sync."""
        asyncio.create_task(self._announce_throttle(seconds))

    # ------------------------------------------------------- Notifier API
    async def notify(self, finding: Finding) -> None:
        """``Notifier`` protocol implementation."""
        channel = await self._resolve_channel()
        if channel is None:
            raise RuntimeError("no channel available to post alerts to")
        await channel.send(embed=_embed_dict_to_discord(build_finding_embed(finding)))

    async def _announce_throttle(self, retry_after: float) -> None:
        with contextlib.suppress(Exception):
            channel = await self._resolve_channel()
            if channel is not None:
                await channel.send(
                    f"⏳ Throttled — pausing {retry_after / 60:.0f} min. Still running."
                )

    # ------------------------------------------------------------ scanning
    @property
    def scanning(self) -> bool:
        return self._scan_task is not None and not self._scan_task.done()

    async def start_scan(self) -> None:
        if self.scanning:
            raise RuntimeError("a scan is already running")
        self._scan_task = asyncio.create_task(self._run_scan(), name="radar-scan")

    async def stop_scan(self) -> None:
        if not self.scanning:
            return
        self.scanner.stop()
        self._scan_task = None  # the task finishes on its own

    async def _run_scan(self) -> None:
        try:
            source = build_source(self.settings)
            self.scan_report = await self.scanner.run(source)
            log.info("scan finished: %s", self.scanner.stats.summary())
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("scan crashed")

    # ----------------------------------------------------------- commands
    def _register_commands(self) -> None:
        tree = self._tree

        @tree.command(name="price", description="Check one username and value it")
        @app_commands.describe(name="the username to check")
        async def price(interaction: discord.Interaction, name: str) -> None:
            await interaction.response.defer(ephemeral=True)
            try:
                verdict = await self.client.check(name.strip().lower())
            except Exception as exc:  # noqa: BLE001
                await interaction.followup.send(f"Could not check `{name}`: {exc}", ephemeral=True)
                return

            if verdict.available:
                quote = estimate(verdict.name)
                self.store.record(verdict, quote)
                await interaction.followup.send(
                    embed=_embed_dict_to_discord(build_finding_embed(Finding(verdict, quote))),
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    f"`{verdict.name}` is already taken.", ephemeral=True
                )

        @tree.command(name="top", description="Best free usernames we already know about")
        @app_commands.describe(tier="minimum tier", limit="how many to show")
        @app_commands.choices(tier=[app_commands.Choice(name=t.value, value=t.value) for t in Tier])
        async def top(
            interaction: discord.Interaction,
            tier: app_commands.Choice[str] | None = None,
            limit: int = 10,
        ) -> None:
            await interaction.response.defer(ephemeral=True)
            min_tier = Tier(tier.value) if tier else Tier.COMMON
            rows = self.store.findings(limit=min(limit, 25), min_tier=min_tier)
            if not rows:
                await interaction.followup.send(
                    "Nothing free yet — run `/scan start`.", ephemeral=True
                )
                return
            listing = "\n".join(
                f"{TIER_EMOJI[f.tier]} `{f.name}` — **{f.quote.price_label}** ({f.tier.value})"
                for f in rows
            )
            await interaction.followup.send(f"**Top free usernames**\n{listing}", ephemeral=True)

        @tree.command(name="status", description="Live scan and rate-limiter state")
        async def status(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True)
            snap = self.limiter.snapshot()
            stats = self.scanner.stats
            summary = self.store.summary()
            blocked = f"{snap.blocked_for_s:.0f}s"
            text = (
                f"**Scanning:** {'yes' if self.scanning else 'no'}\n"
                f"**Checked:** {stats.checked:,} · **free:** {stats.found:,} · "
                f"**alerts:** {stats.alerts_sent:,}\n"
                f"**Rate:** {snap.rate_rps:.4f} r/s "
                f"(ceiling {snap.ceiling_rps:.2f})\n"
                f"**Throttles:** {snap.throttles} · **blocked for:** {blocked}\n"
                f"**DB:** {summary['total']:,} checked · {summary['free']:,} free"
            )
            await interaction.followup.send(text, ephemeral=True)

        scan = app_commands.Group(name="scan", description="Control the background scan")

        @scan.command(name="start", description="Start or resume scanning")
        @app_commands.describe(
            source="where to pull candidates from",
            length="username length",
            rate="requests per second ceiling",
        )
        @app_commands.choices(
            source=[
                app_commands.Choice(name=v, value=v)
                for v in ("wordlist", "bruteforce", "file", "stdin")
            ]
        )
        async def scan_start(
            interaction: discord.Interaction,
            source: app_commands.Choice[str] | None = None,
            length: int = 4,
            rate: float = 0.5,
        ) -> None:
            if self.scanning:
                await interaction.response.send_message(
                    "A scan is already running.", ephemeral=True
                )
                return
            if source:
                self.settings.source = source.value
            self.settings.length = max(2, min(length, 32))
            self.settings.rate_limit_rps = max(0.01, min(rate, 20.0))
            self.settings.concurrency = 1

            # Rebuild the limiter so the new ceiling takes effect immediately.
            self.limiter = AdaptiveRateLimiter(self.settings.rate_limit_rps)
            self.client.limiter = self.limiter
            self.scanner = Scanner(self.settings, self.store, self.client, self.limiter, self)

            await self.start_scan()
            await interaction.response.send_message(
                f"📡 Scanning `{self.settings.source}` "
                f"(length {self.settings.length}, ≤{self.settings.rate_limit_rps} r/s).",
                ephemeral=True,
            )

        @scan.command(name="stop", description="Stop the running scan")
        async def scan_stop(interaction: discord.Interaction) -> None:
            if not self.scanning:
                await interaction.response.send_message("Nothing is running.", ephemeral=True)
                return
            await self.stop_scan()
            await interaction.response.send_message(
                "🛑 Stopping after the current check.", ephemeral=True
            )

        tree.add_command(scan)
