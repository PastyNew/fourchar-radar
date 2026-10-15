"""End-to-end scanner behaviour with a fake checker."""

from __future__ import annotations

import asyncio

import pytest

from fourchar_radar.checking.ratelimit import AdaptiveRateLimiter
from fourchar_radar.checking.scanner import Scanner
from fourchar_radar.config import Settings
from fourchar_radar.errors import CheckFailed, InvalidUsername
from fourchar_radar.models import Tier, Verdict, utcnow
from fourchar_radar.notify.base import NullNotifier
from fourchar_radar.sources.wordlist import WordlistSource
from fourchar_radar.storage import Store

WEBHOOK = "https://discord.com/api/webhooks/1/t"


class FakeChecker:
    """Stands in for DiscordAvailabilityClient."""

    def __init__(
        self,
        free: set[str] | None = None,
        fail: set[str] | None = None,
        invalid: set[str] | None = None,
    ) -> None:
        self.free = free or set()
        self.fail = fail or set()
        self.invalid = invalid or set()
        self.seen: list[str] = []

    async def check(self, name: str) -> Verdict:
        self.seen.append(name)
        if name in self.invalid:
            raise InvalidUsername(name, "scripted")
        if name in self.fail:
            raise CheckFailed(name, 500, "scripted")
        return Verdict(name=name, available=name in self.free, checked_at=utcnow(), latency_ms=1.0)

    async def aclose(self) -> None:
        return None


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "discord_webhook_url": WEBHOOK,
        "rate_limit_rps": 20.0,
        "concurrency": 1,
        "limit": 0,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


class FixedSource:
    def __init__(self, names: list[str]) -> None:
        self._names = names
        self.total = len(names)
        self.label = "fixed"

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._names)


async def _scan(
    store: Store,
    settings: Settings,
    checker: FakeChecker,
    source: object,
    notifier: object | None = None,
):  # type: ignore[no-untyped-def]
    # Deliberately not settings.rate_limit_rps: tests must not sleep on pacing.
    limiter = AdaptiveRateLimiter(ceiling_rps=1000.0, start_rps=1000.0)
    scanner = Scanner(settings, store, checker, limiter, notifier or NullNotifier())  # type: ignore[arg-type]
    return scanner, await scanner.run(source)  # type: ignore[arg-type]


async def test_free_names_are_recorded_and_valued(store: Store) -> None:
    checker = FakeChecker(free={"cool", "face"})
    _, report = await _scan(store, _settings(), checker, FixedSource(["cool", "zzzq", "face"]))

    assert report.stats.checked == 3
    assert report.stats.found == 2
    assert report.stats.taken == 1
    assert {f.name for f in store.findings()} == {"cool", "face"}


async def test_alerts_only_fire_above_the_tier_floor(store: Store) -> None:
    notifier = NullNotifier()
    checker = FakeChecker(free={"cool"})  # "cool" is RARE by default

    scanner, report = await _scan(store, _settings(), checker, FixedSource(["cool"]), notifier)

    assert report.stats.alerts_sent == 1
    assert [f.name for f in notifier.delivered] == ["cool"]
    assert store.was_notified("cool")
    assert scanner.stats.free_rate == pytest.approx(1.0)


async def test_common_names_are_recorded_but_not_alerted(store: Store) -> None:
    notifier = NullNotifier()
    checker = FakeChecker(free={"qqzz"})  # not a word -> COMMON

    _, report = await _scan(
        store, _settings(alert_min_tier=Tier.EPIC), checker, FixedSource(["qqzz"]), notifier
    )

    assert report.stats.found == 1
    assert report.stats.alerts_sent == 0
    assert notifier.delivered == []
    # Still in the database — the record is the point, the alert is optional.
    assert len(store.findings()) == 1


async def test_failures_do_not_stop_the_scan(store: Store) -> None:
    checker = FakeChecker(free={"cool"}, fail={"boom"})
    _, report = await _scan(store, _settings(), checker, FixedSource(["boom", "cool", "also"]))

    assert report.stats.errors == 1
    assert report.stats.checked == 2
    assert {f.name for f in store.findings()} == {"cool"}


async def test_invalid_names_are_remembered_so_they_are_never_retried(store: Store) -> None:
    checker = FakeChecker(invalid={"bad!"})
    await _scan(store, _settings(), checker, FixedSource(["bad!"]))

    # Recorded as known, so a resumed scan skips it without a request.
    assert store.is_checked("bad!")


async def test_resume_skips_everything_already_checked(store: Store) -> None:
    names = ["cool", "face", "bean"]

    await _scan(store, _settings(), FakeChecker(free=set(names)), FixedSource(names))
    assert store.summary()["total"] == 3

    checker = FakeChecker(free=set(names))
    scanner, report = await _scan(store, _settings(), checker, FixedSource(names))

    assert checker.seen == [], "a resumed scan must not re-request known names"
    assert report.stats.skipped == 3
    assert report.stats.checked == 0


async def test_limit_caps_the_number_of_new_checks(store: Store) -> None:
    checker = FakeChecker()
    names = ["a1b1", "b2c2", "c3d3", "d4e4"]
    _, report = await _scan(store, _settings(limit=2), checker, FixedSource(names))
    assert report.stats.checked == 2


