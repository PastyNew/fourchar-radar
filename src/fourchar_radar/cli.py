"""Command-line interface.

Subcommands
-----------
``run``      the main event: scan and alert
``price``    check specific names right now
``top``      best free names recorded so far
``status``   database summary
``sources``  what can be scanned, and how big each source is
``export``   dump findings to CSV/JSON
``prune``    housekeeping
``bot``      run in bot mode with slash commands

Every flag is an override on top of environment/`.env` values, so the same
config can be expressed either way.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import json
import logging
import signal
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from . import __version__
from .checking.client import DiscordAvailabilityClient
from .checking.ratelimit import AdaptiveRateLimiter
from .checking.scanner import Scanner
from .config import Settings
from .errors import RadarError
from .logging_setup import setup_logging
from .models import TIER_ORDER, Tier
from .notify.base import NullNotifier
from .notify.webhook import WebhookNotifier, redact_webhook
from .platforms import supports_signal_handlers
from .pricing.estimator import estimate
from .sources import build_source
from .storage import Store

log = logging.getLogger("fourchar_radar")

TIER_ORDER_LIST = list(TIER_ORDER)


# ------------------------------------------------------------------ parser
# ------------------------------------------------------------------ parser
def _common_parser() -> argparse.ArgumentParser:
    """Global flags, accepted either before or after the subcommand.

    ``SUPPRESS`` keeps an unspecified flag from clobbering the value parsed at
    the other level (argparse shares one namespace between the two).
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--db", metavar="PATH", default=argparse.SUPPRESS, help="override RADAR_DB_PATH"
    )
    common.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=argparse.SUPPRESS,
        help="override",
    )
    common.add_argument(
        "--log-json", action="store_true", default=argparse.SUPPRESS, help="JSON log lines"
    )
    return common


