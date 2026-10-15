"""CLI argument handling and the settings-override path."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from fourchar_radar.cli import _apply_overrides, build_parser, main
from fourchar_radar.config import Settings
from fourchar_radar.errors import ConfigError

WEBHOOK = "https://discord.com/api/webhooks/123/token"


def parse(argv: list[str]):  # type: ignore[no-untyped-def]
    return build_parser().parse_args(argv)


def test_global_flags_work_before_the_subcommand() -> None:
    args = parse(["--db", "/tmp/a.db", "top"])
    assert args.db == "/tmp/a.db"
    assert args.command == "top"


def test_global_flags_work_after_the_subcommand() -> None:
    args = parse(["top", "--db", "/tmp/a.db"])
    assert args.db == "/tmp/a.db"


def test_unspecified_global_flag_does_not_clobber() -> None:
    args = parse(["--db", "/tmp/a.db", "top"])
    assert args.db == "/tmp/a.db"
    assert not hasattr(args, "log_json") or args.log_json is False


def test_run_flags_are_parsed() -> None:
    args = parse(
        [
            "run",
            "--source",
            "bruteforce",
            "--length",
            "4",
            "--limit",
            "10",
            "--rps",
            "0.2",
            "--min-tier",
            "epic",
            "--dry-run",
        ]
    )
    assert args.source == "bruteforce"
    assert args.limit == 10
    assert args.rps == 0.2
    assert args.min_tier == "epic"
    assert args.dry_run is True


def test_min_price_maps_to_a_tier() -> None:
    from fourchar_radar.cli import _tier_for_price

    assert _tier_for_price(0).value == "common"
    assert _tier_for_price(5_000).value == "rare"
    assert _tier_for_price(60_000).value == "legendary"


def test_require_webhook_explains_itself() -> None:
    settings = Settings(discord_webhook_url="", alert_webhook=True)
    with pytest.raises(ConfigError) as excinfo:
        settings.require_webhook()
    assert "DISCORD_WEBHOOK_URL" in str(excinfo.value)
    assert "--dry-run" in str(excinfo.value)


def test_require_webhook_returns_the_url() -> None:
    settings = Settings(discord_webhook_url=WEBHOOK)
    assert settings.require_webhook() == WEBHOOK


def test_webhook_url_is_validated() -> None:
    with pytest.raises(ValidationError):
        Settings(discord_webhook_url="https://example.com/not-a-webhook")


def test_settings_without_webhook_are_valid_for_read_only_commands() -> None:
    """`top` and `status` must work with no webhook configured at all."""
    settings = Settings(discord_webhook_url="")
    assert settings.webhook_enabled is False


def test_alert_webhook_false_disables_delivery() -> None:
    settings = Settings(discord_webhook_url=WEBHOOK, alert_webhook=False)
    assert settings.webhook_enabled is False


def test_status_command_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    assert main(["status", "--db", str(tmp_path / "s.db")]) == 0


def test_sources_command_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    assert main(["sources"]) == 0


def test_export_command_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    out = tmp_path / "findings.json"
    rc = main(["export", "--db", str(tmp_path / "s.db"), "--format", "json", "--out", str(out)])
    assert rc == 0
    assert out.exists()


def test_bot_without_token_exits_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.delenv("RADAR_DISCORD_BOT_TOKEN", raising=False)
    assert main(["bot"]) == 2


# ------------------------------------------------------------- proxies
def test_proxy_flag_is_repeatable() -> None:
    args = build_parser().parse_args(["run", "--proxy", "http://a:1", "--proxy", "http://b:2"])
    assert args.proxy == ["http://a:1", "http://b:2"]


def test_repeated_proxies_become_a_list() -> None:
    args = build_parser().parse_args(["run", "--proxy", "http://a:1", "--proxy", "http://b:2"])
    settings = _apply_overrides(args, Settings(discord_webhook_url=WEBHOOK))
    assert settings.proxy_list == "http://a:1,http://b:2"


def test_proxy_file_is_accepted() -> None:
    args = build_parser().parse_args(["run", "--proxy-file", "/tmp/p.txt"])
    settings = _apply_overrides(args, Settings(discord_webhook_url=WEBHOOK))
    assert settings.proxy_file == "/tmp/p.txt"


def test_test_proxies_runs_without_a_webhook(monkeypatch: pytest.MonkeyPatch) -> None:
    """A diagnostic must not require the configuration it exists to diagnose."""
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("RADAR_DISCORD_WEBHOOK_URL", raising=False)
    # No proxies configured: says so and points at the real flags.
    assert main(["run", "--test-proxies"]) == 1
