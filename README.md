# fourchar-radar

Find free 4-character Discord usernames, value them, and get pinged the moment
one shows up.

Paste a webhook URL. The radar starts pulling candidates, checks each one
against Discord's public availability endpoint, prices whatever is still free,
and posts the good ones to your channel. Everything it has ever seen is kept in
a local SQLite file, so you can stop it, restart it, and pick up exactly where
it left off.

```
🎯 cool is free
• exact English word
• easy to say out loud
• no confusable glyphs
• almost no repeated characters
Estimated value   $1.2k      Tier  🔵 RARE
```

---

## Read this first: how fast this can actually go

Discord's unauthenticated username endpoint does not publish a budget, and in
practice it is **brutally** throttled. Measured on a residential IP:

| Observation | Result |
|---|---|
| Short burst | first ~16 requests returned `200` |
| Immediately after | `429` with `{"retry_after": 2114}` — **~35 minutes** |
| 35 minutes later, first request | `429` again, `retry_after: 2151` — a **fresh** penalty |
| Randomised cookies | Cloudflare challenge instead of a different bucket |

So this is not "slow, use a lower rate". The endpoint behaves in
**burst-and-penalise** cycles: a small burst, then a lockout roughly half an
hour long, then the lockout again. Sustained throughput is on the order of
**tens of checks per hour** — a few hundred per day.

Against a search space of 37⁴ = 1,874,161 possible 4-character names:

| Plan | Realistic time |
|---|---|
| bundled wordlist (2,418 names) | **~4 days** |
| full `bruteforce` sweep | **years** — it works, but expect never to finish it |

One more measurement worth knowing: a request sent *during* a penalty makes it
worse. A single probe mid-penalty extended it by ~15 minutes. So the radar
sleeps out the full `retry_after` and never probes early — and the limiter now
**starts** at the measured rate (~0.0076 req/s) rather than at the configured
ceiling, because climbing down from too fast costs one 35-minute penalty per
halving, several hours of doing nothing.

This is not a limitation the project pretends away. It is the reason the
architecture looks the way it does:

* **Candidates are ordered best-first.** Real English words first, because they
  are both the most wanted and the most saleable. You spend your scarce budget
  on names worth having.
* **Everything is resumable.** SQLite holds the skip-set, so a restart costs
  you nothing and re-checking costs zero requests.
* **The rate limiter is adaptive (AIMD).** It sits at whatever rate Discord
  tolerates and only creeps upward after sustained success.

If you need materially more throughput, the only real lever is **more egress
IP addresses**: set `RADAR_PROXY` and run several instances against different
exits, which scales roughly linearly. Do not try to evade the limit by
rotating cookies or client fingerprints — that gets an IP blocked outright,
and a hard block is far worse than patience.

---

## The menu

If you would rather not remember a single flag, there is a menu:

```bash
fourchar-radar tui          # or: fourchar-radar-tui
```

```
  Discord Username Search
  Developer: PastyNew

  Your webhook: (not set yet)

  1. Change Webhook
  2. Search for usernames
  3. Close the program

  Choose 1, 2 or 3:
```

Press **1** once, paste your webhook, and it is remembered for next time. Then
**2** starts the search and prints what it finds as it goes — `Ctrl+C` stops it
whenever you like, and nothing already checked is lost, so **2** again resumes
where it stopped. **3** closes the program.

Everything the menu does is the same engine the CLI uses; it is a menu, not a
second implementation.

---

## Quick start

Paste a webhook URL. That's the whole setup — no config file, no `.env`, no bot
account, no OAuth.

**1. Get your webhook URL.** In your Discord channel: *Edit Channel →
Integrations → Webhooks → New Webhook → Copy Webhook URL*.

**2. Run it.**

```bash
fourchar-radar run https://discord.com/api/webhooks/ID/TOKEN
```

Try it without alerts first, to see it work before sending anything to your
channel:

```bash
fourchar-radar run --dry-run --limit 20
```

### Install

<details open>
<summary><b>Linux / macOS</b> — one line</summary>

```bash
curl -sSL https://raw.githubusercontent.com/PastyNew/fourchar-radar/main/install.sh | sh
```

</details>

<details>
<summary><b>Windows</b> — PowerShell</summary>

```powershell
irm https://raw.githubusercontent.com/PastyNew/fourchar-radar/main/install.ps1 | iex
```

