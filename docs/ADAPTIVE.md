# Adaptive Learning Subsystem

ujin's `adapt` package forms a **durable feedback loop** that makes pollers and
scrapers progressively more polite and efficient as they accumulate observations.

```
  SiteStore ──→ HostRecord
                    │
                    ▼
              derive_signals ──→ PolicySignals
                    │
              SignalAdvisor
                    │
         LearnedRateLimiter ←── ujin.robots (Crawl-delay)
                    │
             acquire(host)  ── paces requests per host
             observe(host)  ── feeds each response back in

  StrategyFeedback ──→ StrategyOutcome ──→ recommend(host)
```

All four layers are **opt-in and additive**: default poll/scrape paths are unchanged
unless you explicitly wire one in. Each layer stores to SQLite (WAL mode) so a fresh
process resumes calibrated state instead of starting from zero.

---

## SiteStore and HostRecord

`SiteStore` is the SQLite-backed foundation. It persists per-host observations so
your poller restarts with pre-warmed, polite settings.

```python
from ujin.adapt import SiteStore, HostRecord

store = SiteStore("site_state.db")   # or ":memory:" for ephemeral use

# Unknown host → zero-valued record; no special-casing needed by callers
rec: HostRecord = store.get("example.com")
print(rec.error_count, rec.rate_limit_count, rec.interval)  # 0 0 0.0

# Feed a response observation; returns the updated record
rec = store.record("example.com",
    status=200,
    latency=0.42,
    interval=10.0,
)
print(rec.p50_latency, rec.last_status)  # 0.42  200

# Counters accumulate across calls; gauges overwrite
store.record("example.com", error=1)
store.record("example.com", error=1)
rec = store.get("example.com")
print(rec.error_count)   # 2

# Persist a Crawl-delay observed from robots.txt
store.record("example.com", crawl_delay=2.0)

store.close()   # WAL checkpoint; call when done (or use a try/finally)
```

### `HostRecord` fields

| Field | Type | Description |
|-------|------|-------------|
| `host` | `str` | Hostname key |
| `last_status` | `int` | Last HTTP status code (0 if never seen) |
| `last_latency` | `float` | Most recent response time in seconds |
| `p50_latency` | `float` | Running median over the last 128 latency samples |
| `error_count` | `int` | Accumulated network / 5xx errors |
| `rate_limit_count` | `int` | Accumulated 429 responses |
| `crawl_delay` | `float` | Last observed `Crawl-delay` from robots.txt |
| `interval` | `float` | Current adaptive poll interval |
| `last_seen` | `float` | Unix timestamp of the last `record()` call |

`HostRecord` is a frozen dataclass; every field defaults to its zero value so
callers never need to special-case an unknown host.

### Signal names for `record()`

| Name | Kind | Effect |
|------|------|--------|
| `status` | gauge | Overwrites `last_status` |
| `latency` | gauge | Overwrites `last_latency`; updates the p50 window |
| `interval` | gauge | Overwrites the stored poll interval |
| `crawl_delay` | gauge | Overwrites `crawl_delay` |
| `error` | counter | Increments `error_count` by the supplied value |
| `rate_limited` | counter | Increments `rate_limit_count` by the supplied value |

Unknown signal names raise `ValueError` immediately.

---

## derive_signals, PolicySignals, SignalAdvisor

`derive_signals` is a **pure, deterministic function** — no I/O, no mutations —
that interprets a `HostRecord` into actionable policy recommendations.

```python
from ujin.adapt import SiteStore, derive_signals, PolicySignals, SignalAdvisor

store = SiteStore()
store.record("example.com", status=200, latency=0.3, interval=5.0)
rec = store.get("example.com")

sig: PolicySignals = derive_signals(rec, base_interval=5.0)

print(sig.recommended_interval)  # >= base_interval; raised by 429s
print(sig.health)                # 1.0 = clean host; falls toward 0 with errors
print(sig.should_cooldown)       # True when any cooldown is needed
print(sig.cooldown_secs)         # seconds to wait before the next request
print(sig.rate_limited)          # True after any 429 response
print(sig.concurrency_factor)    # 1.0 = full; < 1.0 under rate-limit pressure
```

`robots_crawl_delay` floors the `recommended_interval`:

