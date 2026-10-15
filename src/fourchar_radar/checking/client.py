"""Availability checks against Discord's public username endpoint.

Endpoint (unauthenticated, used by the web client during registration):

    POST /api/v9/unique-username/username-attempt-unauthed
    {"username": "huhu", "global_name": null}  ->  {"taken": true|false}

Notes that shaped this client:

  * It is aggressively throttled. A short burst succeeds, then 429s arrive
    with ``retry_after`` in the thousands of seconds. All pacing lives in
    :mod:`.ratelimit`; this module just reports what happened.
  * A 429 is not a failure — it is the server asking us to wait. Those retries
    are budgeted separately from transport errors, because sitting out a
    35-minute penalty should never exhaust the error-retry budget.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

import httpx

from ..config import Settings
from ..errors import CheckFailed, InvalidUsername, Shutdown, Throttled
from ..models import Verdict, utcnow
from ..names import is_valid
from .proxies import ProxyPool
from .ratelimit import AdaptiveRateLimiter

log = logging.getLogger(__name__)

API_BASE = "https://discord.com/api/v9"
AVAILABILITY_PATH = "/unique-username/username-attempt-unauthed"

#: Error codes that mean "this name can never work" -> stop asking.
PERMANENT_FORM_CODES = frozenset(
    {
        "USERNAME_INVALID_CHARACTERS",
        "USERNAME_TOO_LONG",
        "BASE_TYPE_BAD_LENGTH",
        "USERNAME_INVALID",
    }
)


class DiscordAvailabilityClient:
    """Async, self-pacing availability checker.

    Usage::

        async with DiscordAvailabilityClient(settings, limiter) as client:
            verdict = await client.check("huhu")
    """

    def __init__(
        self,
        settings: Settings,
        limiter: AdaptiveRateLimiter,
        *,
        client: httpx.AsyncClient | None = None,
        pool: ProxyPool | None = None,
    ) -> None:
        self.settings = settings
        self.limiter = limiter
        self._owns_client = client is None
        self.pool = pool if pool is not None else ProxyPool.from_string(settings.proxy_list)
        # httpx binds a proxy to the client, so a pool means one client each.
        self._clients: dict[str | None, httpx.AsyncClient] = {}
        if client is not None:
            self._clients[None] = client
        elif self.pool:
            for slot in self.pool.slots:
                self._clients[slot.url] = self._build_client(settings, slot.url)
        else:
            self._clients[None] = self._build_client(settings)

    # ---------------------------------------------------------- lifecycle
    def _build_client(self, settings: Settings, proxy: str | None = None) -> httpx.AsyncClient:
        headers = {
            "Accept": "application/json",
            "Origin": "https://discord.com",
            "Referer": "https://discord.com/register",
            "User-Agent": settings.user_agent,
            "Accept-Language": "en-US,en;q=0.9",
        }
        kwargs: dict[str, object] = {
            "headers": headers,
            "timeout": httpx.Timeout(settings.timeout_s),
            # We do our own pacing; transport-level pooling stays generous.
            "limits": httpx.Limits(
                max_connections=max(4, settings.concurrency * 2),
                max_keepalive_connections=max(2, settings.concurrency),
            ),
            "follow_redirects": False,
        }
        if proxy:
            kwargs["proxy"] = proxy
        return httpx.AsyncClient(**kwargs)  # type: ignore[arg-type]

    def _client_for(self, proxy: str | None) -> httpx.AsyncClient:
        """The client bound to *proxy*; ``None`` means a direct connection."""
        try:
            return self._clients[proxy]
        except KeyError as exc:  # pragma: no cover - a bug, not a runtime case
            raise KeyError(f"no HTTP client for proxy {proxy!r}") from exc

    async def __aenter__(self) -> DiscordAvailabilityClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            for client in self._clients.values():
                await client.aclose()
        self._clients.clear()

    # ------------------------------------------------------------- check
    async def check(self, name: str) -> Verdict:
        """Return the availability of *name*.

        Raises:
            InvalidUsername: the name is permanently unacceptable.
            Throttled: throttled repeatedly, past the point of usefulness.
            CheckFailed: transport or server error that survived retries.
        """
        # Cheap local validation: never spend a request on a name we know
        # Discord will reject.
        if not is_valid(name):
            raise InvalidUsername(name, "failed local syntax validation")

        payload = {"username": name, "global_name": None}
        url = f"{API_BASE}{AVAILABILITY_PATH}"

        error_attempts = 0
        throttle_retries = 0

        while True:
            if not await self.limiter.acquire():
                raise Shutdown(name)

            # With a pool, every proxy that is not currently penalised gets a
            # turn; if all of them are, wait for the first to come back rather
            # than walking straight into another 429.
            if self.pool:
                proxy = self.pool.acquire()
                if proxy is None:
                    await asyncio.sleep(min(self.pool.wait_time(), 5.0) or 1.0)
                    continue
            else:
                proxy = None

            started = time.perf_counter()

            try:
                response = await self._client_for(proxy).post(url, json=payload)
            except httpx.HTTPError as exc:
                error_attempts += 1
                self.limiter.penalize_error()
                if proxy is not None:
                    # A dead proxy is the proxy's problem, not Discord's.
                    self.pool.fail(proxy)
                if error_attempts > self.settings.max_retries:
                    raise CheckFailed(name, None, str(exc)) from exc
                await self._sleep_backoff(error_attempts)
                continue

            latency_ms = (time.perf_counter() - started) * 1000

            # --- 200: the happy path -------------------------------------
            if response.status_code == 200:
                taken = self._parse_taken(name, response)
                self.limiter.record_success()
                if proxy is not None:
                    self.pool.record_success(proxy)
                return Verdict(
                    name=name,
                    available=not taken,
                    checked_at=utcnow(),
                    latency_ms=round(latency_ms, 1),
                )

            # --- 429: wait it out, don't count it as a failure -----------
            if response.status_code == 429:
                throttle_retries += 1
                retry_after = self._parse_retry_after(response)
                if throttle_retries > self.settings.max_retries:
                    raise Throttled(name, retry_after)
                if proxy is not None:
                    # Park *this* proxy only. The others are still good, so a
                    # pool keeps working while one IP serves out its penalty.
                    self.pool.penalize(proxy, retry_after)
                # Without a pool this is the only budget we have, so the gate
                # has to apply globally.
                if not self.pool:
                    self.limiter.penalize(retry_after)
                continue

            # --- 400: permanently invalid --------------------------------
            if response.status_code == 400:
                raise InvalidUsername(name, self._extract_form_error(response))

            # --- anything else: retry with backoff ------------------------
            error_attempts += 1
            self.limiter.penalize_error()
            if error_attempts > self.settings.max_retries:
                raise CheckFailed(name, response.status_code, response.text[:200].strip())
            log.warning(
                "unexpected response",
                extra={"candidate": name, "status": response.status_code},
            )
            await self._sleep_backoff(error_attempts)

    async def _sleep_backoff(self, attempt: int) -> None:
        """Exponential backoff with full jitter, interruptible by shutdown."""
        window = min(60.0, 2.0**attempt)
        try:
            await asyncio.sleep(random.uniform(window * 0.5, window))
        except asyncio.CancelledError:
            raise
        if self.limiter.stopping:
            raise Shutdown("backoff")

    # ----------------------------------------------------------- parsing
    @staticmethod
    def _parse_taken(name: str, response: httpx.Response) -> bool:
        try:
            payload = response.json()
        except ValueError as exc:
            raise CheckFailed(name, response.status_code, "non-JSON body") from exc
        if not isinstance(payload, dict) or "taken" not in payload:
            raise CheckFailed(name, response.status_code, f"unexpected shape: {payload!r}")
        return bool(payload["taken"])

    @staticmethod
    def _parse_retry_after(response: httpx.Response) -> float:
        """Seconds to wait, preferring the body over headers."""
        try:
            body = response.json()
            if isinstance(body, dict) and isinstance(body.get("retry_after"), (int, float)):
                return float(body["retry_after"])
        except ValueError:
            pass
        header = response.headers.get("x-ratelimit-reset-after") or response.headers.get(
            "retry-after"
        )
        if header:
            try:
                return float(header)
            except ValueError:
                pass
        return 5.0

    @staticmethod
    def _extract_form_error(response: httpx.Response) -> str:
        """Pull the human-readable reason out of a 50035 form error."""
        try:
            body = response.json()
        except ValueError:
            return response.text[:200].strip()
        errors = body.get("errors") if isinstance(body, dict) else None
        if isinstance(errors, dict):
            messages = []
            for field_errors in errors.values():
                for entry in field_errors.get("_errors", []):
                    message = entry.get("message") or entry.get("code")
                    if message:
                        messages.append(str(message))
            if messages:
                return "; ".join(messages)
        return str(body.get("message", ""))[:200] if isinstance(body, dict) else ""
