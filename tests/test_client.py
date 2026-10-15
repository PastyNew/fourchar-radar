"""HTTP client policy: 200 / 400 / 429 / 5xx handling, with a mock transport.

No real network is touched. ``httpx.MockTransport`` lets each case script the
exact response Discord would send.
"""

from __future__ import annotations

import httpx
import pytest

from fourchar_radar.checking.client import DiscordAvailabilityClient
from fourchar_radar.checking.ratelimit import AdaptiveRateLimiter
from fourchar_radar.config import Settings
from fourchar_radar.errors import CheckFailed, InvalidUsername, Shutdown, Throttled

WEBHOOK = "https://discord.com/api/webhooks/1/t"


def _client(
    settings: Settings, handler: httpx.MockTransport
) -> tuple[DiscordAvailabilityClient, httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=handler)
    limiter = AdaptiveRateLimiter(ceiling_rps=1000)
    return DiscordAvailabilityClient(settings, limiter, client=http), http


def _settings(**overrides: object) -> Settings:
    base = {
        "discord_webhook_url": WEBHOOK,
        "rate_limit_rps": 20.0,
        "max_retries": 2,
        "timeout_s": 5.0,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


async def test_available_name(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path.endswith("/unique-username/username-attempt-unauthed")
        assert b'"username":"huhu"' in request.content.replace(b" ", b"")
        return httpx.Response(200, json={"taken": False})

    client, http = _client(settings, httpx.MockTransport(handler))
    verdict = await client.check("huhu")
    await http.aclose()

    assert verdict.available is True
    assert verdict.name == "huhu"
    assert verdict.latency_ms >= 0


async def test_taken_name(settings: Settings) -> None:
    handler = httpx.MockTransport(lambda r: httpx.Response(200, json={"taken": True}))
    client, http = _client(settings, handler)
    verdict = await client.check("cool")
    await http.aclose()
    assert verdict.available is False


async def test_local_validation_short_circuits_without_a_request(settings: Settings) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"taken": False})

    client, http = _client(settings, httpx.MockTransport(handler))
    with pytest.raises(InvalidUsername):
        await client.check("NOT VALID")
    await http.aclose()

    assert calls == 0, "must never spend a request on a name we know is invalid"


async def test_form_error_is_surfaced_as_invalid(settings: Settings) -> None:
    body = {
        "message": "Invalid Form Body",
        "code": 50035,
        "errors": {
            "username": {
                "_errors": [
                    {
                        "code": "BASE_TYPE_BAD_LENGTH",
                        "message": "Must be between 2 and 32 in length.",
                    }
                ]
            }
        },
    }
    client, http = _client(settings, httpx.MockTransport(lambda r: httpx.Response(400, json=body)))

    with pytest.raises(InvalidUsername) as excinfo:
        await client.check("huhu")
    await http.aclose()
    assert "between 2 and 32" in str(excinfo.value)


async def test_429_is_retried_not_failed(settings: Settings) -> None:
    """A 429 is the server saying 'wait', and must not burn the error budget."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429, json={"message": "slow down", "retry_after": 0.05, "global": False}
            )
        return httpx.Response(200, json={"taken": False})

    client, http = _client(settings, httpx.MockTransport(handler))
    verdict = await client.check("huhu")
    await http.aclose()

    assert verdict.available is True
    assert calls == 2


async def test_persistent_429_raises_throttled(settings: Settings) -> None:
    handler = httpx.MockTransport(lambda r: httpx.Response(429, json={"retry_after": 0.01}))
    client, http = _client(settings, handler)

    with pytest.raises(Throttled):
        await client.check("huhu")
    await http.aclose()


async def test_retry_after_falls_back_to_the_header(settings: Settings) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"retry-after": "0.05"}, text="slow down")
        return httpx.Response(200, json={"taken": True})

    client, http = _client(settings, httpx.MockTransport(handler))
    verdict = await client.check("huhu")
    await http.aclose()
    assert verdict.available is False


async def test_server_error_is_retried_then_raised(settings: Settings) -> None:
    handler = httpx.MockTransport(lambda r: httpx.Response(503, text="nope"))
    client, http = _client(settings, handler)

    with pytest.raises(CheckFailed):
        await client.check("huhu")
    await http.aclose()


async def test_transient_500_then_success(settings: Settings) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(500, text="oops")
        return httpx.Response(200, json={"taken": False})

    client, http = _client(settings, httpx.MockTransport(handler))
    verdict = await client.check("huhu")
    await http.aclose()
    assert verdict.available is True


async def test_unexpected_body_shape_raises(settings: Settings) -> None:
    handler = httpx.MockTransport(lambda r: httpx.Response(200, json={"wat": 1}))
    client, http = _client(settings, handler)
    with pytest.raises(CheckFailed):
        await client.check("huhu")
    await http.aclose()


async def test_non_json_body_raises(settings: Settings) -> None:
    handler = httpx.MockTransport(lambda r: httpx.Response(200, text="<html>"))
    client, http = _client(settings, handler)
    with pytest.raises(CheckFailed):
        await client.check("huhu")
    await http.aclose()


async def test_stop_during_a_penalty_aborts_promptly(settings: Settings) -> None:
    """Guards the hang this project actually shipped during development."""
    import asyncio

    limiter = AdaptiveRateLimiter(ceiling_rps=1000)
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(429, json={"retry_after": 3600}))
    )
    client = DiscordAvailabilityClient(settings, limiter, client=http)
    limiter.stop_event = asyncio.Event()

    async def stopper() -> None:
        await asyncio.sleep(0.1)
        limiter.stop_event.set()

    task = asyncio.create_task(stopper())
    with pytest.raises(Shutdown):
        await client.check("huhu")
    await task
    await http.aclose()


async def test_default_client_sends_web_client_headers(settings: Settings) -> None:
    """The real (non-injected) client must look like the registration flow."""
    client = DiscordAvailabilityClient(settings, AdaptiveRateLimiter(ceiling_rps=1.0))
    try:
        headers = client._client.headers
        assert headers["user-agent"].startswith("fourchar-radar")
        assert headers["origin"] == "https://discord.com"
        assert "register" in headers["referer"]
        assert "json" in headers["accept"]
    finally:
        await client.aclose()


async def test_proxy_is_configured_when_set(settings: Settings) -> None:
    configured = settings.model_copy(update={"proxy": "http://127.0.0.1:9"})
    client = DiscordAvailabilityClient(configured, AdaptiveRateLimiter(1.0))
    await client.aclose()