</details>

<details>
<summary><b>From a checkout</b> (any platform)</summary>

```bash
git clone https://github.com/PastyNew/fourchar-radar
cd fourchar-radar
sh install.sh            # or: powershell -ExecutionPolicy Bypass -File install.ps1
```

</details>

<details>
<summary><b>Manual install</b></summary>

```bash
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e .            # add [bot] for slash commands, [dev] for tests
```

</details>

The installer needs Python 3.11+ and no root: it creates a virtualenv where
one is possible, and falls back to a `--user` install on minimal distros
(Alpine, Debian without `python3-venv`) and on PEP 668 distros (Debian 12+,
Fedora 40+) where a plain `pip install` is refused.

**Where does my data go?** Into the per-user data directory, not the current
folder — so it keeps working when run from systemd, cron, or any other
directory:

| Platform | Location |
|---|---|
| Linux | `~/.local/share/fourchar-radar/radar.db` |
| macOS | `~/Library/Application Support/fourchar-radar/radar.db` |
| Windows | `%LOCALAPPDATA%\fourchar-radar\radar.db` |

Override with `--db PATH` or `RADAR_DB_PATH` if you want it elsewhere.

### A `.env` file is still supported

If you'd rather not retype the URL every time, put it in `.env` once:

```bash
cp .env.example .env
```

Everything in `.env.example` still works exactly as documented below; the
positional URL simply takes precedence.

### In Russian, коротко

**Если хочешь просто меню, без флагов:**

```bash
fourchar-radar tui
```

Откроется меню: вставить вебхук (1), искать (2), выйти (3). Поиск идёт
прямо в терминале, `Ctrl+C` останавливает, повторный запуск продолжает с
того же места.

**Либо через терминал, как раньше:**

1. **Установка** — одна строка:
   ```bash
   curl -sSL https://raw.githubusercontent.com/PastyNew/fourchar-radar/main/install.sh | sh
   ```
   На Windows в PowerShell:
   ```powershell
   irm https://raw.githubusercontent.com/PastyNew/fourchar-radar/main/install.ps1 | iex
   ```
2. В канале Discord: **Изменение канала → Интеграции → Вебхуки → Новый вебхук → Копировать URL**.
3. **Запуск** — вставь URL прямо в команду:
   ```bash
   fourchar-radar run https://discord.com/api/webhooks/ID/TOKEN
   ```

Никаких файлов редактировать не нужно. Базa данных сама создаётся в
`~/.local/share/fourchar-radar/radar.db` (на Windows — в `%LOCALAPPDATA%`), так
что запуск из любой папки работает одинаково. Бот-токен не нужен — хватает
вебхука.

---

## Commands

| Command | What it does |
|---|---|
| `run` | the main event — scan and alert |
| `price huhu code cool` | check specific names right now |
| `top --tier rare --limit 20` | best free names recorded so far |
| `status` | database summary |
| `sources` | what can be scanned, and how big each source is |
| `export --format csv --out free.csv` | dump findings |
| `prune --days 30` | housekeeping |
| `bot` | run with slash commands (needs `[bot]`) |

Useful `run` flags:

```bash
fourchar-radar run <URL> --dry-run --limit 50   # no alerts, just prove it works
fourchar-radar run <URL> --source bruteforce    # every 4-char combination, best-first
fourchar-radar run <URL> --watch                # re-stream forever, catching new frees
fourchar-radar run <URL> --min-tier epic        # only wake me for the good stuff
```

To avoid retyping the URL, set it once in `.env` (or export
`DISCORD_WEBHOOK_URL`) and call plain `fourchar-radar run`. The positional URL
wins if both are present.

---

## Candidate sources

| Source | Size (len 4) | Notes |
|---|---|---|
| `wordlist` | 2,418 | bundled English words + modern/internet slang. **Start here.** |
| `file` | yours | one name per line, `#` comments allowed |
| `stdin` | yours | `printf 'huhu\ncode\n' \| fourchar-radar run --source stdin` |
| `bruteforce` | 1,874,161 | every combination, grouped best-first. A patient, resumable sweep. |

Bring your own list for anything specific — that is usually the highest-value
configuration:

```bash
fourchar-radar run --source file --names-file my-list.txt
```

---

## What gets posted to Discord

