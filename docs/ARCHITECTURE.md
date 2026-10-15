# Architecture

A short map of the moving parts and the reasoning behind them. The README
covers usage; this covers *why the code is shaped this way*.

```
                 ┌───────────────────────────────┐
   config  ─────▶│ settings.Settings (validated) │
   (.env)        └──────────────┬────────────────┘
                                │ validated once, at startup
                                ▼
   ┌──────────────┐      ┌───────────────┐
   │  sources/    │      │  ratelimit    │
   │  wordlist    │─────▶│  AdaptiveRate │  ◀── AIMD feedback from
   │  bruteforce  │ names│  Limiter      │      every 429 / 200
   │  file        │      └───────┬───────┘
   │  stdin       │              │ acquire() / penalize()
   └──────────────┘              ▼
                         ┌───────────────┐        ┌──────────────┐
                         │  client.py    │───────▶│ Discord API  │
                         │  200/400/429/ │        │ (public)     │
                         │  5xx policy   │        └──────────────┘
                         └───────┬───────┘
                                 │ Verdict
                                 ▼
                         ┌───────────────┐        ┌──────────────┐
                         │  estimator    │───────▶│  models.Quote│
                         │  pricing/     │        └──────────────┘
                         └───────┬───────┘
                                 │ Finding
                    ┌────────────┼────────────┐
                    ▼                         ▼
             ┌─────────────┐           ┌─────────────┐
             │ storage.py  │           │  notify/    │
             │  SQLite     │           │  webhook    │
             │  skip-set   │           │  bot        │
             └─────────────┘           └─────────────┘
```

## The constraint that shapes everything

Discord's unauthenticated availability endpoint is throttled far harder than
it documents. Measured: ~16 successful requests, then `429` with
`retry_after: 2114` (35 minutes). 35 minutes later, the very first request
drew a *fresh* ~35-minute penalty. Randomised cookies get you a Cloudflare
challenge rather than a separate bucket.

That is burst-and-penalise, not a slow trickle, so tuning the rate down does
not help. Effective sustained throughput is **tens of checks per hour**
against a search space of 37⁴ = 1,874,161 possible four-character names. The
bundled wordlist is a multi-day job; a full sweep is not reachable. Every
design decision below follows from that.

## Decisions

### Candidates are ordered best-first, not exhaustively

`BruteforceSource` emits pure-letter combinations before anything with a digit
or separator. `WordlistSource` is a curated list of real English words plus
modern internet slang. Since budget is the scarce resource, the scanner
always spends it on names most likely to be worth something.

### Skip-set, not a queue

The 1.87M candidates are **streamed**, never materialised. Each one gets a
single indexed SQLite lookup (`is_checked`) before any request is spent. That
turns restart-and-resume into a property of the data rather than something the
process has to remember, and costs microseconds next to a network round-trip.

### 429 is not a failure

A 429 means "wait", not "broken". It is budgeted on a separate counter from
transport errors, so sitting out a 35-minute penalty can never exhaust the
retry budget. `AdaptiveRateLimiter` then gates *all* workers on the longest
`retry_after` seen, halves the effective rate, and only creeps back up after a
sustained run of successes (AIMD — the same shape as TCP congestion control,
because the problem is the same shape).

### Waits are interruptible

An early version blocked in `asyncio.sleep()` for the full penalty, so
`SIGTERM` did nothing for half an hour and `docker stop` effectively hung. The
limiter now races the scanner's stop event and returns `False`, which the
client turns into `Shutdown`. Covered by
`test_stop_during_a_penalty_aborts_promptly`.

### Alerts can never kill a scan

Every notification failure is caught, logged, and swallowed. The finding is
already in SQLite; losing the alert degrades the experience, losing the scan
loses the work. `notified_at` is only set after a confirmed delivery, so a
failed alert is retried on the next pass.

### Pricing is a transparent heuristic

`PriceModel` is a flat bag of USD weights, and every term that fired is
returned in `Quote.reasons`. It is not a market prediction and the docs say so
in three places. The point is triage — "should this interrupt me?" — and being
able to explain the number is what makes it trustworthy at that job.

### Webhook first, bot second

The primary path needs one string. A bot account means OAuth, permissions,
intents, slash-command registration, and a second token to leak — all of it in
exchange for commands nobody needs to run every day. So `notify/webhook.py` is
the default, `notify/bot.py` is an extra, and both satisfy the same one-method
`Notifier` protocol. The bot reuses the identical checker, limiter, store and
pricer; it is a control surface, not a second implementation.

### Storage is SQLite, synchronously

SQLite is stdlib, gives atomicity across an ungraceful kill, and indexes the
skip-set. The access pattern is single-row reads and inserts measured in tens
of microseconds, so wrapping it in a thread executor would add more overhead
than it removes. `isolation_level=None` (autocommit) with explicit `BEGIN`/`COMMIT`
around bulk writes keeps that predictable.

## Testing notes

The suite targets the paths that actually break:

* throttling, and the interplay of 429 retries with the error budget
* shutdown during a throttle penalty
* resume semantics and alert re-delivery
* invalid names being recorded so they are never re-requested
* alert delivery failure not aborting a scan
* estimator *ordering* rather than exact prices, so the weights stay tunable

No test touches the network (`httpx.MockTransport`) and none of them sleep on
a real rate limit.

### The database lives in the OS data directory, not the cwd

`state/radar.db` looks tidy and then breaks the first time the radar is started
from a systemd unit, a cron job, or a second terminal tab — each with a
different working directory, each silently creating a *different* database and
therefore forgetting everything. `platforms.default_db_path()` resolves XDG on
Linux, `~/Library/Application Support` on macOS and `LOCALAPPDATA` on Windows,
and creates the parent on the way. A read-only `HOME` (some containers) falls
back to the cwd rather than refusing to start.

### Cross-platform means the boring things too

* The webhook is a **positional argument**, not a config file. `fourchar-radar
  run <url>` is the whole setup, and the common case needs nothing on disk.
  `.env` still works and is simply lower precedence.
* `loop.add_signal_handler` **does not exist on Windows**, and neither does
  SIGTERM; there Ctrl-C arrives as `KeyboardInterrupt`. The registration is
  skipped and the run is cancelled and unwound instead, so the summary still
  prints and the database still closes on every platform.
* The installer prefers a **virtualenv** and falls back to a `--user` install
  where `ensurepip` is missing (Debian without `python3-venv`, minimal images)
  or where PEP 668 blocks it (Debian 12+, Fedora 40+). It never needs root.

## Deliberately not built

* **Automatic claiming or registration.** Out of scope by design.
* **Rate-limit evasion** (cookie/proxy rotation to dodge penalties). It gets
  IPs blocked and makes the penalty worse. `RADAR_PROXY` exists to give a
  *legitimate* extra egress, not to multiply buckets.
* **A web UI.** The Discord channel is the interface.
* **Cloud sync.** A local SQLite file is the right size of problem for this.
