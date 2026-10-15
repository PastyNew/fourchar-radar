# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] — 2026-10-14

First public release.

### Added

- **Availability checking** against Discord's public username endpoint
  (`POST /api/v9/unique-username/username-attempt-unauthed`), with a distinct
  policy for 200 / 400 / 429 / 5xx.
- **Adaptive rate limiting** (AIMD: additive increase, multiplicative
  decrease). The configured RPS is a ceiling, never a target — the limiter
  learns whatever rate the endpoint actually tolerates.
- **Candidate sources**: `wordlist` (bundled 2,418 four-letter words plus
  internet slang), `bruteforce` (all 37⁴ combinations, best-first),
  `file` and `stdin`.
- **Heuristic price estimation** with explainable per-term reasons, mapped onto
  five tiers (`common` → `legendary`).
- **Webhook alerting** — a URL is the only setup required. No bot account.
- **Optional bot mode** (`[bot]` extra) with `/price`, `/top`, `/status`,
  `/scan start` and `/scan stop`.
- **SQLite persistence** doubling as a resume skip-set, so stopping and
  restarting costs nothing and never re-spends a request.
- **CLI**: `run`, `price`, `top`, `status`, `sources`, `export`, `prune`, `bot`.
  Global flags work before or after the subcommand.
- **Graceful shutdown** — `SIGINT`/`SIGTERM` stop after the current check.
- **Docker image and Compose stack**, plus a systemd example.
- **CI** running ruff, `mypy --strict` and pytest on Python 3.11–3.13, plus a
  Docker build check.

### Changed

- **The webhook is now a positional argument.** `fourchar-radar run <url>` is
  the intended usage and needs no config file; `.env` and
  `DISCORD_WEBHOOK_URL` still work and are simply lower precedence.
- The database moved from a relative `state/radar.db` to the OS per-user data
  directory (XDG / `~/Library/Application Support` / `%LOCALAPPDATA%`), so the
  radar no longer starts "fresh" when launched from a different directory by
  systemd or cron.
- Added `install.sh` and `install.ps1`: no-root, one-command bootstrap for any
  Linux distro and for Windows, with fallbacks for distros missing `ensurepip`
  and for those enforcing PEP 668.

### Added

- **Proxy pool.** `--proxy` (repeatable), `--proxy-file`, and `RADAR_PROXY_LIST`.
  The endpoint's limit is per IP, so N ready proxies mean N budgets and the
  pacing budget scales with them. A proxy Discord throttles is parked for
  exactly as long as it is told while the rest keep working; one that cannot be
  reached drops out of rotation with a backoff.
- `--test-proxies` checks each configured proxy against the real endpoint and
  reports which are usable, before committing to a multi-day scan.
- `RADAR_RATE_LIMIT_START_RPS` to override the starting rate when you know the
  endpoints better than the default.
- MTProto is rejected with an explanation: it is a Telegram transport and will
  not connect to Discord. SOCKS4/5 and HTTP/HTTPS proxies are supported.
- `[socks]` extra for SOCKS proxy support.

### Fixed

- A resumed scan silently skipped every name it already knew, so the user
  watched a "skipped" counter climb with no output — indistinguishable from a
  hang. Skipped names are now replayed from the database, once each: free ones
  always (they are finds the user never heard about), taken ones in
  `--report-every` mode. Gated on `notified_at`, so a name is announced once
  and not once per restart.
- `install.sh` failed outright for everyone using the advertised one-liner:
  the package is not on PyPI, so `pip install fourchar-radar` found nothing
  and the script died with "install failed". It now falls back to the GitHub
  archive and verifies the import actually succeeded before reporting success.

- `run` on Windows raised `RuntimeError` from the signal-handler setup, which
  does not exist on that platform. Registration is now skipped there and the
  `KeyboardInterrupt` path unwinds the run so the summary and the database are
  still handled.
- A read-only `$HOME` (common in containers) aborted startup instead of
  falling back to a writable location.
- Throttle penalties made the process un-stoppable: `SIGTERM` was ignored for
  the full `retry_after` window (up to 35 minutes), so `docker stop` appeared
  to hang. Waits are now interruptible via a stop event the limiter races.
- `extra={"name": ...}` collided with `LogRecord.name`, raising
  `KeyError: "Attempt to overwrite 'name' in LogRecord"` on every skipped
  invalid name and every unexpected response.
- The bundled wordlist was not shipped in the wheel (it lived in the repo
  root), so an installed package silently had no candidates at all. It now
  ships as package data, with a regression test.
- A rejected webhook URL crashed `run` with a raw traceback, and only after
  the database had already been created. `run` now verifies the webhook at
  startup and fails with actionable text and exit code 1 before doing work.
- Read-only commands (`top`, `status`, `sources`, `export`) failed when no
  webhook was configured. The requirement is now scoped to `run`.
- Candidate sources yielding an empty stream could hang on `queue.join()`.
- `Store.close()` was awaited despite being synchronous, masking the real exit
  code of every scan.

### Known limitations

- Throughput is **tens of checks per hour**, not thousands. The public
  endpoint is burst-and-penalise: ~16 requests succeed, then a ~35-minute
  `retry_after` lockout, and the cycle repeats. Lowering the rate does not
  help. See the README for measurements. This is the dominant constraint on
  the whole design, not an oversight.
- A full `bruteforce` sweep (1,874,161 names) is not reachable — measured
  throughput puts it in the range of years. It is implemented, ordered
  best-first and resumable; run it only with several egress IPs, or as a
  long-lived background job you do not expect to finish.
- Price estimates are a triage heuristic, not market valuations.
- `bruteforce` yields some names twice (the tail overlaps the shaped
  prefixes). The skip-set absorbs this at the cost of one local `SELECT`; no
  extra requests are made.
- Whether a long-idle IP is ever granted a steady (non-burst) allowance is
  unconfirmed. The documentation assumes the worst case on purpose.
