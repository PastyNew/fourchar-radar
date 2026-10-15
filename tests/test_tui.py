"""Terminal menu logic that does not need a real terminal.

The menu is the product here, so the contract it presents — the three numbered
options, the exact wording, what happens on each — is pinned down here. The
menu can then be driven with a scripted stdin, which is the closest thing to
using it that a test can do.
"""

from __future__ import annotations

import logging
import queue
import sys

import pytest

tui = pytest.importorskip("fourchar_radar.tui")

from fourchar_radar.config import Settings  # noqa: E402
from fourchar_radar.notify.webhook import redact_webhook  # noqa: E402

WEBHOOK = "https://discord.com/api/webhooks/123456789012345678/real-token"


# ------------------------------------------------------------------- menu
def test_menu_labels_are_exactly_as_specified() -> None:
    """The three numbered options are the whole contract."""
    assert tui.MENU_CHANGE_WEBHOOK == "1. Change Webhook"
    assert tui.MENU_SEARCH == "2. Search for usernames"
    assert tui.MENU_QUIT == "3. Close the program"


def test_app_identity() -> None:
    assert tui.APP_TITLE == "Discord Username Search"
    assert tui.DEVELOPER == "PastyNew"


def test_menu_is_drawn_with_all_three_options(capsys: pytest.CaptureFixture[str]) -> None:
    app = tui.RadarTui()
    app._banner()
    app._menu()
    out = capsys.readouterr().out

    for label in (tui.MENU_CHANGE_WEBHOOK, tui.MENU_SEARCH, tui.MENU_QUIT):
        assert label in out


# ------------------------------------------------------------------ colour
def test_colours_disable_when_not_a_terminal() -> None:
    """Piping the menu into a file must not emit escape codes."""
    off = tui.Ink(False)
    assert "\033" not in off.red("x")
    assert off.bold("x") == "x"


def test_colours_enable_when_requested() -> None:
    on = tui.Ink(True)
    assert on.red("x").startswith("\033[")
    assert "x" in on.red("x")


def test_no_color_env_is_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    assert tui._ink().on is False


# ------------------------------------------------------------ log pumping
def test_log_pump_forwards_records() -> None:
    sink: queue.Queue[str] = queue.Queue()
    tui.LogPump(sink).emit(logging.LogRecord("x", logging.INFO, "f", 1, "hello", None, None))
    assert "hello" in sink.get_nowait()


def test_log_pump_never_raises() -> None:
    """A broken sink must not take a running scan down."""

    class Broken(queue.Queue):  # type: ignore[type-arg]
        def put_nowait(self, item: object) -> None:
            raise RuntimeError("gone")

    pump = tui.LogPump(Broken())  # type: ignore[arg-type]
    pump.emit(logging.LogRecord("x", logging.INFO, "f", 1, "m", None, None))  # must not raise


# ----------------------------------------------------------------- markers
def test_found_marker_round_trip() -> None:
    item = f"__FOUND__{'cool':<8} {'$1,250':>9}  rare"
    assert item[len("__FOUND__") :].startswith("cool")


def test_stats_marker_round_trip() -> None:
    from fourchar_radar.models import ScanStats, utcnow

    stats = ScanStats(started_at=utcnow(), checked=1204, found=5, skipped=0)
    item = "__STATS__" + tui._stats_line(stats)
    assert "checked 1,204" in item[len("__STATS__") :]


def test_drain_handles_every_marker(capsys: pytest.CaptureFixture[str]) -> None:
    """An unhandled marker would print its own `__NAME__` prefix to the user."""
    app = tui.RadarTui()
    app._queue.put("__FOUND__cool  $1,250  rare")
    app._queue.put("__ERROR__webhook rejected")
    app._queue.put("plain log line")
    app._queue.put("__DONE__")
    app._drain()
    out = capsys.readouterr().out

    assert "cool" in out
    assert "webhook rejected" in out
    assert "plain log line" in out
    assert "__FOUND__" not in out
    assert "__ERROR__" not in out


# ------------------------------------------------------------------- stats
def test_stats_line_reads_naturally() -> None:
    from fourchar_radar.models import ScanStats, utcnow

    stats = ScanStats(started_at=utcnow(), checked=120, found=7, skipped=3)
    line = tui._stats_line(stats)
    assert "checked 120" in line
    assert "free 7" in line
    assert "skipped 3" in line


def test_stats_line_with_no_activity() -> None:
    from fourchar_radar.models import ScanStats

    assert "checked 0" in tui._stats_line(ScanStats())


# ---------------------------------------------------------------- webhooks
def test_webhook_is_remembered(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """It is setup, not per-search state, so it must survive a restart."""
    monkeypatch.setattr(tui, "user_data_dir", lambda: tmp_path)
    tui._save_webhook(WEBHOOK)
    assert tui._load_webhook() == WEBHOOK


def test_saved_webhook_is_owner_only_on_posix(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform.startswith("win"):
        pytest.skip("POSIX permissions only")
    monkeypatch.setattr(tui, "user_data_dir", lambda: tmp_path)
    tui._save_webhook(WEBHOOK)
    assert (tmp_path / "webhook.txt").stat().st_mode & 0o077 == 0


def test_missing_webhook_file_is_not_an_error(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tui, "user_data_dir", lambda: tmp_path)
    assert tui._load_webhook() == ""


def test_webhook_validates_like_the_cli() -> None:
    assert Settings(discord_webhook_url=WEBHOOK).discord_webhook_url == WEBHOOK
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        Settings(discord_webhook_url="nope")


def test_webhook_is_never_printed_in_full() -> None:
    assert "real-token" not in redact_webhook(WEBHOOK)


# ------------------------------------------------------------------ reuse
def test_tui_reuses_the_cli_engine() -> None:
    """The menu must not reimplement scanning; it wraps the same parts."""
    from pathlib import Path

    source = Path(tui.__file__ or "").read_text(encoding="utf-8")
    assert "from .checking.scanner import Scanner" in source
    assert "from .sources import build_source" in source
    assert "from .storage import Store" in source


def test_scan_without_a_webhook_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    app = tui.RadarTui()
    app.webhook = ""
    app.search()
    out = capsys.readouterr().out
    assert "No webhook" in out
    assert app._thread is None, "must not start a thread it cannot run"
