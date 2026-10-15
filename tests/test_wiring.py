"""Wiring: the pool and the limiter must be built consistently.

These two decisions are the whole reason a pool is worth running, so they are
pinned here: the budget scales with the number of ready egress addresses, and
a single IP still starts at the measured-safe rate.

Both front ends (the CLI and the menu) go through this module, so a test here
covers both.
"""

from __future__ import annotations

import pytest

from fourchar_radar.checking.proxies import ProxyPool
from fourchar_radar.checking.ratelimit import OBSERVED_SAFE_RPS
from fourchar_radar.config import Settings
from fourchar_radar.wiring import build_limiter, build_pool

WEBHOOK = "https://discord.com/api/webhooks/123/token"


def _settings(**kw: object) -> Settings:
    return Settings(discord_webhook_url=WEBHOOK, **kw)  # type: ignore[arg-type]


def _proxies(n: int) -> str:
    return ",".join(f"http://p{i}:8080" for i in range(n))


def test_no_proxies_means_no_pool() -> None:
    assert len(build_pool(_settings())) == 0


def test_pool_is_built_from_the_list() -> None:
    assert len(build_pool(_settings(proxy_list=_proxies(3)))) == 3


def test_pool_is_built_from_the_single_proxy_field() -> None:
    assert len(build_pool(_settings(proxy="http://one:8080"))) == 1


def test_pool_is_built_from_a_file(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "proxies.txt"
    path.write_text("http://a:1\nhttp://b:2\n", encoding="utf-8")
    assert len(build_pool(_settings(proxy_file=str(path)))) == 2


def test_a_missing_proxy_file_is_reported() -> None:
    from fourchar_radar.errors import ConfigError

    with pytest.raises(ConfigError, match="PROXY_FILE"):
        _settings(proxy_file="/nope/proxies.txt").proxy_sources()


def test_one_ip_starts_at_the_measured_safe_rate() -> None:
    """Starting at the ceiling is what cost several hours of 429s."""
    for ps in ("", _proxies(1)):
        limiter = build_limiter(_settings(proxy_list=ps), build_pool(_settings(proxy_list=ps)))
        assert limiter.rate == pytest.approx(OBSERVED_SAFE_RPS)


def test_the_budget_scales_with_the_pool() -> None:
    """N egress addresses means N budgets — this is the whole point."""
    for count in (2, 4, 10):
        settings = _settings(proxy_list=_proxies(count))
        limiter = build_limiter(settings, build_pool(settings))
        assert limiter.rate == pytest.approx(OBSERVED_SAFE_RPS * count)


def test_a_throttled_proxy_stops_counting() -> None:
    """Once a proxy is serving a penalty it has no budget to contribute."""
    settings = _settings(proxy_list=_proxies(4))
    pool = build_pool(settings)
    limiter = build_limiter(settings, pool)
    assert limiter.rate == pytest.approx(OBSERVED_SAFE_RPS * 4)

    pool.penalize(pool.slots[0].url, 600)
    pool.penalize(pool.slots[1].url, 600)
    assert build_limiter(settings, pool).rate == pytest.approx(OBSERVED_SAFE_RPS * 2)


def test_an_explicit_ceiling_is_never_lowered() -> None:
    """A pool only raises the floor; it must not cap what the user asked for."""
    settings = _settings(proxy_list="", rate_limit_rps=1.0)
    limiter = build_limiter(settings, build_pool(settings))
    assert limiter.ceiling_rps >= 1.0


def test_an_explicit_start_rate_wins() -> None:
    settings = _settings(proxy_list="", rate_limit_start_rps=0.5)
    limiter = build_limiter(settings, build_pool(settings))
    assert limiter.rate == pytest.approx(0.5)


def test_pool_and_limiter_agree_on_the_count() -> None:
    pool = ProxyPool.from_string(_proxies(5))
    assert pool.healthy == 5