```python
sig = derive_signals(rec, base_interval=5.0, robots_crawl_delay=10.0)
assert sig.recommended_interval >= 10.0   # robots.txt wins
```

The same record always produces the same signals — no hidden state — so you can
unit-test policies with a hand-built `HostRecord`:

```python
rec = HostRecord("example.com", rate_limit_count=3, last_status=429)
sig = derive_signals(rec, base_interval=1.0)
assert sig.rate_limited
assert sig.concurrency_factor < 1.0
assert sig.recommended_interval > 1.0
```

### `PolicySignals` fields

| Field | Range | Meaning |
|-------|-------|---------|
| `recommended_interval` | ≥ 0.0 | Suggested seconds between requests |
| `cooldown_secs` | 0..300 | How long to wait before the next attempt |
| `should_cooldown` | bool | `cooldown_secs > 0` |
| `rate_limited` | bool | `rate_limit_count > 0` or `last_status == 429` |
| `concurrency_factor` | 0.25..1.0 | Scales the concurrency cap; only rate limiting lowers it |
| `health` | 0.0..1.0 | `1.0` for a clean host; falls with errors and 429s |

A clean record (`error_count == 0`, `rate_limit_count == 0`, `last_status != 429`)
always yields `health == 1.0`, `should_cooldown == False`, `concurrency_factor == 1.0`,
and `recommended_interval == base_interval`.

### `SignalAdvisor` — read-only bridge

`SignalAdvisor` wraps a `SiteStore` and vends `PolicySignals` for any host without
ever writing to the store. Use it when you want policy lookups across many hosts
without mutation risk.

```python
advisor = SignalAdvisor(store, base_interval=5.0)
sig = advisor.for_host("example.com")

# per-call overrides (store defaults apply if not given)
sig2 = advisor.for_host("slow-site.com", robots_crawl_delay=30.0)
```

---

## LearnedRateLimiter

`LearnedRateLimiter` wires `SiteStore`, `SignalAdvisor`, and `ujin.robots`
`Crawl-delay` together into a **self-calibrating async governor**: AIMD concurrency
control, a per-host token bucket, and an adaptive interval — no external dependencies.

```python
import asyncio
import time
from ujin.adapt import SiteStore, LearnedRateLimiter
from ujin.robots import RobotsPolicy

store = SiteStore("site_state.db")

# robots is duck-typed: anything with .crawl_delay(host) -> float | None
robots = RobotsPolicy("User-agent: *\nCrawl-delay: 2\n")
gov = LearnedRateLimiter(store, robots=robots, base_interval=1.0)

async def fetch_with_pacing(host: str) -> None:
    t0 = time.monotonic()
    async with gov.acquire(host):          # waits for pacing + concurrency slot
        await asyncio.sleep(0)             # replace with your real fetch
        status = 200
    elapsed = time.monotonic() - t0

    gov.observe(host, status=status, latency=elapsed)

asyncio.run(fetch_with_pacing("example.com"))
print(f"interval={gov.interval_for('example.com'):.1f}s  "
      f"concurrency={gov.concurrency_for('example.com')}")
```

### Constructor parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `store` | required | `SiteStore`, or any object with `get(host)` / `record(host, **signals)` |
| `robots` | `None` | Any object with `crawl_delay(host) -> float \| None`; optional |
| `base_interval` | `0.0` | Seconds between requests for healthy hosts (0 = no artificial pacing) |
| `clock` | `time.monotonic` | Injectable for deterministic tests |
| `sleep` | `asyncio.sleep` | Injectable for deterministic tests |
| `max_concurrency` | `8` | Full concurrency cap per host |
| `max_interval` | `3600.0` | Learned interval never exceeds this value |

### `observe()` — feed responses back in

```python
# Clean response → relaxes interval and concurrency toward baseline
gov.observe("example.com", status=200, latency=0.4)

# 429 → raises interval, throttles concurrency
gov.observe("example.com", status=429)

# Network / 5xx error → throttles concurrency; does not speed up interval
gov.observe("example.com", error=True)

# Pass observed Crawl-delay from robots.txt (persists to the store too)
gov.observe("example.com", status=200, crawl_delay=2.0)
```

### Query methods

```python
gov.interval_for("example.com")     # float: effective seconds between requests
gov.concurrency_for("example.com")  # int: effective max in-flight requests
gov.cooldown_for("example.com")     # float: suggested rest period in seconds
```

