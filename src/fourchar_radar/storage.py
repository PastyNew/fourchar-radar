"""SQLite persistence.

Two responsibilities:

  1. **Resume.** The candidate stream (especially ``bruteforce``) is far larger
     than what any request budget can chew through, so every name that has been
     checked is recorded. On restart the scanner streams the same source again
     and skips what it has already seen — a local index lookup per candidate,
     which costs microseconds next to a network round-trip.

  2. **History.** Findings accumulate so you can see what is still free, what
     you already alerted on, and re-price the catalogue later.

Why SQLite rather than a JSON file: atomicity across restarts (no half-written
state after a kill), indexed lookups for the skip-set, and no dependency
beyond the stdlib.

This module is synchronous by design. SQLite calls here are single-row and
measured in tens of microseconds; wrapping them in a thread executor would add
more overhead than it saves.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .errors import StorageError
from .models import TIER_ORDER, Finding, Quote, Tier, Verdict

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS checks (
    name        TEXT PRIMARY KEY,
    available   INTEGER NOT NULL,
    checked_at  TEXT    NOT NULL,
    latency_ms  REAL    NOT NULL DEFAULT 0,
    price_usd   INTEGER,
    score       REAL,
    tier        TEXT,
    tier_rank   INTEGER,
    reasons     TEXT,
    notified_at TEXT
);

-- Serves the "best free names we know about" query.
CREATE INDEX IF NOT EXISTS idx_checks_available
    ON checks (tier_rank DESC, price_usd DESC) WHERE available = 1;

CREATE INDEX IF NOT EXISTS idx_checks_time ON checks (checked_at);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class Store:
    """Thread-safe façade over the radar's SQLite database."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        try:
            if self.path.parent and str(self.path.parent) not in ("", "."):
                self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                self.path,
                isolation_level=None,  # autocommit; we batch explicitly
                check_same_thread=False,
                timeout=30.0,
            )
        except (OSError, sqlite3.Error) as exc:
            raise StorageError(f"cannot open database at {self.path}: {exc}") from exc

        self._lock = threading.Lock()
        self._conn.row_factory = sqlite3.Row
        self._configure()
        self._migrate()

    # ---------------------------------------------------------- lifecycle
    def _configure(self) -> None:
        cur = self._conn
        # WAL keeps readers from blocking the writer (the bot polls for stats
        # while a scan is running).
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=30000")

    def _migrate(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(SCHEMA_VERSION),),
            )

    def close(self) -> None:
        """Close the connection. Safe to call more than once."""
        with self._lock, contextlib.suppress(sqlite3.ProgrammingError):
            self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -------------------------------------------------------------- meta
    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    # ------------------------------------------------------------ checks
    def is_checked(self, name: str) -> bool:
        """True if *name* already has a recorded verdict (the skip-set)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM checks WHERE name = ? LIMIT 1", (name,)
            ).fetchone()
        return row is not None

    def record(self, verdict: Verdict, quote: Quote | None = None) -> None:
        """Upsert a verdict and its valuation.

        A re-check that finds the name now taken clears the stale price, so the
        findings list can never show a name that is no longer free.
        """
        rank = TIER_ORDER[quote.tier] if quote else None
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO checks
                    (name, available, checked_at, latency_ms,
                     price_usd, score, tier, tier_rank, reasons)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    available   = excluded.available,
                    checked_at  = excluded.checked_at,
                    latency_ms  = excluded.latency_ms,
                    price_usd   = excluded.price_usd,
                    score       = excluded.score,
                    tier        = excluded.tier,
                    tier_rank   = excluded.tier_rank,
                    reasons     = excluded.reasons,
                    notified_at = CASE WHEN excluded.available = 0 THEN NULL
                                       ELSE checks.notified_at END
                """,
                (
                    verdict.name,
                    int(verdict.available),
                    _iso(verdict.checked_at),
                    verdict.latency_ms,
                    quote.price_usd if quote else None,
                    quote.score if quote else None,
                    str(quote.tier) if quote else None,
                    rank,
                    json.dumps(list(quote.reasons)) if quote else None,
                ),
            )

    def record_many(self, pairs: Iterable[tuple[Verdict, Quote | None]]) -> None:
        """Bulk upsert inside a single transaction."""
        rows = [
            (
                v.name,
                int(v.available),
                _iso(v.checked_at),
                v.latency_ms,
                q.price_usd if q else None,
                q.score if q else None,
                str(q.tier) if q else None,
                TIER_ORDER[q.tier] if q else None,
                json.dumps(list(q.reasons)) if q else None,
            )
            for v, q in pairs
        ]
        if not rows:
            return
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.executemany(
                    """
                    INSERT INTO checks
                        (name, available, checked_at, latency_ms,
                         price_usd, score, tier, tier_rank, reasons)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        available  = excluded.available,
                        checked_at = excluded.checked_at,
                        latency_ms = excluded.latency_ms,
                        price_usd  = excluded.price_usd,
                        score      = excluded.score,
                        tier       = excluded.tier,
                        tier_rank  = excluded.tier_rank,
                        reasons    = excluded.reasons,
                        notified_at = CASE WHEN excluded.available = 0 THEN NULL
                                           ELSE checks.notified_at END
                    """,
                    rows,
                )
            except sqlite3.Error:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def mark_notified(self, name: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE checks SET notified_at = ? WHERE name = ?", (_iso(datetime.now(UTC)), name)
            )

    def was_notified(self, name: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT notified_at FROM checks WHERE name = ?", (name,)
            ).fetchone()
        return bool(row and row["notified_at"])

    # ---------------------------------------------------------- findings
    def findings(self, limit: int = 25, min_tier: Tier = Tier.COMMON) -> list[Finding]:
        """Free names we know about, most valuable first."""
        min_rank = TIER_ORDER[min_tier]
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM checks
                 WHERE available = 1 AND tier_rank >= ?
                 ORDER BY tier_rank DESC, price_usd DESC, checked_at DESC
                 LIMIT ?
                """,
                (min_rank, limit),
            ).fetchall()
        return [self._to_finding(row) for row in rows]

    def unreported(self, min_tier: Tier, limit: int = 100) -> list[Finding]:
        """Free names that pass the tier filter and have never been alerted."""
        min_rank = TIER_ORDER[min_tier]
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM checks
                 WHERE available = 1 AND tier_rank >= ? AND notified_at IS NULL
                 ORDER BY tier_rank DESC, price_usd DESC
                 LIMIT ?
                """,
                (min_rank, limit),
            ).fetchall()
        return [self._to_finding(row) for row in rows]

    def pending_recheck(self, older_than_days: int, limit: int = 1000) -> list[str]:
        """Names last checked longer than *older_than_days* ago.

        Availability is a snapshot: a free name can be taken by someone else
        between runs, so long-lived findings are worth re-verifying.
        """
        cutoff = _iso(datetime.now(UTC) - timedelta(days=older_than_days))
        with self._lock:
            rows = self._conn.execute(
                "SELECT name FROM checks WHERE checked_at < ? ORDER BY checked_at LIMIT ?",
                (cutoff, limit),
            ).fetchall()
        return [row["name"] for row in rows]

    @staticmethod
    def _to_finding(row: sqlite3.Row) -> Finding:
        reasons = tuple(json.loads(row["reasons"])) if row["reasons"] else ()
        quote = Quote(
            name=row["name"],
            price_usd=int(row["price_usd"] or 0),
            score=float(row["score"] or 0.0),
            tier=Tier(row["tier"]) if row["tier"] else Tier.COMMON,
            reasons=reasons,
        )
        verdict = Verdict(
            name=row["name"],
            available=bool(row["available"]),
            checked_at=_parse_iso(row["checked_at"]),
            latency_ms=float(row["latency_ms"] or 0.0),
        )
        return Finding(verdict=verdict, quote=quote)

    # ------------------------------------------------------------- stats
    def summary(self) -> dict[str, int]:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT
                    COUNT(*)                                        AS total,
                    COALESCE(SUM(available), 0)                    AS free,
                    COALESCE(SUM(CASE WHEN notified_at IS NOT NULL
                                      THEN 1 ELSE 0 END), 0)      AS notified
                FROM checks
                """
            ).fetchone()
        return {"total": row["total"], "free": row["free"], "notified": row["notified"]}

    def counts_by_tier(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT tier, COUNT(*) AS n FROM checks "
                "WHERE available = 1 AND tier IS NOT NULL GROUP BY tier"
            ).fetchall()
        return {row["tier"]: row["n"] for row in rows}

    def prune_taken(self, older_than_days: int = 30) -> int:
        """Forget long-settled taken names so the DB stays small."""
        cutoff = _iso(datetime.now(UTC) - timedelta(days=older_than_days))
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM checks WHERE available = 0 AND checked_at < ?", (cutoff,)
            )
        return cur.rowcount

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM checks")

    def __iter__(self) -> Iterator[Finding]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM checks WHERE available = 1 ORDER BY tier_rank DESC, price_usd DESC"
            ).fetchall()
        return iter([self._to_finding(row) for row in rows])
