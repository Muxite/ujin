# Architecture

ujin is a layered scraper-poller: a dependency-free adaptive poll engine at the
core, a web fetch/extract toolkit above it, and three independent FastAPI
services (plus an MCP server) on top. Heavier capabilities are opt-in extras;
`pip install ujin` alone is pure stdlib.

## Module map

```
ujin/
├── engine.py        PollEngine: due-target loop, sweep(), poll_once()        [core]
├── adapt/           AdaptiveInterval, jitter, Backoff, CircuitBreaker,
│                    TokenBucket, AIMDLimiter, SiteStore (durable per-host),
│                    derive_signals/SignalAdvisor (policy signals)             [core]
├── poll/            Pollable roles: callable, command                        [core]
│                    http, rss, api, site, scrape, browser                    [web]
├── registry.py      plugin registry: source/transform/sink/scorer/action     [core]
├── plugins/         hot-reloadable plugin loader                             [core]
├── auth.py          shared UJIN_API_KEY ASGI middleware (HTTP + WS)          [service]
│
├── fetch/           http.py (aiohttp), obscura.py (Rust renderer),
│                    browser.py (playwright/selenium recipes),
│                    altpath.py (sitemap/RSS fallback),
│                    capabilities.py (backend matrix)                         [web]
├── extract/         links, article (trafilatura), structured, profile        [web]
├── cache/           ScrapeCache (LRU+TTL), DiskCache (SQLite), HostPolicy    [web]
├── sources/         rss, sitemap, discover, social/ (x chain, mastodon…)    [web]
├── diff/            region-scoped change detection + event sinks            [diff]
├── session/ proxy/  cookie persistence, proxy rotation                      [sessions]
├── trends/          breaking-score tiering + corroboration                  [social]
│
├── scrape/          ScrapeService orchestrator + :8901 app                  [scrape]
├── jobs/            JobSpec/JobStore/JobManager/pipeline + :8902 app        [jobs]
├── service.py       poller control :8900 app                                [service]
├── mcp/             FastMCP server over scrape+jobs                         [mcp]
└── cli.py           serve/sweep/api/scrape-serve/jobs-serve/mcp-serve/watch
```

## The poll loop (engine.py + adapt/)

Every target couples a `Pollable` with adaptive state:

```
due? ── token bucket (global rate) ── semaphore (max_concurrency) ── poll(prev)
                                                                        │
   next_due = now + jitter(interval)                                    ▼
   interval *= grow (no change) | shrink (changed)        fingerprint comparison
   Backoff on failure; CircuitBreaker trips after N        → changed? → on_change
```

- **Adaptive interval**: unchanged polls back off multiplicatively (×1.6),
  changes tighten (×0.4), clamped to [min, max].
- **Jitter** (`decorrelated` default) prevents thundering herds; **TokenBucket**
  smooths the aggregate request rate; the **semaphore** caps in-flight polls;
  the **CircuitBreaker** stops hammering dead targets (open → half-open probe).
- Everything takes injectable `clock`/`sleep`, which is why the whole engine is
  testable with a fake clock in milliseconds.

The controllers above are in-memory and reset on restart. **`SiteStore`**
(`adapt/site_store.py`) is the durable floor under them: a stdlib-`sqlite3`
table of per-host observed state — last status, p50/last latency, error and 429
counts, observed `Crawl-delay`, the current adaptive interval, and `last_seen` —
so a fresh process resumes calibrated, polite polling instead of relearning
every target. `get(host)` returns a zero-valued `HostRecord` for unknown hosts;
`record(host, **signals)` is an atomic, serialized upsert (counters accumulate,
gauges overwrite, `last_seen` is stamped from an injectable clock). It reuses
the cache's durability pattern — one connection in WAL mode with
`synchronous=NORMAL`, a single lock, and a truncating `wal_checkpoint` on
`close()`. It is additive and wires into nothing by default; it is the
foundation other Track-1 adaptive units consume.