The effective interval is **never below** `max(observed crawl_delay, robots.crawl_delay(host))`
when either is set, so robots.txt constraints are always respected.

### `acquire` — async gate

```python
# Recommended: context-manager (releases the slot on any exception)
async with gov.acquire(host):
    resp = await fetch(...)

# Manual form (if you need finer control)
gate = await gov.acquire(host)
try:
    resp = await fetch(...)
finally:
    await gate.release()
```

### Injecting clock and sleep for tests

```python
ticks = [0.0]
slept: list[float] = []

async def fake_sleep(secs: float) -> None:
    ticks[0] += secs
    slept.append(secs)

store = SiteStore()
gov = LearnedRateLimiter(
    store,
    base_interval=1.0,
    clock=lambda: ticks[0],
    sleep=fake_sleep,
)
# drive the governor without touching the real wall clock
```

---

## StrategyFeedback and StrategyOutcome

`StrategyFeedback` tracks **which (backend, render_mode) pair works best** per host
and recommends the proven winner for the next request.

A *strategy* is a `(backend, render_mode)` tuple, e.g. `("aiohttp", "none")` or
`("playwright", "js")`.

```python
from ujin.adapt import StrategyFeedback, StrategyOutcome

fb = StrategyFeedback("feedback.db")    # or ":memory:"

# Record outcomes as they arrive
fb.record("example.com", ("aiohttp", "none"),  ok=True,  latency=0.3)
fb.record("example.com", ("aiohttp", "none"),  ok=True,  latency=0.4)
fb.record("example.com", ("playwright", "js"), ok=False, latency=3.0)

# Ask for the recommended strategy
winner = fb.recommend("example.com")
print(winner)                          # ("aiohttp", "none")

# Unknown host → None; caller falls back to its own default
print(fb.recommend("never-seen.com"))  # None

fb.close()
```

### `StrategyOutcome` fields

| Field | Type | Description |
|-------|------|-------------|
| `host` | `str` | Hostname key |
| `strategy` | `tuple[str, str]` | `(backend, render_mode)` |
| `attempts` | `int` | Total attempts recorded |
| `successes` | `int` | Total successes |
| `failures` | `int` | Total failures |
| `p50_latency` | `float` | Running median latency (last 128 samples) |
| `last_latency` | `float` | Most recent latency |
| `last_seen` | `float` | Unix timestamp of last `record()` call |

### Penalization check

Skip a strategy before attempting it when the host is currently rate-limited or
unhealthy — without any extra I/O:

```python
from ujin.adapt import SiteStore, StrategyFeedback

store = SiteStore("site_state.db")
fb = StrategyFeedback("feedback.db")

host = "example.com"
strategy = ("playwright", "js")
rec = store.get(host)

if fb.is_penalized(host, strategy, rec):
    # fall back to the proven winner (or a safe default)
    strategy = fb.recommend(host) or ("aiohttp", "none")
```

`is_penalized` is **pure (no I/O)**: it calls `derive_signals(record)` internally
and returns `True` when `rate_limited` is set or `health < 0.5`.

### The closed loop in the scrape service

`ScrapeService` can drive `StrategyFeedback` end-to-end so the `auto` backend
order *learns* per host. It is **opt-in and off by default** — a no-config scrape
is byte-identical to before. Two `ScrapeConfig` fields (and their env aliases)
turn it on:

| Config field | Env var | Default | Meaning |
|--------------|---------|---------|---------|
| `learn_strategy` | `UJIN_LEARN_STRATEGY` | `False` | Enable the feedback loop |
| `strategy_db` | `UJIN_STRATEGY_DB` | `""` | SQLite path; empty → ephemeral `:memory:` |

```python
from ujin.scrape.config import ScrapeConfig
from ujin.scrape.build import build_scrape_service

# Durable across restarts: outcomes accumulate in strategy.db.
cfg = ScrapeConfig(learn_strategy=True, strategy_db="strategy.db")
service, comps, aclose = await build_scrape_service(cfg)
# ... service.scrape(...) ...
await aclose()        # checkpoints + closes the StrategyFeedback store
```

