"""Webhook delivery.

Discord delivery cannot be exercised in CI, so these tests drive the notifier
through `httpx.MockTransport` and assert on the exact payload that would go
out. They also pin the two safety properties: webhook tokens are never logged,
and alerts can never @-mention anyone.
"""

from __future__ import annotations

import json

import httpx
import pytest

from conftest import WEBHOOK
from fourchar_radar.config import Settings
from fourchar_radar.errors import InvalidWebhook
from fourchar_radar.models import ScanStats
from fourchar_radar.notify.base import Notifier, NullNotifier
from fourchar_radar.notify.webhook import WebhookNotifier, redact_webhook


class Recorder:
    """Captures request payloads and replays a scripted response."""

    def __init__(self, responses: list[httpx.Response] | None = None) -> None:
        self.payloads: list[dict] = []
        self._responses = list(responses or [])

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.payloads.append(json.loads(request.content))
        return self._responses.pop(0) if self._responses else httpx.Response(204)

    @property
    def last(self) -> dict:
        return self.payloads[-1]


def _notifier(
    monkeypatch: pytest.MonkeyPatch, recorder: Recorder, **overrides: object
) -> tuple[WebhookNotifier, httpx.AsyncClient]:
    base: dict[str, object] = {"discord_webhook_url": WEBHOOK, "rate_limit_rps": 20.0}
    base.update(overrides)
    settings = Settings(**base)  # type: ignore[arg-type]
    http = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    return WebhookNotifier(settings, client=http), http


async def test_finding_payload_shape(monkeypatch: pytest.MonkeyPatch, make_finding) -> None:  # type: ignore[no-untyped-def]
    recorder = Recorder()
    notifier, http = _notifier(monkeypatch, recorder)

    await notifier.notify(make_finding("cool", price=5000))
    await http.aclose()

    payload = recorder.last
    assert payload["username"] == "fourchar-radar"
    embed = payload["embeds"][0]
    assert "cool" in embed["title"]
    assert embed["description"]
    assert embed["color"] == 0x5865F2
    assert embed["fields"]


async def test_mentions_are_never_parsed(monkeypatch: pytest.MonkeyPatch, make_finding) -> None:  # type: ignore[no-untyped-def]
    """A finding must never be able to ping @everyone."""
    recorder = Recorder()
    notifier, http = _notifier(monkeypatch, recorder)

    await notifier.notify(make_finding("cool"))
    await http.aclose()

    assert recorder.last["allowed_mentions"] == {"parse": []}


async def test_4xx_raises_with_a_redacted_url(
    monkeypatch: pytest.MonkeyPatch, make_finding
) -> None:  # type: ignore[no-untyped-def]
    recorder = Recorder([httpx.Response(400, text="bad webhook")])
    notifier, http = _notifier(monkeypatch, recorder)

    with pytest.raises(RuntimeError) as excinfo:
        await notifier.notify(make_finding("cool"))
    await http.aclose()

    message = str(excinfo.value)
    assert "400" in message
    assert "secret-token" not in message, "the webhook token must never surface in errors"
    assert "bad webhook" in message


async def test_429_is_retried_after_the_requested_delay(
    monkeypatch: pytest.MonkeyPatch, make_finding
) -> None:  # type: ignore[no-untyped-def]
    recorder = Recorder([httpx.Response(429, json={"retry_after": 0.01}), httpx.Response(204)])
    notifier, http = _notifier(monkeypatch, recorder)

    await notifier.notify(make_finding("cool"))
    await http.aclose()

    assert len(recorder.payloads) == 2, "a 429 should be retried, not dropped"


async def test_throttle_and_report_notices(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = Recorder()
    notifier, http = _notifier(monkeypatch, recorder)

    await notifier.notify_throttle(2100.0, 0.25)
    await notifier.notify_started("wordlist:words4.txt(4)", 2418)
    stats = ScanStats(checked=10, taken=7, found=2, alerts_sent=1)
    await notifier.notify_report(stats)
    await http.aclose()

    assert len(recorder.payloads) == 3
    assert "Throttled" in recorder.payloads[0]["embeds"][0]["title"]
    assert "online" in recorder.payloads[1]["embeds"][0]["title"]
    assert "complete" in recorder.payloads[2]["embeds"][0]["title"]


async def test_requires_a_webhook_url() -> None:
    with pytest.raises(ValueError):
        WebhookNotifier(Settings(discord_webhook_url=""))


async def test_rejected_webhook_raises_an_actionable_error(
    monkeypatch: pytest.MonkeyPatch, make_finding
) -> None:  # type: ignore[no-untyped-def]
    """A wrong URL must fail loudly at startup, not hours into a scan."""
    recorder = Recorder([httpx.Response(404, json={"message": "Unknown Webhook", "code": 10015})])
    notifier, http = _notifier(monkeypatch, recorder)

    with pytest.raises(InvalidWebhook) as excinfo:
        await notifier.verify()
    await http.aclose()

    message = str(excinfo.value)
    assert "Unknown Webhook" in message
    assert "secret-token" not in message, "the token must never appear in the error"
    assert "--dry-run" in message, "the user needs a way forward"


async def test_verify_sends_a_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = Recorder()
    notifier, http = _notifier(monkeypatch, recorder)

    await notifier.verify()
    await http.aclose()

    assert len(recorder.payloads) == 1
    assert recorder.last["allowed_mentions"] == {"parse": []}
    assert "fourchar-radar" in recorder.last["embeds"][0]["title"]


def test_notifier_satisfies_the_protocol() -> None:
    assert isinstance(NullNotifier(), Notifier)


async def test_null_notifier_collects_instead_of_sending(make_finding) -> None:  # type: ignore[no-untyped-def]
    notifier = NullNotifier()
    await notifier.notify(make_finding("cool"))
    assert [f.name for f in notifier.delivered] == ["cool"]
    await notifier.close()


def test_redaction_covers_every_shape() -> None:
    assert "tok" not in redact_webhook(WEBHOOK)
    assert redact_webhook("") == "(unset)"
    for url in (
        "https://discord.com/api/webhooks/1/abc",
        "https://discordapp.com/api/webhooks/999999999999999999/xyz",
    ):
        redacted = redact_webhook(url)
        assert url.split("/")[-1] not in redacted