**`derive_signals` / `PolicySignals` / `SignalAdvisor`** (`adapt/signals.py`) is
the pure, deterministic *interpretation* layer above that store. Given one
`HostRecord`, `derive_signals(record, *, base_interval=0.0, robots_crawl_delay=None)`
returns a frozen `PolicySignals` — `recommended_interval`, `cooldown_secs` /
`should_cooldown`, `rate_limited`, `concurrency_factor`, and a single `health` in
`0..1`. The rules are documented and side-effect-free: a 429 (counter or last
status) sets `rate_limited`, raises the interval, and throttles concurrency;
`recommended_interval` is never below `max(crawl_delay, robots_crawl_delay)`;
rising `error_count` lowers `health` and raises `cooldown_secs`; a clean record is
pristine (`health == 1.0`, no cooldown, full concurrency, interval ==
`base_interval`). `SignalAdvisor(store)` is the only stateful piece — a read-only
bridge whose `for_host(host)` reads `store.get(host)` and derives signals without
mutating anything. It is additive and opt-in: nothing wires it into the
scrape/poll path. It is the input layer the planned strategy-feedback and
learned-rate-limit units consume.

## The scrape fallback chain (scrape/service.py)

`ScrapeService.scrape(url, mode, render)` is the only entry the routes/MCP use:

```
host on cooldown? ── yes ─► cache hit? ─► serve cache : raise HostCooldown(429)
        │ no
        ▼
HTTP GET (ETag/If-Modified-Since)
        ├── 304 ──────────────► serve cache                     strategy http_304
        ├── 200, ≥5 links ────► extract → cache → result        strategy http
        ├── 200 thin / 4xx/5xx ► obscura render ───────────────► strategy obscura
        │                          └─ failed/missing ► altpath:
        │                               news-sitemap probes ───► strategy sitemap_news
        │                               pinned/discovered RSS ─► strategy rss
        └── everything failed ► record_failure → RuntimeError
render="browser" (pinned only): recipe via BrowserFetcher      strategy browser
mode="combined": RSS + HTML legs in parallel, merged by URL    strategy combined
```

Per-host state lives in `HostPolicy` (exponential cooldown after failures) and
`HostMetrics` (counters + latency percentiles, served at `/metrics`).
Per-site behavior (pinned strategies, extraction profiles, deny patterns)
comes from `HostOverrideRegistry` (per_host.yaml).

## Topology

```
:8900 ujin api          PollEngine control — targets CRUD, /sweep, WS /ws
:8901 ujin scrape-serve ScrapeService — /scrape /feed /sitemap /discover
                        /capabilities /metrics /social/* /trends/*
:8902 ujin jobs-serve   JobManager — /jobs CRUD + workflows dir + WS events
stdio ujin mcp-serve    FastMCP tools over the same ScrapeService/JobManager
```

All three HTTP apps are independent FastAPI factories sharing the same
building blocks (`build_scrape_components` wires fetcher/cache/policy/metrics
once; the jobs app and MCP server reuse it). `UJIN_API_KEY` mounts the same
ASGI key gate on each (ujin/auth.py); `/health` responses share the
`{ok, status, service}` shape.

A *job* (:8902) is `source → transforms → sinks` on a schedule; workflow YAML
files in `UJIN_WORKFLOWS_DIR` are jobs with filename-stable ids (see
WORKFLOWS.md / JOBS.md). All kinds resolve through `ujin.registry.register`,
which plugins extend at runtime (PLUGINS.md).

## Dependency policy

- The core never imports outside the stdlib; web deps import lazily inside
  functions so `import ujin` stays cheap and dependency-free.
- The obscura Rust crate is vendored as a git submodule but **excluded from
  the wheel** and never built by pip or CI — `ujin obscura-build` is explicit.
- Stable import surface: `ujin` (engine + pollables + register + fingerprint),
  `ujin.scrape` (ScrapeService, ScrapeResult, build_scrape_service),
  `ujin.jobs` (specs, JobStore, JobManager), `ujin.fetch.obscura`
  (obscura_available, ObscuraFetcher). Everything else is internal.

## Environment variables

Scrape config (`ScrapeConfig.from_env()`, prefix `UJIN_`): timeouts, user
agent, per-host concurrency, host cooldown, cache sizes/TTL, disk cache path,
batch max, browser engine/headless, brave key, nitter pool, breaking-scorer
weights — see `ujin/scrape/config.py` for the full list. Cross-cutting:

| var | effect |
|---|---|
| `UJIN_API_KEY` | mount the key gate on all services |
| `UJIN_JOBS_DB` | jobs SQLite path (`./ujin-jobs.db`) |
| `UJIN_WORKFLOWS_DIR` | workflow YAML dir (`/workflows`) |
| `OBSCURA_BIN` / `OBSCURA_URL` | renderer binary path / HTTP service |
| `SEARCH_API_KEY` | Brave key for the twitter leg |
