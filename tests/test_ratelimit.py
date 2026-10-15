"""Adaptive rate limiter: AIMD behaviour and interruptible waits."""

from __future__ import annotations

import asyncio
import time

import pytest

from fourchar_radar.checking.ratelimit import AdaptiveRateLimiter


async def test_acquire_returns_true_when_open() -> None:
    limiter = AdaptiveRateLimiter(ceiling_rps=1000)
    assert await limiter.acquire() is True


async def test_penalize_gates_and_halves_the_rate() -> None:
    limiter = AdaptiveRateLimiter(ceiling_rps=1.0)
    original = limiter.rate

    limiter.penalize(30.0)

    assert limiter.rate == pytest.approx(original * 0.5)
    assert limiter.blocked_for > 25.0


async def test_blocked_acquire_waits_until_the_penalty_expires() -> None:
    limiter = AdaptiveRateLimiter(ceiling_rps=1000)
    limiter.penalize(0.25)

    started = time.monotonic()
    assert await limiter.acquire() is True
    elapsed = time.monotonic() - started

    assert elapsed >= 0.2, "must actually respect the penalty"


async def test_repeated_penalties_take_the_longest_not_the_sum() -> None:
    limiter = AdaptiveRateLimiter(ceiling_rps=1.0)
    limiter.penalize(30.0)
    limiter.penalize(10.0)  # shorter, must not shorten the existing gate
    assert limiter.blocked_for > 25.0


async def test_rate_never_falls_below_the_floor() -> None:
    limiter = AdaptiveRateLimiter(ceiling_rps=10.0, floor_rps=0.05)
    for _ in range(200):
        limiter.penalize(1.0)
    assert limiter.rate >= 0.05


async def test_successes_earn_back_throughput_but_never_past_the_ceiling() -> None:
    limiter = AdaptiveRateLimiter(ceiling_rps=4.0, success_step=2)
    start = limiter.rate

    limiter.penalize(1.0)  # knock it down
    assert limiter.rate < start

    for _ in range(100):
        limiter.record_success()

    assert limiter.rate <= limiter.ceiling_rps


async def test_acquire_returns_false_when_stop_is_requested() -> None:
    """The bug this guards: a 30-minute penalty used to be un-interruptible."""
    stop = asyncio.Event()
    limiter = AdaptiveRateLimiter(ceiling_rps=1000)
    limiter.stop_event = stop
    limiter.penalize(3600.0)

    stop.set()
    started = time.monotonic()
    assert await limiter.acquire() is False
    assert time.monotonic() - started < 1.0


async def test_acquire_wakes_as_soon_as_stop_is_set_from_another_task() -> None:
    stop = asyncio.Event()
    limiter = AdaptiveRateLimiter(ceiling_rps=1000)
    limiter.stop_event = stop
    limiter.penalize(3600.0)

    async def stopper() -> None:
        await asyncio.sleep(0.1)
        stop.set()

    task = asyncio.create_task(stopper())
    started = time.monotonic()
    assert await limiter.acquire() is False
    assert time.monotonic() - started < 2.0
    await task


async def test_snapshot_reports_throttles() -> None:
    limiter = AdaptiveRateLimiter(ceiling_rps=1.0)
    limiter.penalize(5.0)
    limiter.penalize(5.0)
    snap = limiter.snapshot()
    assert snap.throttles == 2
    assert snap.rate_rps < snap.ceiling_rps


async def test_on_throttle_callback_fires() -> None:
    seen: list[float] = []
    limiter = AdaptiveRateLimiter(ceiling_rps=1.0, on_throttle=seen.append)
    limiter.penalize(12.0)
    assert seen == [12.0]


async def test_absurd_retry_after_is_clamped() -> None:
    limiter = AdaptiveRateLimiter(ceiling_rps=1.0)
    returned = limiter.penalize(999_999.0)
    assert returned <= 6 * 60 * 60
