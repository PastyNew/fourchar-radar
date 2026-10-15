# Contributing

Thanks for looking at this. A few things that make patches land quickly.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
make dev          # pip install -e ".[bot,dev]"
```

## Before you open a PR

```bash
make check        # ruff + mypy + pytest — exactly what CI runs
```

All three are configured strictly. `mypy --strict` and the `ruff` rule set
(incl. `S` for bandit-style security checks) are not negotiable; if a rule is
wrong for this codebase, change the config with a comment explaining why,
rather than scattering `noqa` around.

## Please don't

**Raise the default request rate, or add anything that evades rate limits.**
Not as a policy preference — it actively breaks the tool. Discord throttles
this endpoint into a multi-minute penalty, and the AIMD limiter in
`checking/ratelimit.py` exists precisely to stay underneath that. Anything
that makes requests look less like a browser (randomised cookies, fingerprint
spoofing, rotating sessions) gets an IP blocked outright, and the resulting
`retry_after` is far worse than being patient.

**Register accounts, claim usernames, or automate account actions.** Out of
scope for this project by design. It observes public availability data and
tells a human about it.

## Where things go

| If you are changing… | Look at |
|---|---|
| what gets scanned, or in what order | `sources/` |
| how requests are paced | `checking/ratelimit.py` |
| HTTP policy (429/400/5xx handling) | `checking/client.py` |
| scan flow, resume, failure handling | `checking/scanner.py` |
| prices and tiers | `pricing/estimator.py` (all weights in `PriceModel`) |
| what an alert looks like | `notify/embed.py` |
| config and validation | `config.py` |

## Tests

Every behaviour change needs a test. The suite deliberately covers the
*awkward* paths, not the happy ones — throttling, shutdown during a
throttle penalty, resume, invalid names, alert failures. Those are where the
real bugs have been.

Two rules:

* Tests must not touch the network. Use `httpx.MockTransport`.
* Tests must not sleep. Build rate limiters with a high `ceiling_rps` rather
  than raising `Settings.rate_limit_rps` (which is capped on purpose).

## Tuning the price model

`tests/test_estimator.py` asserts *ordering and invariants* — that a real word
always outranks an invented name, that penalties always lower the price — not
exact numbers. You are meant to retune `PriceModel`; the calibration guard in
`test_words_are_priced_at_the_documented_order_of_magnitude` is the one
assertion that says "and keep real words roughly here".

## Commit messages

Conventional-ish, imperative, one line of subject and an optional body that
says *why*:

```
fix: make throttle waits interruptible

A 35-minute retry_after blocked SIGTERM for the full duration, so
`docker stop` took half an hour. acquire() now races the scan's stop
event, and Shutdown unwinds the worker cleanly.
```
