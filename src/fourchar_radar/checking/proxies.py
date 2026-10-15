"""Proxy pool.

The rate limit on Discord's availability endpoint is per *IP address*, not per
process. So the only real way to go faster is to have more than one egress.
This module holds a set of proxies, tracks which ones are currently penalised,
and hands out a healthy one per request.

What works, for the record:

* ``http://user:pass@host:port``   — HTTP CONNECT. Most common, easiest.
* ``https://...``                   — same, TLS to the proxy.
* ``socks5://user:pass@host:port`` — works, needs the ``socks`` extra.

What does *not* work:

* **MTProto.** That is Telegram's transport. It has nothing to do with
  Discord's API and a client speaking it will not connect. If a proxy vendor
  sells you "MTProto", that is a Telegram feature, not a general-purpose
  proxy.
* Rotating exit IPs *within* one proxy by request. Discord counts the exit IP;
  a proxy that hands out a random one per request is still one bucket in
  practice, and looks worse to the far end than a stable address.

A note on honesty: this multiplies throughput, and the reason it works is that
each proxy has its own budget. That is legitimate if the proxies are yours or
your provider's, and it is not legitimate if the point is to hammer an
endpoint you have no relationship with. The tool does one public read per
candidate name and nothing else; keep it that way.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

SUPPORTED_SCHEMES = ("http://", "https://", "socks5://", "socks5h://", "socks4://")


class ProxyError(ValueError):
    """A proxy URL that cannot work for this tool."""


def normalise(raw: str) -> str:
    """Validate and tidy a proxy URL, with a message aimed at the user."""
    url = raw.strip()
    if not url:
        raise ProxyError("empty proxy URL")

    if not url.lower().startswith(SUPPORTED_SCHEMES):
        raise ProxyError(
            f"{url!r} is not a supported proxy.\n"
            f"  use one of: {', '.join(SUPPORTED_SCHEMES)}\n"
            "  MTProto is a Telegram protocol and does not work here."
        )

    parts = urlsplit(url)
    if not parts.hostname:
        raise ProxyError(f"{url!r} has no host")

    # A bare "host:port" is the shape most vendors hand out; assume http.
    if "@" not in url and parts.scheme in ("http", "https"):
        return url
    return url


def parse_list(raw: str) -> tuple[str, ...]:
    """Parse a comma or newline separated proxy list.

    Accepts the shapes people actually paste: one URL, a list of URLs, a
    file-like blob, and ``#`` comments. Comment lines are dropped whole before
    tokenising, so a URL's own punctuation is never mistaken for a separator.
    """
    if not raw:
        return ()

    seen: dict[str, None] = {}
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        for token in stripped.replace(",", " ").split():
            seen.setdefault(normalise(token), None)
    return tuple(seen)


@dataclass(slots=True)
class ProxySlot:
    """One proxy and what is known about its current state."""

    url: str
    #: monotonic time until which this proxy should not be used.
    blocked_until: float = 0.0
    #: Consecutive transport failures; a proxy that cannot connect is parked.
    failures: int = 0
    successes: int = 0
    throttles: int = 0

    @property
    def is_socks(self) -> bool:
        return self.url.lower().startswith(("socks5://", "socks5h://", "socks4://"))

    @property
    def ready(self) -> bool:
        return self.blocked_until <= time.monotonic()

    def blocked_for(self) -> float:
        return max(0.0, self.blocked_until - time.monotonic())


@dataclass(slots=True)
class ProxyPool:
    """Round-robins over proxies, skipping the ones Discord has penalised."""

    slots: list[ProxySlot] = field(default_factory=list)
    _cursor: int = 0

    @classmethod
    def from_string(cls, raw: str) -> ProxyPool:
        return cls(slots=[ProxySlot(url=u) for u in parse_list(raw)])

    def __len__(self) -> int:
        return len(self.slots)

    def __bool__(self) -> bool:
        return bool(self.slots)

    @property
    def healthy(self) -> int:
        """How many proxies are usable right now.

        This is the number that multiplies throughput: each ready proxy has
        its own rate budget.
        """
        return sum(1 for s in self.slots if s.ready)

    def acquire(self) -> str | None:
        """Next usable proxy, or ``None`` when there are none configured.

        Round-robins rather than always picking the first ready one, so the
        load spreads instead of hammering whichever proxy sorts first.
        """
        if not self.slots:
            return None

        for offset in range(len(self.slots)):
            slot = self.slots[(self._cursor + offset) % len(self.slots)]
            if slot.ready:
                self._cursor = (self._cursor + offset + 1) % len(self.slots)
                return slot.url
        return None

    def _find(self, url: str) -> ProxySlot | None:
        return next((s for s in self.slots if s.url == url), None)

    def penalize(self, url: str, retry_after: float) -> None:
        """Park one proxy for *retry_after*; the others carry on."""
        slot = self._find(url)
        if slot is None:
            return
        slot.throttles += 1
        slot.blocked_until = max(slot.blocked_until, time.monotonic() + max(0.0, retry_after))
        log.warning(
            "proxy throttled",
            extra={
                "proxy": redact(slot.url),
                "retry_after_s": round(max(0.0, retry_after), 1),
                "healthy": self.healthy,
                "total": len(self.slots),
            },
        )

    def fail(self, url: str) -> None:
        """A transport failure: back this proxy off without blaming Discord."""
        slot = self._find(url)
        if slot is None:
            return
        slot.failures += 1
        # Exponential-ish, capped: a dead proxy should drop out of rotation
        # quickly but not vanish permanently on one blip.
        cooldown = min(600.0, 5.0 * (2 ** min(slot.failures, 6)))
        slot.blocked_until = time.monotonic() + cooldown
        log.warning(
            "proxy unreachable, pausing it",
            extra={
                "proxy": redact(slot.url),
                "failures": slot.failures,
                "paused_s": round(cooldown, 1),
                "healthy": self.healthy,
            },
        )

    def record_success(self, url: str) -> None:
        slot = self._find(url)
        if slot is None:
            return
        slot.successes += 1
        if slot.failures:
            slot.failures = 0

    def wait_time(self) -> float:
        """Seconds until at least one proxy is usable again."""
        if not self.slots:
            return 0.0
        return min(s.blocked_until for s in self.slots) - time.monotonic()

    def summary(self) -> str:
        if not self.slots:
            return "no proxies"
        ready = self.healthy
        total = len(self.slots)
        throttled = sum(s.throttles for s in self.slots)
        detail = f"{ready}/{total} ready"
        if throttled:
            detail += f", {throttled} throttles"
        return detail


def redact(url: str) -> str:
    """Hide credentials before a proxy URL reaches a log line."""
    parts = urlsplit(url)
    if not parts.username and not parts.password:
        return url
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return f"{parts.scheme}://***@{host}"