When enabled, `build_scrape_components` constructs one shared `StrategyFeedback`
(durable at `strategy_db`, or `:memory:` when unset — what the tests use) and
`close_scrape_components` (called by the returned `aclose`) checkpoints and closes
it on shutdown. The service then:

- **Biases the first attempt.** On the `auto` path (no `render=`/per-host pin), it
  consults `recommend(host)` and tries that proven-best `(backend, render_mode)`
  first — e.g. a host that has been winning on `obscura` skips the HTTP attempt.
- **Skips penalized strategies.** If an injected `SiteStore` reports the host as
  rate-limited or unhealthy, `is_penalized()` suppresses the bias and the request
  falls back to the normal auto order. Biasing **never raises**: a broken or
  closed store degrades to the default order.
- **Records every outcome.** After each fetch attempt — HTTP, obscura, or browser,
  success *or* failure — it calls `record(host, strategy, ok=..., latency=...)`,
  so the next request's recommendation reflects what just happened. The loop
  closes: an HTTP `403` that escalates to obscura records `http=fail` + `obscura=ok`,
  and the *next* scrape biases obscura first.

With `learn_strategy=False` (the default) none of this runs: nothing is recorded
and the auto order is untouched.

---

## ujin.robots — robots.txt policy

`ujin.robots` provides a pure parser and an optional async fetch + TTL cache.
`Crawl-delay` values flow directly into `LearnedRateLimiter`.

```python
from ujin.robots import RobotsPolicy, RobotsCache

# Parse from text — no I/O, no network
text = """
User-agent: *
Disallow: /private/
Crawl-delay: 2

Sitemap: https://example.com/sitemap.xml
"""
policy = RobotsPolicy(text)
policy.is_allowed("/public/page")     # True
policy.is_allowed("/private/secret")  # False
policy.crawl_delay()                  # 2.0
policy.sitemaps                       # ["https://example.com/sitemap.xml"]

# Agent-specific lookup (falls back to the '*' group if no specific group)
policy.is_allowed("/page", agent="Googlebot")
policy.crawl_delay("Googlebot")       # float | None

# Allow-all convenience (empty/missing/malformed file → allow-all)
permissive = RobotsPolicy.allow_all()
```

### Async fetch + TTL cache (requires `ujin[web]`)

```python
# Live fetching
cache = RobotsCache(ttl=3600)
policy = await cache.get("https://example.com")   # fetches /robots.txt; cached after
cache.invalidate("https://example.com")            # force re-fetch on next get()

# Test with injected fetcher + clock
t = [0.0]

async def fake_fetch(url: str) -> str:
    return "User-agent: *\nCrawl-delay: 5\n"

cache = RobotsCache(ttl=60, fetcher=fake_fetch, clock=lambda: t[0])
policy = await cache.get("https://example.com")   # uses fake_fetch
t[0] = 61.0                                        # expire the TTL
policy = await cache.get("https://example.com")   # fake_fetch called again
```

`robots` is duck-typed in `LearnedRateLimiter` — pass `RobotsPolicy`,
`RobotsCache`, or any object with `crawl_delay(host) -> float | None`:

```python
class FixedDelays:
    def crawl_delay(self, host: str) -> float | None:
        return {"slow.com": 5.0}.get(host)

gov = LearnedRateLimiter(store, robots=FixedDelays(), base_interval=1.0)
```

### robots auto-respect in `PollEngine`

Pass `respect_robots=True` (requires `adaptive=True`) to have the engine
automatically build a `RobotsCache` and wire it into the adaptive path.  No
external configuration is needed; the TTL and fetcher are injectable for tests.

```python
engine = PollEngine(
    adaptive=True,
    respect_robots=True,             # off by default
    robots_ttl=3600.0,               # re-fetch robots.txt after this many seconds (default 1 h)
    robots_fetcher=None,             # async (url) -> str; None = default aiohttp fetcher
)
```

**`Crawl-delay` floor** — every host's effective poll interval is raised to at
least the `Crawl-delay` declared in that host's robots.txt.  This is applied
through the existing `robots=` hook on `LearnedRateLimiter`, so it works the same
way as passing a `RobotsPolicy` directly.

**Disallow skip** — before each poll the engine checks whether the target URL's
path is allowed.  If the path is disallowed the engine:
- records the poll in `target.polls` (so stats are accurate)
- returns `PollResult(ok=True, changed=False)` without calling `pollable.poll()`
- does **not** advance backoff, circuit-breaker, or the penalty interval —
  cooldown/rate-limit state is unaffected

