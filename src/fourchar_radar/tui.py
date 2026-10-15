"""Interactive menu, drawn in the terminal.

The point of this interface is that there is nothing to learn: no flags, no
config file, no arguments. You paste a webhook once and press a number.

Like the CLI it wraps, it contains no scanning logic of its own — it reuses the
same checker, limiter, pricer and store. What it adds is a menu, a live log,
and a stop you can reach with Ctrl-C.

Threading: the scan runs in a worker thread with its own event loop while the
main thread keeps printing. There is one shared lock, around the terminal, so
log lines can never interleave mid-line with a status update.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import queue
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .checking.client import DiscordAvailabilityClient
from .checking.ratelimit import AdaptiveRateLimiter
from .checking.scanner import Scanner
from .config import Settings
from .errors import ConfigError, InvalidWebhook
from .logging_setup import setup_logging
from .models import ScanStats
from .notify.webhook import WebhookNotifier, redact_webhook
from .platforms import IS_WINDOWS, user_data_dir
from .sources import build_source
from .storage import Store

APP_TITLE = "Discord Username Search"
DEVELOPER = "PastyNew"

MENU_CHANGE_WEBHOOK = "1. Change Webhook"
MENU_SEARCH = "2. Search for usernames"
MENU_QUIT = "3. Close the program"

WEBHOOK_FILE = "webhook.txt"
DB_FILE = "radar.db"

#: How often the menu redraws while idle, in seconds.
TICK = 0.1


# ------------------------------------------------------------------- colour
class Ink:
    """ANSI colours, disabled automatically when the output is not a terminal."""

    def __init__(self, enabled: bool) -> None:
        self.on = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.on else text

    def bold(self, t: str) -> str:
        return self._wrap("1", t)

    def dim(self, t: str) -> str:
        return self._wrap("2", t)

    def cyan(self, t: str) -> str:
        return self._wrap("36", t)

    def green(self, t: str) -> str:
        return self._wrap("32", t)

    def yellow(self, t: str) -> str:
        return self._wrap("33", t)

    def red(self, t: str) -> str:
        return self._wrap("31", t)

    def blue(self, t: str) -> str:
        return self._wrap("34", t)

    def grey(self, t: str) -> str:
        return self._wrap("90", t)


def _ink() -> Ink:
    if os.environ.get("NO_COLOR"):
        return Ink(False)
    return Ink(sys.stdout.isatty())


INK = _ink()


# -------------------------------------------------------------------- output
class Console:
    """The only thing allowed to write to the terminal.

    ``sys.stdout`` is looked up per call rather than captured, so redirecting
    output (``fourchar-radar tui > log.txt``) and test capture both work.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def write(self, text: str = "") -> None:
        with self._lock:
            stream = sys.stdout
            stream.write(text)
            stream.flush()

    def line(self, text: str = "") -> None:
        self.write(text + "\n")

    def rule(self, char: str = "─", width: int = 58) -> None:
        self.line(INK.dim(char * width))


CONSOLE = Console()


def _first_line(exc: Exception) -> str:
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__


# ----------------------------------------------------------------- webhooks
def _webhook_path() -> Path:
    return user_data_dir() / WEBHOOK_FILE


def _load_webhook() -> str:
    path = _webhook_path()
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return ""


def _save_webhook(url: str) -> None:
    path = _webhook_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(url, encoding="utf-8")
        # A live credential: keep it readable only by its owner where the
        # platform lets us.
        if not IS_WINDOWS:
            with contextlib.suppress(OSError):
                path.chmod(0o600)
    except OSError as exc:
        CONSOLE.line(INK.yellow(f"  note: could not save the webhook ({exc})"))


# -------------------------------------------------------------- log capture
class LogPump(logging.Handler):
    """Pipes the radar's loggers into the console, from the scan thread."""

    def __init__(self, sink: queue.Queue[str]) -> None:
        super().__init__(level=logging.INFO)
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        with contextlib.suppress(Exception):
            self._sink.put_nowait(self.format(record))