| Message | When |
|---|---|
| `fourchar-radar` connected | once, at startup |
| `🟢 cool is free — $1,250 (rare)` | per-name mode, name still free |
| `⚪ zzzz is taken` | per-name mode, name taken |
| `🎯 cool is free` + reasons | default mode, good find only |
| `📡 Still searching` | heartbeat: checked / taken / free / rate / throttles |

The heartbeat matters more than it looks. A scan runs for days and the channel
is otherwise silent for hours, which is indistinguishable from a crash.

```bash
fourchar-radar run <URL> --report-every     # every name, free and taken
fourchar-radar run <URL> --report-every-checks 50
fourchar-radar run <URL> --no-progress      # only the good finds
```

In the menu, option **2** asks which of the two you want.

---

## How the price estimate works

**It is a triage heuristic, not a market quote.** Its job is to answer "is this
find worth interrupting me for?" — not to predict what a name would fetch on an
auction site.

`pricing/estimator.py` scores a handful of interpretable features and adds a USD
bonus for each:

| Feature | Effect |
|---|---|
| exact English word | the dominant term |
| one letter off a real word | small fraction of the above |
| easy to say out loud | moderate |
| no confusable glyphs (`0`/`o`, `1`/`l`) | moderate |
| few repeated characters | moderate |
| contains a keyboard/alpha sequence (`abcd`, `0987`) | moderate |
| doubled letter (`cool`, `best`) | small |
| letters only, no digits/underscores | small |
| all vowels, or a 3+ consonant cluster | **penalty** |

Totals are capped and mapped onto tiers:

| Tier | From |
|---|---|
| `COMMON` | below $150 |
| `UNCOMMON` | $150+ |
| `RARE` | $1,000+ |
| `EPIC` | $10,000+ |
| `LEGENDARY` | $50,000+ |

Every contributing term is returned in `Quote.reasons`, which is why each alert
explains itself. All weights live in one `PriceModel` dataclass — tune them
there rather than patching logic.

---

## Configuration

Everything is environment-driven (or `.env`); every flag overrides it.

| Variable | Default | Meaning |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | — | **required** unless `--dry-run` |
| `DISCORD_BOT_TOKEN` | — | only for bot mode |
| `DISCORD_COMMAND_CHANNEL_ID` | — | where the bot posts alerts |
| `RADAR_SOURCE` | `wordlist` | `wordlist` / `bruteforce` / `file` / `stdin` |
| `RADAR_LENGTH` | `4` | username length |
| `RADAR_NAMES_FILE` | — | required for `--source file` |
| `RADAR_RATE_LIMIT_RPS` | `0.5` | **ceiling**, not a target |
| `RADAR_CONCURRENCY` | `1` | workers; keep it at 1–2 |
| `RADAR_ALERT_MIN_TIER` | `rare` | alert threshold |
| `RADAR_ALERT_COOLDOWN_S` | `0` | minimum gap between alerts |
| `RADAR_DB_PATH` | `state/radar.db` | SQLite location |
| `RADAR_PROXY` | — | single egress proxy, e.g. `http://user:pass@host:port` |
| `RADAR_LOG_JSON` | `false` | JSON log lines for Docker/journald |

Invalid config fails at startup with a readable message, rather than three
hours into a scan.

---

## Docker

```bash
docker compose up -d
docker compose logs -f
```

Or plain Docker:

```bash
docker build -t fourchar-radar .
docker run --rm -it --env-file .env -v "$PWD/state:/app/state" fourchar-radar
```

A systemd unit for a long-running instance:

```ini
[Unit]
Description=fourchar-radar
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/fourchar-radar
EnvironmentFile=/opt/fourchar-radar/.env
ExecStart=/opt/fourchar-radar/.venv/bin/fourchar-radar run --watch
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

---

## Bot mode (optional)

```bash
pip install -e ".[bot]"
export DISCORD_BOT_TOKEN=...
export DISCORD_COMMAND_CHANNEL_ID=...
fourchar-radar bot
```

Slash commands, all ephemeral (only you see the replies):

* `/price <name>` — check and value one name
* `/top` — best free names already known
* `/status` — live scan + rate-limiter state
* `/scan start` / `/scan stop` — drive the background scan

The bot shares the exact same checker, limiter, store and pricer as the CLI.
It is a control surface, not a second implementation.

---

## Architecture

```
        candidates                 verdict              finding
  ┌──────────────────┐        ┌───────────────┐    ┌──────────────┐
  │ sources/         │        │ checking/     │    │ pricing/     │
  │  wordlist        │───────▶│  ratelimit    │───▶│  estimator    │
  │  bruteforce      │        │  client       │    └──────────────┘
  │  file / stdin    │        │  scanner      │           │
  └──────────────────┘        └───────┬───────┘           │
                                     │                   │
                              ┌──────▼───────┐     ┌─────▼──────┐
                              │ storage.py   │     │ notify/    │
                              │  SQLite      │     │  webhook   │
                              │  queue+log   │     │  bot       │
                              └──────────────┘     └────────────┘
