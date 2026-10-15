"""Discord reporting: every verdict, and a heartbeat while nothing is found.

Two things are being pinned here:

1. The user asked to be told whether each name is taken or free, not only
   about the lucky finds. That is `--report-every`.
2. A scan runs for days against a throttle that lets through a handful of
   requests at a time. A channel that says nothing for hours is
   indistinguishable from a crash, so there is a heartbeat on both a count
   gate and a time gate.
"""

from __future__ import annotations

import httpx
import pytest

from conftest import WEBHOOK
from fourchar_radar.config import Settings
from fourchar_radar.models import ScanStats, utcnow
from fourchar_radar.notify.embed import build_progress_embed, build_result_embed
from fourchar_radar.notify.webhook import WebhookNotifier


def _settings(**kw: object) -> Settings:
    return Settings(discord_webhook_url=WEBHOOK, **kw)  # type: ignore[arg-type]


# ------------------------------------------------------------------ embeds
def test_result_embed_says_free() -> None:
    embed = build_result_embed("cool", True, "$1,250", "rare")
    assert "free" in embed["title"].lower()
    assert "$1,250" in embed["title"]
    assert embed["color"] == 0x3BA55C


def test_result_embed_says_taken() -> None:
    embed = build_result_embed("zzzz", False)
    assert "taken" in embed["title"].lower()
    assert embed["color"] == 0x9BA5B4


def test_result_embed_without_price_still_reads() -> None:
    assert "$" not in build_result_embed("cool", True)["title"]


def test_progress_embed_reports_both_buckets() -> None:
    stats = ScanStats(started_at=utcnow(), checked=500, taken=480, found=20)
    embed = build_progress_embed(stats, rate_rps=0.0076, throttles=3)
    fields = {f["name"]: f["value"] for f in embed["fields"]}

    assert fields["Checked"] == "500"
    assert fields["Taken"] == "480"
    assert fields["Free"] == "20"
    assert "req/s" in fields["Rate"]
    assert fields["Throttles"] == "3"


def test_progress_embed_lists_finds() -> None:
    stats = ScanStats(checked=10, found=3, taken=7)
    embed = build_progress_embed(stats, free_names=["cool", "face"])
    assert "cool" in embed["description"]
    assert "face" in embed["description"]


def test_progress_embed_caps_the_list() -> None:
    stats = ScanStats(checked=100, found=50)
    embed = build_progress_embed(stats, free_names=[f"n{i:03d}" for i in range(50)])
    assert "more" in embed["description"], "must not paste 50 names into one message"


def test_progress_embed_says_so_when_nothing_is_free() -> None:
    """The important case: a long scan with no finds must not look broken."""
    stats = ScanStats(checked=200, found=0, taken=200)
    embed = build_progress_embed(stats)
    assert "Nothing free yet" in embed["description"]
    assert embed["title"]


# ---------------------------------------------------------------- settings
def test_report_every_is_off_by_default() -> None:
    """One message per check is too noisy to be anyone's default."""
    assert _settings().report_every is False


def test_progress_reporting_is_on_by_default() -> None:
    assert _settings().report_progress is True


def test_report_gates_have_sane_defaults() -> None:
    s = _settings()
    assert 0 < s.report_every_checks <= 100
    assert s.report_interval_s >= 300


def test_report_every_checks_must_not_be_negative() -> None:
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        _settings(report_every_checks=-1)


# --------------------------------------------------------------- delivery
class Recorder:
    def __init__(self) -> None:
        self.payloads: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        import json

        self.payloads.append(json.loads(request.content))
        return httpx.Response(204)


async def _notifier(recorder: Recorder) -> tuple[WebhookNotifier, httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    return WebhookNotifier(_settings(), client=http), http


async def test_progress_is_delivered() -> None:
    rec = Recorder()
    notifier, http = await _notifier(rec)

    await notifier.notify_progress(ScanStats(checked=100, found=4, taken=96), rate_rps=0.008)
    await http.aclose()

    assert len(rec.payloads) == 1
    payload = rec.payloads[0]
    assert "Still searching" in payload["embeds"][0]["title"]
    assert payload["allowed_mentions"] == {"parse": []}


async def test_every_result_is_delivered() -> None:
    rec = Recorder()
    notifier, http = await _notifier(rec)

    await notifier.notify_result("cool", True, "$1,250", "rare")
    await notifier.notify_result("zzzz", False)
    await http.aclose()

    titles = [p["embeds"][0]["title"] for p in rec.payloads]
    assert "free" in titles[0].lower()
    assert "taken" in titles[1].lower()
