"""Proxy pool.

The pool is the only real throughput lever: the endpoint's rate limit is per
IP, so N usable proxies mean N budgets. What matters is that a throttled proxy
steps aside for the others instead of stalling the whole scan, and that a dead
one drops out of rotation without taking the rest with it.
"""

from __future__ import annotations

import time

import pytest

from fourchar_radar.checking.proxies import (
    ProxyError,
    ProxyPool,
    normalise,
    parse_list,
    redact,
)

A = "http://user:pass@one.example:8080"
B = "http://user:pass@two.example:8080"
C = "http://user:pass@three.example:8080"


# ----------------------------------------------------------------- parsing
def test_parses_a_comma_separated_list() -> None:
    assert parse_list(f"{A},{B}") == (A, B)


def test_parses_newlines_and_spaces() -> None:
    assert parse_list(f"{A}\n{B} {C}") == (A, B, C)


def test_ignores_comments_and_blanks() -> None:
    assert parse_list(f"# a comment\n\n{A}\n") == (A,)


def test_deduplicates() -> None:
    assert parse_list(f"{A},{A},{B}") == (A, B)


def test_empty_is_empty() -> None:
    assert parse_list("") == ()


@pytest.mark.parametrize(
    "url",
    [
        "http://host:8080",
        "https://host:8443",
        "socks5://host:1080",
        "socks5h://host:1080",
        "socks4://host:1080",
    ],
)
def test_accepts_the_schemes_that_actually_work(url: str) -> None:
    assert normalise(url) == url


def test_rejects_mtproto_with_an_explanation() -> None:
    """The user asked for MTProto. It is Telegram's protocol and will not work."""
    with pytest.raises(ProxyError) as excinfo:
        normalise("mtproto://secret@host:443")
    message = str(excinfo.value)
    assert "MTProto" in message
    assert "Telegram" in message
    # And it should point at what does work.
    assert "socks5" in message


def test_rejects_a_url_with_no_host() -> None:
    with pytest.raises(ProxyError):
        normalise("http://")


def test_redaction_hides_credentials() -> None:
    hidden = redact(A)
    assert "pass" not in hidden
    assert "one.example" in hidden


def test_redaction_leaves_a_credential_free_url_alone() -> None:
    assert redact("http://one.example:8080") == "http://one.example:8080"


# ------------------------------------------------------------------- pool
def test_empty_pool_is_falsy() -> None:
    assert not ProxyPool()


def test_acquire_returns_none_without_proxies() -> None:
    assert ProxyPool().acquire() is None


def test_acquire_round_robins() -> None:
    pool = ProxyPool.from_string(f"{A},{B}")
    picked = [pool.acquire() for _ in range(4)]
    assert picked == [A, B, A, B]


def test_a_throttled_proxy_is_skipped() -> None:
    pool = ProxyPool.from_string(f"{A},{B}")
    pool.penalize(A, 60)

    assert pool.acquire() == B
    assert pool.healthy == 1


def test_penalising_one_proxy_does_not_stall_the_others() -> None:
    """The whole point: one IP serving a penalty must not stop the scan."""
    pool = ProxyPool.from_string(f"{A},{B},{C}")
    pool.penalize(A, 3600)
    pool.penalize(B, 3600)

    assert pool.acquire() == C
    assert pool.healthy == 1


def test_all_throttled_returns_none_until_the_first_frees_up() -> None:
    pool = ProxyPool.from_string(f"{A},{B}")
    pool.penalize(A, 60)
    pool.penalize(B, 10)

    assert pool.acquire() is None
    assert 0 < pool.wait_time() <= 10


def test_penalty_expires() -> None:
    pool = ProxyPool.from_string(A)
    pool.penalize(A, 0.05)
    assert pool.acquire() is None
    time.sleep(0.08)
    assert pool.acquire() == A


def test_a_failing_proxy_is_paused() -> None:
    pool = ProxyPool.from_string(f"{A},{B}")
    pool.fail(A)
    assert pool.acquire() == B
    assert pool.healthy == 1


def test_success_clears_the_failure_count() -> None:
    pool = ProxyPool.from_string(f"{A},{B}")
    pool.fail(A)
    pool.record_success(A)
    assert pool.slots[0].failures == 0


def test_healthy_scales_with_the_pool() -> None:
    """Throughput scales with egress addresses, which is why a pool helps."""
    pool = ProxyPool.from_string(f"{A},{B},{C}")
    assert pool.healthy == 3
    pool.penalize(A, 60)
    assert pool.healthy == 2


def test_summary_is_human_readable() -> None:
    pool = ProxyPool.from_string(f"{A},{B}")
    assert "2/2 ready" in pool.summary()
    pool.penalize(A, 60)
    assert "1/2 ready" in pool.summary()


def test_unknown_url_is_ignored_by_penalise() -> None:
    pool = ProxyPool.from_string(A)
    pool.penalize("http://not-in-pool:1", 60)  # must not raise
