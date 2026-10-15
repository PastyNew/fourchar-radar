"""Adaptive request pacing.

Discord's unauthenticated username endpoint does not advertise a clean budget:
in practice it allows a short burst and then hands back a ``retry_after`` in
the *thousands of seconds*. The only honest way to deal with that is to treat
the limit as unknown and learn it at runtime.

This module implements AIMD (additive increase, multiplicative decrease),
the standard congestion-control shape:

  * every ``success_step`` successful checks nudge the rate up slightly
  * a 429 cuts the rate in half and hard-gates *all* workers until the
    server-supplied ``retry_after`` has elapsed

The configured RPS is treated as a ceiling, never a target.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)

#: Never throttle below this, or the radar silently stops making progress.
FLOOR_RPS = 0.002  # one request per ~8 minutes


@dataclass(slots=True)
class LimiterSnapshot:
    """Point-in-time view, surfaced by ``/status`` and the CLI."""

    rate_rps: float
    ceiling_rps: float
    blocked_for_s: float
    successes: int
    throttles: int


class AdaptiveRateLimiter:
    """Paces requests and adapts to server-side throttling.

    Thread-safe with respect to the asyncio event loop: no ``await`` occurs
    between reading and writing shared counters.
    """

    def __init__(
        self,
        ceiling_rps: float,
        *,
        floor_rps: float = FLOOR_RPS,
        success_step: int = 25,
        increase: float = 1.25,
        decrease: float = 0.5,
        jitter: float = 0.2,
        on_throttle: Callable[[float], None] | None = None,
    ) -> None:
        self.ceiling_rps = max(ceiling_rps, floor_rps)
        self._rate = self.ceiling_rps
        self._floor_rps = floor_rps
        self._success_step = success_step
        self._increase = increase
        self._decrease = decrease
        self._jitter = jitter
        self._on_throttle = on_throttle

        self._next_slot: float = 0.0
        self._blocked_until: float = 0.0
        self._successes = 0
        self._throttles = 0

        #: Assigned by :class:`~fourchar_radar.checking.scanner.Scanner`. Waiting
        #: out a 35-minute throttle penalty has to stay interruptible, otherwise
        #: SIGTERM could not stop the process for half an hour.
        self.stop_event: asyncio.Event | None = None

    # ------------------------------------------------------------ pacing
    @property
    def rate(self) -> float:
        """Current effective requests-per-second."""
        return self._rate

    @property
    def blocked_for(self) -> float:
        """Seconds until the gate opens (0.0 if not currently gated)."""
        return max(0.0, self._blocked_until - time.monotonic())

    @property
    def stopping(self) -> bool:
        """True if a stop has been requested and waits should abort."""
        return self.stop_event is not None and self.stop_event.is_set()

    async def acquire(self) -> bool:
        """Block until allowed to issue the next request.

        Returns ``False`` if a stop was requested while waiting, so callers can
        bail out of a long throttle penalty promptly instead of after it
        expires. Waits in short slices *and* races the stop event, so a
        30-minute penalty never delays shutdown by more than a moment.
        """
        while True:
            if self.stopping:
                return False

            now = time.monotonic()
            gate = max(self._next_slot, self._blocked_until)
            wait = gate - now

            if wait <= 0:
                interval = 1.0 / self._rate
                # Jitter avoids lockstep retries when many workers wake at once.
                spread = random.uniform(1.0 - self._jitter, 1.0 + self._jitter)
                self._next_slot = now + interval * spread
                return True

            slice_s = min(wait, 1.0)
            if self.stop_event is None:
                await asyncio.sleep(slice_s)
                continue
            # A TimeoutError just means the slice expired: loop and re-check.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stop_event.wait(), timeout=slice_s)

    # --------------------------------------------------------- feedback
    def record_success(self) -> None:
        """Called after every HTTP 2xx. Slowly earns back throughput."""
        self._successes += 1
        if self._successes % self._success_step == 0 and self._rate < self.ceiling_rps:
            self._rate = min(self.ceiling_rps, self._rate * self._increase)
            log.debug("rate limit raised", extra={"rate_rps": round(self._rate, 4)})

    def penalize(self, retry_after: float) -> float:
        """Record a 429 and gate every worker for *retry_after* seconds."""
        retry_after = max(0.0, retry_after)
        # Take the server at its word, but never trust it blindly forever.
        retry_after = min(retry_after, 60 * 60 * 6)

        self._throttles += 1
        self._rate = max(self._floor_rps, self._rate * self._decrease)
        self._blocked_until = max(self._blocked_until, time.monotonic() + retry_after)

        log.warning(
            "throttled by discord",
            extra={
                "retry_after_s": round(retry_after, 1),
                "new_rate_rps": round(self._rate, 4),
                "throttle_count": self._throttles,
            },
        )
        if self._on_throttle is not None:
            self._on_throttle(retry_after)
        return retry_after

    def penalize_error(self) -> None:
        """Record a transient (non-429) failure: back off, but don't crash."""
        self._rate = max(self._floor_rps, self._rate * 0.8)

    # ------------------------------------------------------------ misc
    def snapshot(self) -> LimiterSnapshot:
        return LimiterSnapshot(
            rate_rps=self._rate,
            ceiling_rps=self.ceiling_rps,
            blocked_for_s=self.blocked_for,
            successes=self._successes,
            throttles=self._throttles,
        )
