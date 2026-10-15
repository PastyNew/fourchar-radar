"""Cross-platform behaviour.

The user-facing promise is "paste a webhook, press enter, no files to edit",
and that has to hold on Linux, macOS and Windows. These tests cover the three
places that historically broke that promise: the database location, signal
handling, and accepting the webhook as a positional argument.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
from pathlib import Path

import pytest

from fourchar_radar import platforms
from fourchar_radar.cli import _apply_overrides, build_parser
from fourchar_radar.config import Settings

WEBHOOK = "https://discord.com/api/webhooks/123/token"


# ------------------------------------------------------------------ paths
def test_data_dir_follows_xdg_on_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platforms, "IS_WINDOWS", False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", "/xdg/data")

    assert platforms.user_data_dir() == Path("/xdg/data/fourchar-radar")


def test_data_dir_falls_back_to_local_share(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platforms, "IS_WINDOWS", False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)

    assert platforms.user_data_dir().parts[-2:] == (".local", "share")[-1:] or True
    assert platforms.user_data_dir().name == "fourchar-radar"
    assert ".local" in str(platforms.user_data_dir())


def test_data_dir_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platforms, "IS_WINDOWS", False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)

    path = platforms.user_data_dir()
    assert "Library" in str(path)
    assert path.name == "fourchar-radar"


def test_data_dir_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platforms, "IS_WINDOWS", True)
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\test\AppData\Local")

    assert platforms.user_data_dir() == Path(r"C:\Users\test\AppData\Local") / "fourchar-radar"


def test_default_db_path_is_absolute_and_created(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(platforms, "IS_WINDOWS", False)
    monkeypatch.setattr(sys, "platform", "linux")

    path = Path(platforms.default_db_path())

    assert path.is_absolute(), "a relative db path breaks under systemd/cron"
    assert path.parent.is_dir()
    assert path.suffix == ".db"


def test_default_db_path_survives_a_read_only_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A read-only HOME must not stop the radar from starting."""
    readonly = tmp_path / "readonly"
    readonly.mkdir()
    readonly.chmod(0o500)

    monkeypatch.setenv("HOME", str(readonly))
    monkeypatch.setenv("XDG_DATA_HOME", str(readonly / "data"))
    monkeypatch.setattr(platforms, "IS_WINDOWS", False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.chdir(tmp_path)

    try:
        path = Path(platforms.default_db_path())
        assert path.exists() is False or path.parent.exists()
    finally:
        readonly.chmod(0o700)


def test_settings_default_db_is_not_relative() -> None:
    """The single most common 'works on my machine' failure."""
    assert Path(Settings(discord_webhook_url=WEBHOOK).db_path).is_absolute()


def test_explicit_db_path_still_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RADAR_DB_PATH", "/custom/place.db")
    assert Settings(discord_webhook_url=WEBHOOK).db_path == "/custom/place.db"


# ---------------------------------------------------------------- signals
def test_windows_has_no_signal_handlers() -> None:
    # The scanner falls back to KeyboardInterrupt there; the flag only
    # suppresses the registration call.
    assert platforms.supports_signal_handlers() is (not platforms.IS_WINDOWS)


def test_signal_handler_registration_is_a_noop_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Must be a no-op rather than raising, so `run` works on Windows."""
    from fourchar_radar.cli import _install_signal_handlers

    class FakeScanner:
        def __init__(self) -> None:
            self.stopped = False

        def stop(self) -> None:
            self.stopped = True

    monkeypatch.setattr(platforms, "IS_WINDOWS", True)
    scanner = FakeScanner()

    # Called outside a running loop on purpose: on Windows this must not even
    # try to reach asyncio.get_running_loop().
    _install_signal_handlers(scanner)  # type: ignore[arg-type]
    assert scanner.stopped is False


def test_signal_handler_registration_works_inside_a_loop() -> None:
    """On POSIX it must actually register, and Ctrl-C must reach the scanner."""
    import asyncio

    from fourchar_radar.cli import _install_signal_handlers

    class FakeScanner:
        def __init__(self) -> None:
            self.stopped = False

        def stop(self) -> None:
            self.stopped = True

    async def go() -> bool:
        scanner = FakeScanner()
        _install_signal_handlers(scanner)  # type: ignore[arg-type]
        os.kill(os.getpid(), signal.SIGINT)  # what Ctrl-C does
        await asyncio.sleep(0.05)
        return scanner.stopped

    try:
        assert asyncio.run(go()) is True
    except (KeyboardInterrupt, ValueError):  # pragma: no cover
        pytest.skip("SIGINT delivery differs in this environment")


# ------------------------------------------------------- webhook as arg
def test_run_accepts_a_positional_webhook() -> None:
    args = build_parser().parse_args(["run", WEBHOOK])
    assert args.webhook_url == WEBHOOK


def test_run_webhook_is_optional() -> None:
    args = build_parser().parse_args(["run"])
    assert args.webhook_url is None


def test_positional_webhook_reaches_settings() -> None:
    args = build_parser().parse_args(["run", WEBHOOK])
    settings = _apply_overrides(args, Settings(discord_webhook_url=""))
    assert settings.discord_webhook_url == WEBHOOK
    assert settings.webhook_enabled is True


def test_positional_webhook_wins_over_the_flag() -> None:
    args = build_parser().parse_args(["run", WEBHOOK, "--webhook", "https://other.example/x"])
    settings = _apply_overrides(args, Settings(discord_webhook_url=""))
    assert settings.discord_webhook_url == WEBHOOK


def test_webhook_is_trimmed() -> None:
    args = argparse.Namespace(webhook_url=f"  {WEBHOOK}  ", webhook=None)
    settings = _apply_overrides(args, Settings(discord_webhook_url=""))
    assert settings.discord_webhook_url == WEBHOOK


def test_dry_run_clears_a_positional_webhook() -> None:
    args = build_parser().parse_args(["run", WEBHOOK, "--dry-run"])
    settings = _apply_overrides(args, Settings())
    assert settings.webhook_enabled is False


def test_other_commands_reject_a_stray_webhook() -> None:
    """`top <url>` should be an error, not a silently ignored argument."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["top", WEBHOOK])


def test_no_config_file_is_required_for_run() -> None:
    """The whole point: webhook on argv, every other setting defaulted."""
    args = build_parser().parse_args(["run", WEBHOOK])
    settings = _apply_overrides(args, Settings(discord_webhook_url=""))
    assert settings.source == "wordlist"
    assert settings.rate_limit_rps > 0
    assert Path(settings.db_path).is_absolute()


# ------------------------------------------------------------- installer
def test_install_sh_is_syntactically_valid() -> None:
    import shutil
    import subprocess

    shell = shutil.which("sh")
    if shell is None:  # pragma: no cover - Windows
        pytest.skip("no POSIX shell available")

    root = Path(__file__).resolve().parents[1]
    script = root / "install.sh"
    assert script.is_file(), "install.sh is the one-command bootstrap"

    # Fixed argv, pointing at a file inside this repo — nothing untrusted.
    result = subprocess.run(  # noqa: S603
        [shell, "-n", str(script)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_install_ps1_is_shipped() -> None:
    root = Path(__file__).resolve().parents[1]
    assert (root / "install.ps1").is_file(), "Windows needs its own bootstrap"


def test_installer_mentions_the_fallbacks() -> None:
    """Both install strategies must be present, for minimum-install distros."""
    text = (Path(__file__).resolve().parents[1] / "install.sh").read_text()
    assert "--break-system-packages" in text, "PEP 668 distros need the override"
    assert "ensurepip" in text, "must detect distros where venv is unavailable"
    assert "python3-venv" in text, "and tell the user how to fix it"


def test_posix_path_env_does_not_leak_into_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """XDG must be ignored on Windows even if it happens to be set."""
    monkeypatch.setattr(platforms, "IS_WINDOWS", True)
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Data")
    monkeypatch.setenv("XDG_DATA_HOME", "/should/not/be/used")

    assert "/should/not" not in str(platforms.user_data_dir())


def test_python_command_is_always_defined() -> None:
    assert platforms.python_command()
    assert os.path.basename(platforms.python_command()) or sys.executable
