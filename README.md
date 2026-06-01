# ujin

The **ultimate scraper-poller**. Two halves that share one toolkit:

1. **Adaptive multi-role poller** — poll *anything* (HTTP pages, RSS, JSON APIs,
   shell commands, Python callables, or scoped page regions) on a cadence that
   **adapts** to change, with **jitter** so aggregate load stays smooth.
2. **Rich scrape service** — one-shot rendering + extraction with an
   HTTP → obscura → sitemap → RSS fallback chain, per-host cooldown,
   fingerprinted change detection, structured-data extraction, and optional
   social/trends sources — all behind a small HTTP API.

It bundles the [obscura](https://github.com/Muxite/obscura) headless renderer as
a submodule, and stays a **pure-python pip install** (the renderer is built
separately, never at install time).

## Why

Naive pollers are either too slow (fixed long interval) or too aggressive (fixed
short interval), and fixed-interval pollers drift into phase and produce periodic
load spikes. ujin backs off targets that aren't changing, speeds up ones that
are, retreats on errors/429s, and spreads work with jitter + a global token
bucket. The scrape side adds everything you actually need to get clean content
out of hostile real-world pages.

## Quick start — the poller

```python
import asyncio
from ujin import PollEngine, HttpPollable, CallablePollable, CommandPollable

engine = PollEngine()                       # smoothing token bucket built in
engine.add(HttpPollable("https://example.com"), base=300, on_change=on_change)
engine.add(CommandPollable(["git", "ls-remote", url]), base=600)
engine.add(CallablePollable(lambda: db.count(), key="rows"), base=30)

await engine.run()           # long-running daemon (adaptive + jittered)
# or: results = await engine.sweep()   # one pass — cron-friendly
```

## Quick start — scraping

```python
from ujin.scrape.service import ScrapeService
# or just run the HTTP service:  ujin scrape-serve   (POST /scrape, /feed, ...)
```
```bash
curl -X POST localhost:8901/scrape -H 'content-type: application/json' \
  -d '{"url":"https://apnews.com","mode":"links"}'
```

## Watch a page for change

```bash
ujin watch https://example.com --selector main --webhook https://hooks/me
```
Fingerprints only the regions matched by your selectors, so cosmetic churn
elsewhere doesn't trip the watcher. Drives the same adaptive engine.

## CLI

```bash
ujin sweep targets.yaml      # poll all targets once; print what changed
ujin serve targets.yaml      # run the poll engine as a daemon
ujin api [targets.yaml]      # poller control service (REST + WS) on :8900
ujin scrape-serve            # rich scrape HTTP service on :8901
ujin watch URL --selector …  # watch a page's regions for change
ujin obscura-build           # build the bundled headless renderer (needs cargo)
```

## HTTP services

Two independent FastAPI apps — run either or both. Full reference in
[docs/API.md](docs/API.md); interactive docs at `/docs` on each.

- **Poller control** (`:8900`): `GET /health /stats /targets`,
  `POST /targets`, `DELETE /targets/{key}`, `POST /sweep`, `WS /ws`.
- **Scrape** (`:8901`): `POST /scrape` (modes `links|article|auto|combined|structured`),
  `/scrape:batch`, `/feed`, `/sitemap`, `/discover`, `/metrics`, plus optional
  `/social/*` and `/trends/*`.

## Docker

```bash
docker compose up --build                  # poller :8900 + scrape :8901 (pure-python, fast)
docker compose --profile render up --build # also build the obscura-enabled service :8902 (slow)
```
The default `ujin` image is pure-python and builds in seconds (the Rust stage is
skipped). The `ujin-full` target bakes the obscura renderer in for JS-heavy /
anti-bot pages — its first build compiles V8 (~15–20 min).

```bash
curl localhost:8901/health
curl -X POST localhost:8901/scrape -H 'content-type: application/json' \
  -d '{"url":"https://example.com","mode":"structured"}'
```

## How it works

- **Roles** (`ujin.poll`): `HttpPollable`, `RssPollable`, `ApiPollable`,
  `CommandPollable`, `CallablePollable`, and `SitePollable` (selector-scoped
  change). Each returns a `PollResult` with a content fingerprint.
- **Adaptive** (`ujin.adapt`): `AdaptiveInterval` grows/shrinks the interval;
  full/equal/decorrelated **jitter**; `TokenBucket` + AIMD smoothing;
  exponential `Backoff` (honors `Retry-After`) and a `CircuitBreaker`.
- **Scrape** (`ujin.scrape`): `ScrapeService` orchestrates fetch + cache +
  extract + the fallback chain; a pluggable `Scorer` ranks links and paces polls
  (`NullScorer` by default, `ujin.trends.BreakingScorer` for news-trading).
- **Toolkit**: `ujin.fetch` (HTTP + obscura + altpath), `ujin.extract`
  (article/links/profile/**structured** JSON-LD·OG·microdata), `ujin.cache`
  (LRU+TTL, SQLite, per-host cooldown), `ujin.sources` (RSS/sitemap/discover +
  `social/`), `ujin.diff` (region diff + webhook sinks), `ujin.session`
  (cookies), `ujin.proxy` (rotation).

The engine takes injectable `clock`/`rng`/`sleep` for deterministic tests.

## Install

```bash
pip install -e .              # core: engine + adapt + callable/command roles (no deps)
pip install -e ".[web]"       # + HTTP/RSS/API roles and the scrape toolkit
pip install -e ".[scrape]"    # + the rich scrape HTTP service
pip install -e ".[all]"       # everything (web, service, scrape, social, diff, sessions)
```
Core is dependency-free; heavier features pull `aiohttp`/`selectolax`/
`trafilatura`/`feedparser`/`fastapi` lazily behind extras. The obscura submodule
is **excluded from the wheel** — `pip install ujin` never triggers a Rust build.

```bash
git submodule update --init --recursive   # fetch the obscura renderer source
ujin obscura-build                         # build it (optional; needs cargo)
```

## Lineage

Formerly `scraperv2`, extracted from jennie's scraper-v2. The poller (`poll/`,
`adapt/`, `engine.py`) was built fresh; the scrape service reaches feature/
endpoint parity with scraper-v2 so jennie's irene pipeline can migrate onto ujin.
News-trading scoring (tiering/corroboration/breaking score) stays optional behind
`ujin.trends.BreakingScorer`. Consumed by awork and hct-site as a submodule.
