"""Scan orchestration: candidate stream → availability → valuation → alert.

Deliberately a **sequential, pull-based** pipeline with a bounded hand-off to
a small worker pool. Two reasons:

  * Throughput is dictated by Discord's throttle, not by local concurrency.
    Firing 16 parallel requests at a 1-request-per-500s budget just makes the
    429s arrive sooner.
  * Every candidate passes through SQLite (``is_checked``) before any request
    is spent, so the skip-set stays authoritative even across restarts.

Failure handling is the important part: an invalid name is dropped forever, a
throttle is waited out, a transport error is retried — and none of them stop
the scan.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from ..config import Settings
from ..errors import CheckFailed, InvalidUsername, Shutdown, Throttled
from ..models import Finding, ScanStats, Verdict, utcnow
from ..pricing.estimator import estimate
from ..sources.base import CandidateSource
from ..storage import Store
from .client import DiscordAvailabilityClient
from .ratelimit import AdaptiveRateLimiter, LimiterSnapshot

log = logging.getLogger(__name__)

#: Log a progress line at most this often.
PROGRESS_INTERVAL_S = 60.0


@dataclass(slots=True)
class ScanReport:
    """Everything the CLI needs to render a summary."""

    stats: ScanStats
    limiter: LimiterSnapshot
    stopped_early: bool = False


class Scanner:
    """Runs one scan session against a candidate source."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        client: DiscordAvailabilityClient,
        limiter: AdaptiveRateLimiter,
        notifier: object | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.client = client
        self.limiter = limiter
        #: Anything exposing ``async def notify(finding) -> None``.
        self.notifier = notifier
        self.stats = ScanStats()
        self._stop = asyncio.Event()
        self._last_alert_at = 0.0
        self._last_progress = 0.0
        # Long throttle penalties must not delay shutdown.
        if limiter.stop_event is None:
            limiter.stop_event = self._stop

    # ------------------------------------------------------------ control
    def stop(self) -> None:
        """Ask the scan to finish the current check and shut down cleanly."""
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    async def wait_for_stop(self) -> None:
        """Block until :meth:`stop` is called.

        Lets callers sleep without needing an ``asyncio.Event`` of their own,
        and keeps ctrl-c responsive during long idle periods.
        """
        await self._stop.wait()

    @property
    def is_alerting(self) -> bool:
        return self.notifier is not None

    # ---------------------------------------------------------------- run
    async def run(self, source: CandidateSource) -> ScanReport:
        """Consume *source* until exhausted, limited, or stopped."""
        log.info(
            "scan starting",
            extra={
                "source": source.label,
                "candidates": source.total if source.total is not None else "?",
                "rate_ceiling_rps": self.settings.rate_limit_rps,
                "alerts": self.is_alerting,
            },
        )
        store_meta = {"source": source.label, "length": str(self.settings.length)}
        self.store.set_meta("last_scan", store_meta["source"])
        self.store.set_meta("last_scan_started", self.stats.started_at.isoformat())

        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=max(2, self.settings.concurrency * 4))
        workers = [
            asyncio.create_task(self._worker(queue), name=f"radar-worker-{i}")
            for i in range(self.settings.concurrency)
        ]

        try:
            await self._feed(source, queue)
        finally:
            await queue.join()
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

        limiter_state = self.limiter.snapshot()
        log.info("scan finished", extra={"stats": self.stats.summary()})
        return ScanReport(stats=self.stats, limiter=limiter_state, stopped_early=self.stopping)

    async def _feed(self, source: CandidateSource, queue: asyncio.Queue[str]) -> None:
        """Push fresh candidates onto the queue, honouring the resume set."""
        processed = 0
        limit = self.settings.limit

        for name in source:
            if self.stopping:
                break
            if limit and processed >= limit:
                break

            if self.store.is_checked(name):
                self.stats.skipped += 1
                continue

            # `put` in slices so a stop request is noticed even when the queue
            # is full and every worker is parked in the rate limiter.
            try:
                await asyncio.wait_for(queue.put(name), timeout=1.0)
            except TimeoutError:
                continue
            processed += 1

            if processed % 25 == 0:
                self._log_progress()

    async def _worker(self, queue: asyncio.Queue[str]) -> None:
        """Pull names until the queue is joined and the scan winds down."""
        while True:
            name = await queue.get()
            try:
                await self._process(name)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a worker must never die quietly
                log.exception("unhandled error processing %r", name)
                self.stats.errors += 1
            finally:
                queue.task_done()

    # ---------------------------------------------------------- pipeline
    async def _process(self, name: str) -> None:
        try:
            verdict = await self.client.check(name)
        except InvalidUsername as exc:
            # Permanently useless: remember it so we never ask again.
            log.debug("skipped invalid name", extra={"candidate": name, "detail": exc.detail})
            self.store.record(Verdict(name=name, available=False, checked_at=utcnow()))
            self.stats.skipped += 1
            return
        except Throttled as exc:
            log.error(
                "giving up on %s: still throttled",
                exc.name,
                extra={"retry_after_s": exc.retry_after},
            )
            self.stats.errors += 1
            return
        except Shutdown:
            return  # normal stop, not a failure
        except CheckFailed as exc:
            log.warning("check failed", extra={"candidate": name, "detail": str(exc)})
            self.stats.errors += 1
            return

        self.stats.checked += 1
        if not verdict.available:
            self.stats.taken += 1
            self.store.record(verdict, None)
            self._log_progress()
            return

        quote = estimate(verdict.name)
        finding = Finding(verdict=verdict, quote=quote)
        self.store.record(verdict, quote)
        self.stats.found += 1

        log.info(
            "free username found",
            extra={
                "candidate": verdict.name,
                "price": quote.price_label,
                "tier": str(quote.tier),
            },
        )
        await self._maybe_alert(finding)
        self._log_progress()

    async def _maybe_alert(self, finding: Finding) -> None:
        """Send an alert if the find clears the tier filter and cooldown."""
        if self.notifier is None:
            return
        if finding.tier.rank < self.settings.min_tier_rank:
            return
        if self.settings.alert_cooldown_s:
            elapsed = time.monotonic() - self._last_alert_at
            if elapsed < self.settings.alert_cooldown_s:
                return

        try:
            await self.notifier.notify(finding)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - alerting must never kill a scan
            log.exception("failed to deliver alert for %s", finding.name)
            return

        self._last_alert_at = time.monotonic()
        self.stats.alerts_sent += 1
        self.store.mark_notified(finding.name)

    # --------------------------------------------------------- reporting
    def _log_progress(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_progress < PROGRESS_INTERVAL_S:
            return
        self._last_progress = now
        log.info(
            "progress",
            extra={
                "checked": self.stats.checked,
                "free": self.stats.found,
                "skipped": self.stats.skipped,
                "errors": self.stats.errors,
                "rate_rps": round(self.limiter.rate, 4),
                "blocked_s": round(self.limiter.blocked_for, 1),
            },
        )