def build_parser() -> argparse.ArgumentParser:
    common = _common_parser()
    parser = argparse.ArgumentParser(
        prog="fourchar-radar",
        parents=[common],
        description="Find free 4-character Discord usernames and get told about them.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  fourchar-radar run                          # scan bundled words, alert to webhook\n"
            "  fourchar-radar run --source bruteforce      # sweep every 4-char combination\n"
            "  fourchar-radar run --dry-run --limit 50      # no alerts, just try it\n"
            "  fourchar-radar price cool code\n"
            "  fourchar-radar top --tier rare --limit 20\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    sub = parser.add_subparsers(dest="command", required=True)

    # -- run -------------------------------------------------------------
    run = sub.add_parser("run", help="scan and alert", parents=[common])
    run.add_argument(
        "--source", choices=["wordlist", "bruteforce", "file", "stdin"], help="override"
    )
    run.add_argument("--length", type=int, help="username length (default 4)")
    run.add_argument("--names-file", help="wordlist path for --source file")
    run.add_argument("--limit", type=int, help="stop after N new checks (0 = no limit)")
    run.add_argument("--rps", type=float, help="request-rate ceiling (the limiter may sit lower)")
    run.add_argument("--concurrency", type=int, help="parallel workers (keep at 1-2)")
    run.add_argument("--webhook", help="override DISCORD_WEBHOOK_URL")
    run.add_argument("--min-tier", choices=TIER_ORDER_LIST, help="only alert at or above this tier")
    run.add_argument("--min-price", type=int, help="only alert at or above this USD")
    run.add_argument("--cooldown", type=float, help="minimum seconds between alerts")
    run.add_argument("--dry-run", action="store_true", help="scan but never post")
    run.add_argument(
        "--watch",
        action="store_true",
        help="keep re-streaming the source after it is exhausted",
    )
    run.add_argument(
        "--watch-interval",
        type=float,
        default=900.0,
        help="seconds between watch passes (default 900)",
    )
    run.add_argument(
        "webhook_url",
        nargs="?",
        metavar="WEBHOOK",
        help=(
            "Discord webhook URL, pasted straight in. This is the short way: "
            "'fourchar-radar run <url>' needs no config file at all."
        ),
    )
    run.set_defaults(func=cmd_run)

    # -- price -----------------------------------------------------------
    price = sub.add_parser("price", help="check specific names now", parents=[common])
    price.add_argument("names", nargs="+", help="one or more usernames")
    price.add_argument("--rps", type=float, default=0.5, help="request-rate ceiling")
    price.add_argument("--json", action="store_true", help="machine-readable output")
    price.set_defaults(func=cmd_price)

    # -- top -------------------------------------------------------------
    top = sub.add_parser("top", help="best free names recorded so far", parents=[common])
    top.add_argument("--limit", type=int, default=25)
    top.add_argument("--tier", choices=TIER_ORDER_LIST, default="common")
    top.add_argument("--json", action="store_true")
    top.set_defaults(func=cmd_top)

    # -- status ----------------------------------------------------------
    status = sub.add_parser("status", help="database summary", parents=[common])
    status.set_defaults(func=cmd_status)

    # -- sources ---------------------------------------------------------
    sources = sub.add_parser("sources", help="what can be scanned", parents=[common])
    sources.set_defaults(func=cmd_sources)

    # -- export ----------------------------------------------------------
    export = sub.add_parser("export", help="dump findings", parents=[common])
    export.add_argument("--format", choices=["csv", "json"], default="csv")
    export.add_argument("--out", default="-", help="output path, or - for stdout")
    export.add_argument("--tier", choices=TIER_ORDER_LIST, default="common")
    export.set_defaults(func=cmd_export)

    # -- prune -----------------------------------------------------------
    prune = sub.add_parser("prune", help="housekeeping", parents=[common])
    prune.add_argument("--days", type=int, default=30, help="drop taken names older than this")
    prune.add_argument("--yes", action="store_true", help="skip the confirmation")
    prune.set_defaults(func=cmd_prune)

    # -- bot -------------------------------------------------------------
    bot = sub.add_parser(
        "bot", help="run with slash commands (needs the [bot] extra)", parents=[common]
    )
    bot.add_argument("--source", choices=["wordlist", "bruteforce", "file", "stdin"])
    bot.add_argument("--length", type=int)
    bot.add_argument("--rps", type=float)
    bot.set_defaults(func=cmd_bot)

    # -- tui -------------------------------------------------------------
    sub.add_parser("tui", help="interactive menu in the terminal", parents=[common]).set_defaults(
        func=cmd_tui
    )

    return parser


# ----------------------------------------------------------------- helpers
def _apply_overrides(args: argparse.Namespace, settings: Settings) -> Settings:
    """Rebuild settings with CLI flags layered on top of env/`.env`.

    Precedence, lowest to highest: ``.env`` / environment, then flags, then the
    positional webhook URL. The positional form exists so that the common case
    needs no config file at all: ``fourchar-radar run <url>``.
    """
    overrides: dict[str, Any] = {}

    simple = {
        "db": "db_path",
        "log_level": "log_level",
        "source": "source",
        "length": "length",
        "names_file": "names_file",
        "limit": "limit",
        "rps": "rate_limit_rps",
        "concurrency": "concurrency",
        "webhook": "discord_webhook_url",
        "min_tier": "alert_min_tier",
        "cooldown": "alert_cooldown_s",
    }
    for attr, field in simple.items():
        value = getattr(args, attr, None)
        if value is not None:
            overrides[field] = value

    # Positional webhook wins over --webhook: it is the more explicit gesture.
    positional = getattr(args, "webhook_url", None)
    if positional:
        overrides["discord_webhook_url"] = positional.strip()

    if getattr(args, "min_price", None):
        overrides["alert_min_tier"] = _tier_for_price(int(args.min_price))
    if getattr(args, "log_json", False):
        overrides["log_json"] = True
    if getattr(args, "dry_run", False):
        overrides["alert_webhook"] = False
        overrides["discord_webhook_url"] = ""

    if not overrides:
        return settings
    return Settings.model_validate({**settings.model_dump(), **overrides})


def _tier_for_price(price: int) -> Tier:
    """Cheapest tier that still admits names worth at least *price*."""
    for tier in reversed(list(Tier)):
        floor = {"common": 0, "uncommon": 150, "rare": 1_000, "epic": 10_000, "legendary": 50_000}
        if price >= floor[tier.value]:
            return tier
    return Tier.COMMON


def _open_store(args: argparse.Namespace, settings: Settings) -> Store:
    return Store(getattr(args, "db", None) or settings.db_path)


# ------------------------------------------------------------------- run
def cmd_run(args: argparse.Namespace, settings: Settings) -> int:
    settings = _apply_overrides(args, settings)
    try:
        return asyncio.run(_run_async(args, settings))
    except KeyboardInterrupt:
        # Only reached on platforms without signal handlers (Windows). The run
        # has already been cancelled and its resources closed by the `finally`
        # blocks inside _run_async; there is simply no report left to print.
        log.info("interrupted")
        print("\nstopped. findings recorded so far are kept — see `fourchar-radar top`")
        return 130


async def _run_async(args: argparse.Namespace, settings: Settings) -> int:
    if settings.alert_webhook:
        # Raises ConfigError with actionable text if no URL was provided.
        settings.require_webhook()
    source = build_source(settings)

    notifier: Any = NullNotifier()
    if settings.webhook_enabled:
        notifier = WebhookNotifier(settings)
        log.info("alerts -> webhook %s", redact_webhook(settings.discord_webhook_url))
        # Fail fast, before spending hours of scan time. A rejected webhook URL
        # would otherwise only surface when the first real find arrives.
        await notifier.verify()
    else:
        log.warning("alerts disabled — findings will be written to the database only")

    limiter = AdaptiveRateLimiter(settings.rate_limit_rps)
    store = _open_store(args, settings)

    try:
        async with DiscordAvailabilityClient(settings, limiter) as client:
            scanner = Scanner(settings, store, client, limiter, notifier)
            _install_signal_handlers(scanner)

            if not isinstance(notifier, NullNotifier):
                await notifier.notify_started(source.label, source.total)

            if args.watch:
                await _watch(scanner, source, args.watch_interval, notifier, settings)
            else:
                try:
                    report = await scanner.run(source)
                except KeyboardInterrupt:
                    # Windows path: translate the interrupt into the same clean
                    # stop that a signal handler would produce elsewhere, so the
                    # summary is still printed and the database still closed.
                    scanner.stop()
                    raise
                if not isinstance(notifier, NullNotifier):
                    await notifier.notify_report(report.stats, report.stopped_early)
                _print_report(report, scanner)
    finally:
        store.close()
        await notifier.close()

    return 0


async def _watch(
    scanner: Scanner,
    source: Any,
    interval: float,
    notifier: Any,
    settings: Settings,
) -> None:
    """Re-stream the source forever, picking up names not yet recorded."""
    log.info("watch mode active; pass every %.0f s (ctrl-c to stop)", interval)
    while True:
        await scanner.run(source)
        if scanner.stopping:
            break
        log.info("pass complete; sleeping %.0f s", interval)
        # Interruptible, so ctrl-c does not have to wait out the full interval.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(scanner.wait_for_stop(), timeout=interval)


def _install_signal_handlers(scanner: Scanner) -> None:
    """Stop the scan cleanly on Ctrl-C, and on SIGTERM where it exists.

    Windows has no ``loop.add_signal_handler`` and no SIGTERM; there, Ctrl-C
    surfaces as ``KeyboardInterrupt``, which ``cmd_run`` already handles by
    cancelling the run and printing the summary. Registering the signal
    handlers is therefore a no-op there, and that is correct rather than
    broken — the fallback path is what actually runs.
    """
    if not supports_signal_handlers():
        return

    # Only meaningful inside a running loop; calling this outside one would
    # raise RuntimeError, which is a programming error worth surfacing.
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # NotImplementedError/AttributeError: platforms without add_signal_handler.
        # ValueError: signal not valid on this platform (e.g. SIGTERM on Windows).
        with contextlib.suppress(NotImplementedError, AttributeError, ValueError):
            loop.add_signal_handler(sig, scanner.stop)


def _print_report(report: Any, scanner: Scanner) -> None:
    stats = report.stats
    print()
    print("── scan summary " + "─" * 44)
    print(f"  checked      {stats.checked:,}")
    print(f"  taken        {stats.taken:,}")
    print(f"  free found   {stats.found:,}")
    print(f"  skipped      {stats.skipped:,}   (already known or invalid)")
    print(f"  errors       {stats.errors:,}")
    print(f"  alerts sent  {stats.alerts_sent:,}")
    print(f"  elapsed      {stats.elapsed_s / 60:.1f} min")
    print(f"  final rate   {report.limiter.rate_rps:.4f} req/s")
    print(f"  throttles    {report.limiter.throttles}")
    if scanner.store.counts_by_tier():
        print(
            "  free by tier "
            + ", ".join(
                f"{tier}={count}" for tier, count in sorted(scanner.store.counts_by_tier().items())
            )
        )
    print("─" * 62)


# ----------------------------------------------------------------- price
def cmd_price(args: argparse.Namespace, settings: Settings) -> int:
    settings = _apply_overrides(args, settings)
    return asyncio.run(_price_async(args, settings))


async def _price_async(args: argparse.Namespace, settings: Settings) -> int:
    from .names import normalize

    names = [normalize(n) for n in args.names]
    names = [n for n in names if n]
    if not names:
        print("no syntactically valid usernames given", file=sys.stderr)
        return 2

    limiter = AdaptiveRateLimiter(args.rps)
    results: list[dict[str, Any]] = []

    async with DiscordAvailabilityClient(settings, limiter) as client:
        for name in names:
            try:
                verdict = await client.check(name)
            except RadarError as exc:
                results.append({"name": name, "error": str(exc)})
                continue

            if not verdict.available:
                results.append({"name": name, "available": False})
                continue

            quote = estimate(name)
            results.append(
                {
                    "name": name,
                    "available": True,
                    "price_usd": quote.price_usd,
                    "tier": str(quote.tier),
                    "reasons": list(quote.reasons),
                }
            )

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        for row in results:
            if "error" in row:
                print(f"  {row['name']:<20} error: {row['error']}")
            elif not row["available"]:
                print(f"  {row['name']:<20} taken")
            else:
                price = f"${row['price_usd']:,}"
                print(f"  {row['name']:<20} FREE  ~{price:<12} [{row['tier']}]")
    return 0


# ------------------------------------------------------------------- top
def cmd_top(args: argparse.Namespace, settings: Settings) -> int:
    store = _open_store(args, settings)
    try:
        rows = store.findings(limit=args.limit, min_tier=Tier(args.tier))
    finally:
        store.close()

    if args.json:
        print(
            json.dumps(
                [
                    {
                        "name": f.name,
                        "price_usd": f.quote.price_usd,
                        "tier": str(f.tier),
                        "reasons": list(f.quote.reasons),
                        "checked_at": f.verdict.checked_at.isoformat(),
                    }
                    for f in rows
                ],
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    if not rows:
        print("nothing recorded yet — try: fourchar-radar run")
        return 0

    print(f"{'NAME':<14}{'EST. VALUE':>12}  {'TIER':<10}NOTED")
    print("─" * 60)
    for f in rows:
        print(
            f"{f.name:<14}{f.quote.price_label:>12}  "
            f"{str(f.tier):<10}{f.verdict.checked_at:%Y-%m-%d}"
        )
    return 0


# ---------------------------------------------------------------- status
def cmd_status(args: argparse.Namespace, settings: Settings) -> int:
    store = _open_store(args, settings)
    try:
        summary = store.summary()
        tiers = store.counts_by_tier()
        last_source = store.get_meta("last_scan", "—")
        best = store.findings(limit=3, min_tier=Tier.COMMON)
    finally:
        store.close()

    print(f"database     {store.path}")
    print(f"last scan    {last_source}")
    print(f"checked      {summary['total']:,}")
    print(f"free         {summary['free']:,}")
    print(f"notified     {summary['notified']:,}")
    if tiers:
        print("by tier      " + ", ".join(f"{k}={v}" for k, v in sorted(tiers.items())))
    if best:
        print("\nbest known free:")
        for f in best:
            print(f"  {f.name:<10} {f.quote.price_label:>10}  {f.tier}")
    return 0


# --------------------------------------------------------------- sources
#: Sustained throughput observed against the public endpoint: a ~16-request
#: burst, then a ~35-minute lockout, repeating. Deliberately pessimistic.
OBSERVED_RPS = 0.0076


def _human_duration(seconds: float) -> str:
    """Render a rough duration, escalating to human units for long horizons."""
    for size, unit in (
        (86400 * 365, "years"),
        (86400 * 30, "months"),
        (86400, "days"),
        (3600, "hours"),
        (60, "min"),
    ):
        if seconds >= size:
            value = seconds / size
            return f"{value:.0f} {unit}" if value >= 10 else f"{value:.1f} {unit}"
    return f"{seconds:.0f}s"


def cmd_sources(args: argparse.Namespace, settings: Settings) -> int:
    from .lexicon import load_wordlist
    from .sources.bruteforce import BruteforceSource

    words = load_wordlist()
    print("available sources:\n")
    print(f"  {'source':<12} {'length':<9} {'candidates':>12} {'est. time':>12}")

    for length in range(2, 8):
        count = sum(1 for w in words if len(w) == length)
        if count:
            eta = _human_duration(count / OBSERVED_RPS)
            print(f"  {'wordlist':<12} {f'length={length}':<9} {count:>12,} {eta:>12}")

    for length in (4, 5):
        count = len(BruteforceSource(length))
        eta = _human_duration(count / OBSERVED_RPS)
        print(f"  {'bruteforce':<12} {f'length={length}':<9} {count:>12,} {eta:>12}")

    print(f"  {'file':<12} {'yours':<9} {'-':>12} {'depends':>12}")
    print(f"  {'stdin':<12} {'yours':<9} {'-':>12} {'depends':>12}")

    print(
        f"\nest. time assumes {OBSERVED_RPS} req/s sustained: the public endpoint\n"
        "allows a short burst, then locks out for ~35 minutes. Scans are resumable,\n"
        "so treat these as 'if left running', not deadlines.\n"
        "bruteforce is realistically a multi-year job from one IP — point several\n"
        "instances at different egress IPs (RADAR_PROXY) if you want it to matter."
    )
    return 0


# ---------------------------------------------------------------- export
def cmd_export(args: argparse.Namespace, settings: Settings) -> int:
    store = _open_store(args, settings)
    try:
        rows = store.findings(limit=100_000, min_tier=Tier(args.tier))
    finally:
        store.close()

    if args.out == "-":
        handle = sys.stdout
        close = False
    else:
        # Opened manually rather than via `with` so stdout can share the branch.
        handle = Path(args.out).open("w", newline="", encoding="utf-8")  # noqa: SIM115
        close = True

    try:
        if args.format == "json":
            json.dump(
                [
                    {
                        "name": f.name,
                        "price_usd": f.quote.price_usd,
                        "tier": str(f.tier),
                        "reasons": list(f.quote.reasons),
                        "checked_at": f.verdict.checked_at.isoformat(),
                    }
                    for f in rows
                ],
                handle,
                indent=2,
                ensure_ascii=False,
            )
            handle.write("\n")
        else:
            writer = csv.writer(handle)
            writer.writerow(["name", "price_usd", "tier", "checked_at", "reasons"])
            for f in rows:
                writer.writerow(
                    [
                        f.name,
                        f.quote.price_usd,
                        str(f.tier),
                        f.verdict.checked_at.isoformat(),
                        "; ".join(f.quote.reasons),
                    ]
                )
    finally:
        if close:
            handle.close()

    if args.out != "-":
        print(f"wrote {len(rows)} rows to {args.out}")
    return 0


# ----------------------------------------------------------------- prune
def cmd_prune(args: argparse.Namespace, settings: Settings) -> int:
    store = _open_store(args, settings)
    try:
        before = store.summary()["total"]
        if not args.yes:
            print(f"database holds {before:,} records.")
            answer = input(f"drop taken names older than {args.days} days? [y/N] ")
            if answer.strip().lower() not in ("y", "yes"):
                print("aborted")
                return 1
        removed = store.prune_taken(args.days)
        after = store.summary()["total"]
    finally:
        store.close()

    print(f"removed {removed:,} records ({before:,} → {after:,})")
    return 0


# ------------------------------------------------------------------- bot
def cmd_bot(args: argparse.Namespace, settings: Settings) -> int:
    settings = _apply_overrides(args, settings)
    if not settings.bot_enabled:
        print(
            "bot mode needs a token: set DISCORD_BOT_TOKEN and install 'fourchar-radar[bot]'",
            file=sys.stderr,
        )
        return 2

    try:
        from .notify.bot import RadarBot
    except ImportError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    async def _serve() -> int:
        store = _open_store(args, settings)
        bot = RadarBot(settings, store)
        try:
            await bot.start()
        finally:
            await bot.close()
            store.close()
        return 0

    try:
        return asyncio.run(_serve())
    except KeyboardInterrupt:
        return 130


# ------------------------------------------------------------------- tui
def cmd_tui(args: argparse.Namespace, settings: Settings) -> int:
    from .tui import main as tui_main

    return tui_main()


# ------------------------------------------------------------------ entry
def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        settings = Settings()
    except ValidationError as exc:
        setup_logging("INFO", False)
        log.error("invalid configuration:\n%s", exc)
        return 2

    setup_logging(
        getattr(args, "log_level", None) or settings.log_level,
        bool(getattr(args, "log_json", False)) or settings.log_json,
    )

    handler: Callable[[argparse.Namespace, Settings], int] = args.func
    try:
        return handler(args, settings)
    except RadarError as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
