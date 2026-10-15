"""Bot-mode wiring.

The bot is an optional extra, so nothing in the default test run imports
`discord.py`. That is exactly how three real type errors reached `main`:
without discord.py installed, mypy resolved every discord type to `Any` and
had nothing to complain about. CI installs the extras and caught them.

These tests keep the failure local, where it is cheap to fix, by asserting the
seams that broke rather than by testing discord.py itself.
"""

from __future__ import annotations

import pytest

from fourchar_radar.cli import build_parser
from fourchar_radar.config import SOURCE_NAMES, Settings

WEBHOOK = "https://discord.com/api/webhooks/1/t"


def test_source_choices_match_the_config() -> None:
    """Slash-command choices and `Settings.source` must not drift apart.

    The bot's `/scan start` assigns the chosen value straight into settings;
    if the two lists diverge, that assignment becomes unvalidated.
    """
    run_parser = build_parser()._subparsers._group_actions[0].choices["run"]
    choices = next(a for a in run_parser._actions if a.dest == "source")

    assert set(choices.choices) == set(SOURCE_NAMES)


def test_source_names_are_unique() -> None:
    assert len(set(SOURCE_NAMES)) == len(SOURCE_NAMES)


def test_bot_module_imports_only_with_the_extra() -> None:
    """Documents the dependency: webhook mode must never need discord.py."""
    import importlib.util

    if importlib.util.find_spec("discord") is None:  # pragma: no cover
        with pytest.raises(ImportError, match=r"fourchar-radar\[bot\]"):
            from fourchar_radar.notify.bot import RadarBot  # noqa: F401
    else:
        from fourchar_radar.notify.bot import RadarBot  # noqa: F401

        assert RadarBot is not None


def test_notify_package_defers_the_bot_import() -> None:
    """`import fourchar_radar.notify` must not drag in discord.py."""
    import fourchar_radar.notify as notify

    # The attribute is only resolved on first access (module __getattr__).
    with pytest.raises(AttributeError):
        _ = notify.does_not_exist


def test_every_source_name_validates() -> None:
    # `file` additionally requires a path; the rest need nothing extra.
    for name in SOURCE_NAMES:
        kwargs: dict[str, object] = {"source": name, "discord_webhook_url": WEBHOOK}
        if name == "file":
            kwargs["names_file"] = "/tmp/words.txt"
        assert Settings(**kwargs).source == name  # type: ignore[arg-type]


def test_unknown_source_is_rejected() -> None:
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        Settings(source="not-a-source", discord_webhook_url=WEBHOOK)