```

| Module | Responsibility |
|---|---|
| `platforms.py` | data dir, signal support, interpreter — the OS differences |
| `models.py` | frozen value objects: `Verdict`, `Quote`, `Finding`, `ScanStats` |
| `names.py` | username syntax rules, shared by every layer |
| `lexicon.py` | wordlist loading (used by both scanning and pricing) |
| `sources/` | pluggable candidate generators, ordered best-first |
| `checking/ratelimit.py` | AIMD adaptive pacing |
| `checking/client.py` | one HTTP check, with the 429/400/5xx policy |
| `checking/scanner.py` | orchestration, resume, failure handling |
| `pricing/estimator.py` | heuristic valuation and tiering |
| `storage.py` | SQLite skip-set + findings history |
| `notify/` | webhook (default) and bot (optional) |
| `cli.py` | argparse front end |

Design decisions worth knowing:

* **Skip-set, not queue.** The candidate stream is streamed, not materialised —
  1.87M names in RAM is pointless. Each candidate gets a SQLite index lookup;
  that is microseconds next to a network round-trip.
* **429 is not an error.** It is the server asking us to wait, so it is budgeted
  separately from transport failures. Sitting out a 35-minute penalty must never
  exhaust the retry budget.
* **Alerts never kill a scan.** Every notification failure is logged and
  swallowed.
* **Webhook tokens are never logged.** URLs are redacted in every log line.

---

## Development

```bash
make dev           # install with [bot,dev] extras
make check         # ruff + mypy + pytest  (what CI runs)
make run           # start with .env
```

---

## Legal / ToS

This tool queries the same public endpoint the Discord web client uses while
registering, at a deliberately conservative rate, and sends nothing on your
behalf — it does not register accounts, claim usernames, or interact with the
Discord API as you.

Still, you are responsible for how you use it. Respect Discord's Terms of
Service and any applicable law. Do not use it to harass, to enumerate users, or
to circumvent rate limits. No warranty, no liability — see `LICENSE`.

---

## Publishing / forking

To put your own copy on GitHub:

```bash
# 1. Point the repo at your account (rewrites 9 placeholder URLs)
python scripts/set_repo.py --user YOURNAME

# 2. Create the repository on GitHub, then push
git init
git add -A
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/YOURNAME/fourchar-radar.git
git push -u origin main
```

`set_repo.py` is idempotent and verifies that no `PastyNew` placeholder is
left behind, so the one-line installer in this README will not point at
somebody else's repository.

The GitHub Actions workflow in `.github/workflows/ci.yml` runs on the push
automatically: ruff, `ruff format --check`, `mypy --strict`, the test suite on
Python 3.11/3.12/3.13, a wheel build, and a Docker build.

### Optional: publish to PyPI

The `release.yml` workflow publishes to PyPI when you push a `v*` tag, using
trusted publishing (no stored token):

```bash
git tag v0.1.0
git push origin v0.1.0
```

Add the environment and the PyPI publisher under **Settings → Environments**
first, or the job will fail at the publish step.

---

## Contributing

Issues and PRs welcome. Please read `CONTRIBUTING.md` first, and keep the test
suite green (`make check`) — it is the fastest way to be useful here.

## License

MIT — see [`LICENSE`](LICENSE).

The bundled wordlist in `src/fourchar_radar/data/words4.txt` is derived from
the public-domain `/usr/share/dict/words` plus a hand-added list of modern
internet slang. To use your own, either pass `--names-file` or drop another
`.txt` file into that package directory and call
`load_wordlist("yourfile.txt")`.