```python
# Any target whose URL matches a Disallow: rule is silently skipped.
# Targets whose URLs are allowed are fetched and paced normally.
engine.add(HttpPollable("https://example.com/public/page"), base=60)
engine.add(HttpPollable("https://example.com/private/data"), base=60)
# ^ the second target will be skipped every tick if robots.txt says Disallow: /private
```

**Injected fetcher for tests** (fully offline):

```python
async def fake_robots(url: str) -> str:
    return "User-agent: *\nCrawl-delay: 5\nDisallow: /private\n"

engine = PollEngine(
    adaptive=True,
    respect_robots=True,
    robots_fetcher=fake_robots,
)
```

---

## End-to-end example

A complete adaptive scraper loop — durable, self-calibrating, robots-aware — in
under 40 lines. Run it directly with `pip install -e .` (no extra dependencies):

```python
import asyncio
import time
from ujin.adapt import (
    SiteStore, HostRecord,
    derive_signals, PolicySignals, SignalAdvisor,
    StrategyFeedback,
    LearnedRateLimiter,
)
from ujin.robots import RobotsPolicy

URLS = ["https://example.com/a", "https://example.com/b"]

async def main() -> None:
    store = SiteStore(":memory:")           # swap for a path to persist across restarts
    fb = StrategyFeedback(":memory:")

    robots = RobotsPolicy("User-agent: *\nCrawl-delay: 1\n")
    gov = LearnedRateLimiter(store, robots=robots, base_interval=1.0)
    advisor = SignalAdvisor(store, base_interval=1.0)

    for url in URLS:
        host = url.split("/")[2]
        rec = store.get(host)

        # Check health before picking a strategy
        sig: PolicySignals = advisor.for_host(host)
        if sig.should_cooldown:
            await asyncio.sleep(sig.cooldown_secs)

        strategy = fb.recommend(host) or ("aiohttp", "none")
        if fb.is_penalized(host, strategy, rec):
            strategy = ("aiohttp", "none")

        t0 = time.monotonic()
        try:
            async with gov.acquire(host):
                await asyncio.sleep(0)       # replace with your real fetch
                status, ok = 200, True
        except Exception:
            status, ok = 0, False
        elapsed = time.monotonic() - t0

        gov.observe(host, status=status, latency=elapsed)
        fb.record(host, strategy, ok=ok, latency=elapsed)

        print(
            f"{host}: interval={gov.interval_for(host):.1f}s  "
            f"health={advisor.for_host(host).health:.2f}  "
            f"best={fb.recommend(host)}"
        )

    store.close()
    fb.close()

asyncio.run(main())
```

---

## All adaptive symbols at a glance

| Symbol | Import from | Purpose |
|--------|-------------|---------|
| `SiteStore` | `ujin.adapt` | Durable per-host SQLite state |
| `HostRecord` | `ujin.adapt` | Immutable per-host snapshot (frozen dataclass) |
| `derive_signals` | `ujin.adapt` | Pure `HostRecord → PolicySignals` (no I/O) |
| `PolicySignals` | `ujin.adapt` | Frozen recommendation struct |
| `SignalAdvisor` | `ujin.adapt` | Read-only `SiteStore → PolicySignals` bridge |
| `StrategyFeedback` | `ujin.adapt` | Per-host `(backend, render_mode)` outcome tracker |
| `StrategyOutcome` | `ujin.adapt` | Immutable per-(host, strategy) snapshot |
| `LearnedRateLimiter` | `ujin.adapt` | Self-calibrating async rate/concurrency governor |
| `RobotsPolicy` | `ujin.robots` | Pure robots.txt parser |
| `RobotsCache` | `ujin.robots` | Async fetch + TTL cache for robots.txt |

---

## See also

- [ROBOTS.md](ROBOTS.md) — `RobotsPolicy` / `RobotsCache` full reference
- [ARCHITECTURE.md](ARCHITECTURE.md) — control primitives, engine flow, internal layering
- [BACKENDS.md](BACKENDS.md) — aiohttp vs obscura vs playwright vs selenium
- [TESTING.md](TESTING.md) — deterministic test patterns, `FakeClock`, injected fixtures
