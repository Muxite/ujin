# eujin

Adaptive multi-role poller with jitter. Poll **anything** — HTTP pages, RSS feeds,
JSON APIs, shell commands, or arbitrary Python callables — on a cadence that
**adapts** to change, with **jitter** so aggregate load stays smooth instead of
spiky. Also ships the shared web-scraping toolkit it grew out of.

## Why

Naive pollers are either too slow (fixed long interval) or too aggressive (fixed
short interval), and many fixed-interval pollers drift into phase and produce
periodic load spikes. eujin backs off targets that aren't changing, speeds up ones
that are, retreats on errors/429s, and spreads work with jitter + a global token
bucket so the request rate is steady.

## Quick start

```python
import asyncio
from eujin import PollEngine, HttpPollable, CallablePollable, CommandPollable

engine = PollEngine()                       # smoothing token bucket built in
engine.add(HttpPollable("https://example.com"), base=300, on_change=on_change)
engine.add(CommandPollable(["git", "ls-remote", url]), base=600)
engine.add(CallablePollable(lambda: db.count(), key="rows"), base=30)

await engine.run()           # long-running daemon (adaptive + jittered)
# or
results = await engine.sweep()   # one pass — cron-friendly
```

CLI:

```bash
eujin sweep targets.yaml     # one pass; print which targets changed
eujin serve targets.yaml     # run the engine as a daemon
eujin api [targets.yaml]     # REST + WebSocket service on :8900
```

## Service (REST + WebSocket)

`eujin.service` drives the engine over HTTP and streams change events:

```
GET  /health   GET /stats   GET /targets
POST /targets  {kind, config, base?, min?, max?, jitter?}
DELETE /targets/{key}        POST /sweep
WS   /ws       -> {"event":"change","key":...,"fingerprint":...}
```

Docker:

```bash
docker compose up --build              # service on :8900
# add a target
curl -X POST localhost:8900/targets -H 'content-type: application/json' \
  -d '{"kind":"http","config":{"url":"https://example.com"},"base":300}'
```

## How it works

- **Roles** (`eujin.poll`): `HttpPollable` (conditional GET + body fingerprint),
  `RssPollable` (new/changed entries), `ApiPollable` (JSON + `json_path`),
  `CommandPollable` (stdout), `CallablePollable` (any function). Each returns a
  `PollResult` with a content fingerprint.
- **Adaptive** (`eujin.adapt.interval`): `AdaptiveInterval` grows the interval when
  unchanged, shrinks it when changed, clamped to `[min, max]`.
- **Jitter / stability** (`eujin.adapt.jitter`, `eujin.adapt.concurrency`): full /
  equal / decorrelated jitter + randomized initial phase, and a `TokenBucket` +
  AIMD limiter that flatten bursts.
- **Resilience** (`eujin.adapt.backoff`): exponential `Backoff` (honors
  `Retry-After`) and a `CircuitBreaker` that skips dead targets until a probe.
- **Engine** (`eujin.engine.PollEngine`): per-target adaptive state, jittered
  scheduling, global smoothing, change callbacks, `stats()`. `clock`/`rng`/`sleep`
  are injectable for deterministic tests.

The scrape toolkit (`eujin.fetch`, `eujin.extract`, `eujin.cache`, `eujin.sources`)
remains available for direct fetching/parsing.

## Install

```bash
pip install -e .            # core: engine + adapt + callable/command roles (no deps)
pip install -e ".[web]"     # + HTTP/RSS/API roles and the scrape toolkit
pip install -e ".[yaml]"    # + `eujin serve/sweep` config loading
```

Core is dependency-free; web roles pull `aiohttp`/`selectolax`/`trafilatura`/
`feedparser` lazily.

## Lineage

Formerly `scraperv2`, extracted from jennie's scraper-v2. The `fetch`/`extract`/
`cache`/`sources` modules are the dependency-decoupled versions hardened in awork;
the poller (`poll/`, `adapt/`, `engine.py`) is new. Consumed by awork as a git
submodule (local path for now; repoint to a remote once pushed).