# ----------------------------------------------------------------- the menu
class RadarTui:
    """The interactive menu. A scan is one of its states, not a mode."""

    def __init__(self) -> None:
        self.db_path = str(user_data_dir() / DB_FILE)
        self.webhook = _load_webhook()

        self._queue: queue.Queue[str] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._scanner: Scanner | None = None
        self._stop_requested = False
        self._found_count = 0
        self._recent: list[str] = []

    # ------------------------------------------------------------- helpers
    def _banner(self) -> None:
        title = INK.bold(INK.cyan(APP_TITLE))
        dev = INK.dim(f"Developer: {DEVELOPER}")
        CONSOLE.line()
        CONSOLE.line(f"  {title}")
        CONSOLE.line(f"  {dev}")
        CONSOLE.line()

    def _menu(self) -> None:
        CONSOLE.line(f"  {INK.bold(MENU_CHANGE_WEBHOOK)}")
        CONSOLE.line(f"  {INK.bold(MENU_SEARCH)}")
        CONSOLE.line(f"  {INK.bold(MENU_QUIT)}")
        CONSOLE.line()

    def _prompt(self, text: str) -> str:
        try:
            return input(INK.blue(f"  {text} ")).strip()
        except (EOFError, KeyboardInterrupt):
            return ""

    # ------------------------------------------------------------- actions
    def change_webhook(self) -> None:
        CONSOLE.rule()
        CONSOLE.line(INK.bold("  Paste your Discord webhook URL"))
        CONSOLE.line()
        CONSOLE.line("  " + INK.dim("Channel -> Edit Channel -> Integrations -> Webhooks ->"))
        CONSOLE.line("  " + INK.dim("New Webhook -> Copy Webhook URL"))
        CONSOLE.line()
        if self.webhook:
            CONSOLE.line(f"  {INK.dim('Current:')} {redact_webhook(self.webhook)}")
            CONSOLE.line()

        url = self._prompt("Webhook URL:")
        if not url:
            CONSOLE.line(INK.yellow("  Nothing entered, keeping the previous webhook."))
            return

        try:
            Settings(discord_webhook_url=url, db_path=self.db_path)
        except Exception as exc:  # noqa: BLE001 - pydantic ValidationError
            CONSOLE.line(INK.red(f"  Not a valid webhook URL: {_first_line(exc)}"))
            CONSOLE.line()
            CONSOLE.line(
                "  "
                + INK.dim("It must look like https://discord.com/api/webhooks/<digits>/<token>")
            )
            return

        self.webhook = url
        _save_webhook(url)
        CONSOLE.line(INK.green(f"  Saved: {redact_webhook(url)}"))
        CONSOLE.line(
            INK.dim("  It is remembered for next time. Option 2 will verify it with Discord.")
        )

    def _ask_report_mode(self) -> bool:
        """Ask how chatty the channel should be.

        Reporting every verdict is what most people want from a radar — silence
        on a scan that runs for days reads as "broken". But it is one message
        per check, so it is opt-in rather than the default.
        """
        CONSOLE.line()
        CONSOLE.line("  How much should the channel say?")
        CONSOLE.line(f"    a) {INK.bold('every name')} — free AND taken, one message each")
        CONSOLE.line(INK.dim("    b) just the good ones + a progress ping every 30 min"))
        CONSOLE.line()
        answer = self._prompt("Choose a or b:").lower()
        return answer.startswith("a")

    def search(self) -> None:
        if not self.webhook:
            CONSOLE.line(INK.yellow("  No webhook yet. Choose option 1 first."))
            return

        report_every = self._ask_report_mode()

        try:
            settings = Settings(
                discord_webhook_url=self.webhook,
                db_path=self.db_path,
                report_every=report_every,
            )
        except ConfigError as exc:
            CONSOLE.line(INK.red(f"  {exc}"))
            return
        except Exception as exc:  # noqa: BLE001
            CONSOLE.line(INK.red(f"  {_first_line(exc)}"))
            return

        self._found_count = 0
        self._recent.clear()
        self._stop_requested = False

        self._thread = threading.Thread(
            target=self._worker, args=(settings,), name="radar-scan", daemon=True
        )
        self._thread.start()
        self._watch()

    def quit(self) -> bool:
        """Offer to stop a running scan and close. False means "stay open"."""
        if self._running:
            CONSOLE.line(INK.yellow("  A search is still running."))
            if self._prompt("Stop it and close? (y/N)").lower() not in ("y", "yes"):
                return False
            self._request_stop()
            self._wait_for_scan(timeout=5.0)
        CONSOLE.line()
        CONSOLE.line(INK.dim("  Bye."))
        return True

    def _wait_for_scan(self, timeout: float) -> bool:
        """Block until the worker unwinds, so the database closes cleanly."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._thread is None or not self._thread.is_alive():
                return True
            time.sleep(0.1)
        return False

    # ----------------------------------------------------------------- loop
    def run(self) -> int:
        setup_logging("INFO", as_json=False)
        logging.getLogger().addHandler(LogPump(self._queue))

        while True:
            self._banner()
            if self.webhook:
                CONSOLE.line(f"  {INK.bold('Your webhook:')} {INK.dim('(saved)')}")
            else:
                CONSOLE.line(f"  {INK.bold('Your webhook:')} {INK.dim('(not set yet)')}")
            CONSOLE.line()
            self._menu()

            choice = self._prompt("Choose 1, 2 or 3:")
            match choice:
                case "1":
                    self.change_webhook()
                case "2":
                    self.search()
                case "3" | "":
                    if choice == "":
                        CONSOLE.line(INK.dim("  Nothing chosen."))
                        continue
                    if self.quit():
                        return 0
                case _:
                    CONSOLE.line(INK.red(f"  '{choice}' is not one of the options."))

    @property
    def _running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _request_stop(self) -> None:
        if self._loop is None or self._scanner is None:
            return
        self._stop_requested = True
        # Scanner.stop() sets an asyncio.Event, which is not thread-safe.
        with contextlib.suppress(RuntimeError):
            self._loop.call_soon_threadsafe(self._scanner.stop)

    # -------------------------------------------------------------- scanning
    def _watch(self) -> None:
        """Print live output until the scan ends or the user interrupts it."""
        CONSOLE.rule("═")
        CONSOLE.line(f"  {INK.bold('Searching')} {INK.dim('— press Ctrl+C to stop when you like')}")
        CONSOLE.rule("═")
        CONSOLE.line()

        interrupted = False
        try:
            while self._running:
                self._drain()
                time.sleep(TICK)
        except KeyboardInterrupt:
            interrupted = True
            CONSOLE.line()
            CONSOLE.line(INK.yellow("  Stopping after the current check..."))
            self._request_stop()
            while self._running:
                self._drain()
                time.sleep(0.1)

        self._drain()
        self._thread = None

        CONSOLE.line()
        if self._recent:
            CONSOLE.line(INK.bold("  Found so far:"))
            for item in self._recent[-10:]:
                CONSOLE.line(f"    {item}")
            CONSOLE.line()
        CONSOLE.line(INK.dim("  Everything checked is saved. Option 2 resumes where it stopped."))
        if interrupted:
            CONSOLE.line(INK.dim("  Press Enter to return to the menu."))
            with contextlib.suppress(EOFError, KeyboardInterrupt):
                input()

    def _drain(self) -> None:
        """Move queued log lines and findings onto the screen."""
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return

            if item == "__RESET_FOUND__":
                self._found_count = 0
                self._recent.clear()
            elif item.startswith("__FOUND__"):
                self._found_count += 1
                self._recent.append(item[len("__FOUND__") :])
                CONSOLE.line(f"  {INK.green('FREE')}   {item[len('__FOUND__') :]}")
            elif item.startswith("__TAKEN__"):
                body = item[len("__TAKEN__") :]
                if "FREE" in body:
                    CONSOLE.line(f"  {INK.green('FREE')}   {body}")
                else:
                    CONSOLE.line(INK.dim(f"  taken  {body}"))
            elif item.startswith("__PING__"):
                self._clear_status()
                CONSOLE.line(INK.dim(f"  ── {item[len('__PING__') :]}"))
            elif item.startswith("__STATS__"):
                # Progress ticker: rewrite the line in place so it does not
                # scroll the log away.
                self._paint_status(item[len("__STATS__") :])
            elif item.startswith("__ERROR__"):
                self._clear_status()
                CONSOLE.line()
                CONSOLE.line(INK.red(f"  {item[len('__ERROR__') :]}"))
            elif item == "__DONE__":
                self._clear_status()
            else:
                self._clear_status()
                CONSOLE.line(INK.dim(item))

    def _paint_status(self, text: str) -> None:
        """Draw the ticker on one line, overwriting whatever was there."""
        if not sys.stdout.isatty():
            return  # redirecting to a file: one line per update is fine
        padded = (INK.dim(text) + " " * 40)[:100]
        CONSOLE.write("\r\033[2K" + padded)

    def _clear_status(self) -> None:
        if not sys.stdout.isatty():
            return
        CONSOLE.write("\r\033[2K")

    def _notifier(self, settings: Settings) -> WebhookNotifier:
        """Post to Discord *and* mirror into the terminal.

        The scanner takes a single notifier, and the terminal app needs to see
        every message too. Handing the scanner the app alone would silence the
        channel — the whole point is that it reports there — so the app wraps
        the webhook rather than replacing it.
        """
        webhook = WebhookNotifier(settings)
        return _DualNotifier(webhook, self)  # type: ignore[return-value]

    def _worker(self, settings: Settings) -> None:
        """Thread body: its own event loop, no terminal writes from here."""
        try:
            asyncio.run(self._scan(settings))
        except (InvalidWebhook, ConfigError) as exc:
            self._queue.put(f"__ERROR__{exc}")
        except Exception as exc:  # noqa: BLE001 - never vanish silently
            logging.getLogger(__name__).exception("scan failed")
            self._queue.put(f"__ERROR__{exc}")
        finally:
            self._queue.put("__DONE__")

    async def _scan(self, settings: Settings) -> None:
        self._loop = asyncio.get_running_loop()

        notifier = self._notifier(settings)
        try:
            await notifier.verify()
        except InvalidWebhook:
            # _worker reports it; queueing here too would print it twice.
            raise
        self._queue.put("webhook verified by Discord")

        limiter = AdaptiveRateLimiter(
            settings.rate_limit_rps,
            start_rps=settings.rate_limit_start_rps or None,
        )
        store = Store(settings.db_path)
        try:
            async with DiscordAvailabilityClient(settings, limiter) as client:
                scanner = Scanner(settings, store, client, limiter, notifier)
                self._scanner = scanner
                beat = asyncio.create_task(self._heartbeat(scanner))
                try:
                    await scanner.run(build_source(settings))
                finally:
                    beat.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await beat

                # Everything still free, not only what cleared the alert
                # threshold — the low tiers are recorded but never pinged.
                # The store is the complete picture, so it replaces the
                # running list rather than adding to it.
                self._queue.put("__RESET_FOUND__")
                for finding in store.findings(limit=50):
                    self._queue.put(
                        f"__FOUND__{finding.name:<8} {finding.quote.price_label:>9}  "
                        f"{finding.tier.value}"
                    )
                self._queue.put(f"── search finished · {scanner.stats.summary()}")
        finally:
            store.close()
            await notifier.close()
            self._scanner = None
            self._loop = None

    async def _heartbeat(self, scanner: Scanner) -> None:
        while not scanner.stopping:
            self._queue.put("__STATS__" + _stats_line(scanner.stats))
            await asyncio.sleep(2.0)

    async def notify(self, finding: Any) -> None:
        """The scanner calls this for every alert-worthy find."""
        quote = finding.quote
        self._queue.put(f"__FOUND__{quote.name:<8} {quote.price_label:>9}  {quote.tier.value}")

    async def notify_result(
        self, name: str, available: bool, price_label: str = "", tier: str = ""
    ) -> None:
        """Mirror the per-name Discord message into the terminal too.

        Discord now says "free" or "taken" for every check, and the terminal
        should not be the poorer half of that.
        """
        if available:
            tail = f"  {price_label} ({tier})" if price_label else ""
            self._queue.put(f"__TAKEN__{name}  FREE{tail}")
        else:
            self._queue.put(f"__TAKEN__{name}  taken")

    async def notify_progress(
        self,
        stats: ScanStats,
        free_names: Sequence[str] = (),
        rate_rps: float | None = None,
        throttles: int = 0,
    ) -> None:
        """Announce the same heartbeat Discord just received."""
        parts = [f"checked {stats.checked:,}", f"taken {stats.taken:,}", f"free {stats.found:,}"]
        if rate_rps is not None:
            parts.append(f"{rate_rps:.4f} req/s")
        if throttles:
            parts.append(f"throttled {throttles}×")
        self._queue.put("__PING__" + " · ".join(parts))


class _DualNotifier:
    """Forwards to the webhook, then mirrors into the terminal.

    Discord is where the user asked to be told; the terminal is where they
    are sitting. Both should show the same thing, so neither is dropped when
    the other is added.
    """

    def __init__(self, webhook: WebhookNotifier, tui: RadarTui) -> None:
        self._webhook = webhook
        self._tui = tui

    async def verify(self) -> None:
        await self._webhook.verify()

    async def notify(self, finding: Any) -> None:
        await self._webhook.notify(finding)
        await self._tui.notify(finding)

    async def notify_result(
        self, name: str, available: bool, price_label: str = "", tier: str = ""
    ) -> None:
        await self._webhook.notify_result(name, available, price_label, tier)
        await self._tui.notify_result(name, available, price_label, tier)

    async def notify_progress(self, *args: Any, **kwargs: Any) -> None:
        await self._webhook.notify_progress(*args, **kwargs)
        await self._tui.notify_progress(*args, **kwargs)

    async def close(self) -> None:
        await self._webhook.close()


def _stats_line(stats: ScanStats) -> str:
    return (
        f"progress · checked {stats.checked:,} · free {stats.found:,} · "
        f"skipped {stats.skipped:,} · {stats.rps:.4f} req/s"
    )


def main() -> int:
    """Entry point for ``fourchar-radar-tui`` and ``fourchar-radar tui``."""
    try:
        RadarTui().run()
    except SystemExit as exc:
        return int(exc.code or 0)
    except KeyboardInterrupt:
        CONSOLE.line()
        return 130
    return 0