async def test_stop_ends_the_scan_early(store: Store) -> None:
    checker = FakeChecker()
    limiter = AdaptiveRateLimiter(ceiling_rps=1000.0, start_rps=1000.0)
    scanner = Scanner(_settings(), store, checker, limiter, NullNotifier())  # type: ignore[arg-type]

    class StopAfterTwo(FixedSource):
        def __iter__(self):  # type: ignore[no-untyped-def]
            yield "aaaa"
            yield "bbbb"
            scanner.stop()
            yield "cccc"

    report = await scanner.run(StopAfterTwo(["x"]))
    assert report.stopped_early is True
    assert report.stats.checked <= 2


async def test_alert_failure_does_not_break_the_scan(store: Store) -> None:
    class ExplodingNotifier:
        async def notify(self, finding: object) -> None:
            raise RuntimeError("webhook is down")

    checker = FakeChecker(free={"cool"})
    _, report = await _scan(store, _settings(), checker, FixedSource(["cool"]), ExplodingNotifier())

    assert report.stats.found == 1
    assert report.stats.alerts_sent == 0
    assert len(store.findings()) == 1  # the finding survives the failed alert


async def test_wordlist_source_runs_end_to_end(store: Store) -> None:
    source = WordlistSource(length=4)
    checker = FakeChecker(free={"cool"})
    _, report = await _scan(store, _settings(), checker, source)

    assert report.stats.checked == source.total
    assert report.stats.skipped == 0


async def test_concurrency_greater_than_one(store: Store) -> None:
    """Every candidate is still checked exactly once, with several workers."""
    names = [f"n{i:03d}" for i in range(40)]
    checker = FakeChecker(free={"n000", "n039"})
    _, report = await _scan(store, _settings(concurrency=4), checker, FixedSource(names))

    assert report.stats.checked == 40
    assert report.stats.found == 2
    assert sorted(checker.seen) == sorted(names)  # every candidate, none dropped


async def test_scanner_wires_the_stop_event_into_the_limiter(store: Store) -> None:
    """Without this, a throttle penalty makes the process un-stoppable."""
    limiter = AdaptiveRateLimiter(ceiling_rps=1000.0, start_rps=1000.0)
    Scanner(_settings(), store, FakeChecker(), limiter, NullNotifier())  # type: ignore[arg-type]
    assert limiter.stop_event is not None


async def test_no_notifier_is_fine(store: Store) -> None:
    limiter = AdaptiveRateLimiter(ceiling_rps=1000.0, start_rps=1000.0)
    scanner = Scanner(_settings(), store, FakeChecker(free={"cool"}), limiter, None)  # type: ignore[arg-type]
    report = await scanner.run(FixedSource(["cool"]))
    assert report.stats.found == 1
    assert report.stats.alerts_sent == 0


async def test_empty_source_completes(store: Store) -> None:
    _, report = await _scan(store, _settings(), FakeChecker(), FixedSource([]))
    assert report.stats.checked == 0


async def test_empty_queue_does_not_hang(store: Store) -> None:
    """Regression guard: queue.join() must resolve when the source is empty."""
    await asyncio.wait_for(_scan(store, _settings(), FakeChecker(), FixedSource([])), timeout=5)


# ------------------------------------------------------------ resume replay
class RecordingNotifier:
    def __init__(self) -> None:
        self.results: list[tuple[str, bool]] = []

    async def notify_result(self, name, available, price_label="", tier=""):  # type: ignore[no-untyped-def]
        self.results.append((name, available))

    async def notify_progress(self, *a, **k) -> None: ...  # type: ignore[no-untyped-def]
    async def notify(self, f) -> None: ...  # type: ignore[no-untyped-def]


async def test_resume_reports_names_it_skipped(store: Store) -> None:
    """A resumed scan that says nothing looks exactly like a hang.

    Everything already known is skipped for speed, so skipped names must be
    reported or the user watches a counter climb with no output at all.
    """
    names = ["aaaa", "bbbb", "cccc"]

    _, first = await _scan(store, _settings(), FakeChecker(free=set(names)), FixedSource(names))
    assert first.stats.checked == 3

    resumed = RecordingNotifier()
    _, report = await _scan(
        store, _settings(report_every=True), FakeChecker(), FixedSource(names), resumed
    )

    assert report.stats.skipped == 3
    assert [n for n, _ in resumed.results] == names
    assert all(available for _, available in resumed.results)


async def test_resume_replay_happens_only_once(store: Store) -> None:
    """A name must be announced once, not once per restart."""
    names = ["aaaa", "bbbb"]
    await _scan(store, _settings(), FakeChecker(free=set(names)), FixedSource(names))

    first = RecordingNotifier()
    await _scan(store, _settings(report_every=True), FakeChecker(), FixedSource(names), first)
    assert len(first.results) == 2

    second = RecordingNotifier()
    await _scan(store, _settings(report_every=True), FakeChecker(), FixedSource(names), second)
    assert second.results == [], "already reported names must stay quiet"


async def test_resume_keeps_taken_names_quiet_by_default(store: Store) -> None:
    """Taken names are not interesting unless the user asked for every check."""
    names = ["aaaa", "bbbb"]
    await _scan(store, _settings(), FakeChecker(free=set()), FixedSource(names))

    notifier = RecordingNotifier()
    await _scan(store, _settings(), FakeChecker(), FixedSource(names), notifier)
    assert notifier.results == []
