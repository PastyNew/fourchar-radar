"""Shared construction of the scan pipeline's services.

The CLI and the menu build exactly the same things — a proxy pool, a rate
limiter scaled to it, a checker — so any difference between the two is a bug
waiting to happen. This is the one place that wiring lives.
"""

from __future__ import annotations

from .checking.proxies import ProxyPool
from .checking.ratelimit import OBSERVED_SAFE_RPS, AdaptiveRateLimiter
from .config import Settings


def build_pool(settings: Settings) -> ProxyPool:
    """Proxies from the list, the file, or the single-proxy setting."""
    return ProxyPool.from_string(settings.proxy_sources())


def build_limiter(settings: Settings, pool: ProxyPool) -> AdaptiveRateLimiter:
    """Scale the pacing budget by how many egress addresses are actually ready.

    The endpoint's limit is per IP, so one IP is one budget. N ready proxies
    means N budgets, and pacing them as if there were one would leave the extra
    capacity unused — which is the whole reason to run a pool.

    An explicit ceiling still wins when it is higher, so ``--rps`` is never
    silently ignored; the pool only ever raises the floor.
    """
    # Each ready proxy is one egress address, and the limit is per address, so
    # the sustainable rate is the single-IP rate times how many are ready.
    per_ip = OBSERVED_SAFE_RPS
    budget = per_ip * max(1, pool.healthy)

    # An explicit --rps still wins when it is higher, so it is never silently
    # ignored. The pool only ever raises the floor.
    ceiling = max(settings.rate_limit_rps, budget)
    start = settings.rate_limit_start_rps or budget
    # An explicit start above the ceiling would be silently clipped by the
    # limiter, so asking for it raises the ceiling too.
    ceiling = max(ceiling, start)

    return AdaptiveRateLimiter(
        ceiling,
        # Start at the pool's own budget, not the single-IP rate: starting one
        # proxy's worth low would leave the rest of the pool idle for the first
        # hour of a multi-day scan.
        start_rps=start,
    )
