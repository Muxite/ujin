# ujin HTTP API reference

ujin ships **two independent HTTP services**. Run either or both:

| Service | Module / CLI | Default port | Purpose |
|---|---|---|---|
| **Poller control** | `ujin.service` / `ujin api` | `8900` | drive the adaptive poll engine (add/list/remove targets, sweep, live change stream) |
| **Scrape** | `ujin.scrape.app` / `ujin scrape-serve` | `8901` | rich one-shot scraping: render + extract + fallback chain + change fingerprint |

Both are FastAPI apps, so interactive docs are served at `/docs` and the OpenAPI
schema at `/openapi.json`.

---

## Scrape service (`:8901`)

Build the app with `create_scrape_app(config: ScrapeConfig, *, scorer=None)` or run
`ujin scrape-serve`. Configuration is read from the environment
(`ScrapeConfig.from_env()`) — see [Configuration](#configuration).

### `GET /health`
Liveness + renderer/cache state. Never fails.

```json
{ "ok": true, "status": "ok", "service": "ujin-scrape",
  "obscura_available": false,
  "cache": { "entries": 0, "max": 2048, "ttl_secs": 120 } }
```
`obscura_available` is `false` when no renderer is reachable; the service still
works (HTTP + altpath only). As of 0.4.0 every ujin service returns the same
`ok` / `status` / `service` trio.

### `GET /capabilities`
The fetch-backend capability matrix (http / obscura / playwright / selenium)
with live availability flags — see [BACKENDS.md](BACKENDS.md). Lets callers
decide which `render=` strategies are usable before scraping.

### Authentication (all services)
Off by default. Set `UJIN_API_KEY` and every request to :8900/:8901/:8902
(HTTP **and** WebSocket) must present `X-API-Key: <key>` or
`Authorization: Bearer <key>`; `/health` stays open for probes.

### `POST /scrape`
Render and extract a single page.

Request (`ScrapeRequest`):
| field | type | default | notes |
|---|---|---|---|
| `url` | string | — | absolute URL (required) |
| `mode` | `links`\|`article`\|`auto`\|`combined`\|`structured` | `links` | what to extract |
| `force_refresh` | bool | `false` | bypass cache + revalidation |
| `enrich_html_top_n` | int 0–20 | `0` | (combined) fan out article fetches for the top-N HTML-only links |

Modes:
- **links** — headline link-set for a homepage/section page.
- **article** — cleaned body text for a single article URL.
- **auto** — pick based on page shape.
- **combined** — RSS + HTML in parallel, merged by canonical URL.
- **structured** — JSON-LD / OpenGraph / microdata, returned in `structured`.

Response (`ScrapeResponse`, abridged):
```json
{ "url": "...", "kind": "links|article|structured|empty|error",
  "fingerprint": "sha256…", "fetched_at": 1780000000.0,
  "cached": false, "age_secs": 0.0, "used_renderer": false,
  "strategy_used": "http|http_304|obscura|sitemap_news|rss|cache|combined",
  "links": [ { "url": "...", "text": "...", "summary": "", "published": "",
               "seen_in": ["rss","html"], "tier": "generic",
               "breaking_score": 0.0, "score_components": {} } ],
  "article": null, "structured": null, "final_url": null, "note": null,
  "next_poll_hint_secs": 60.0, "max_breaking_score": 0.0 }
```
- `fingerprint` is a stable SHA-256 over the normalized payload — compare across
  calls to detect real change.
- `next_poll_hint_secs` is the scraper's suggested wait before re-polling.
- `tier` / `breaking_score` / `score_components` are neutral unless a
  `BreakingScorer` is wired (see [Scoring](#scoring--news-trading-mode)).

Errors: `400` empty url · `429` host on cooldown with no cache (`HostCooldown`) ·
`502` fetch/render/parse failure.

### `POST /scrape:batch`
Fan out many scrapes concurrently. Per-item failures come back inline as
`kind:"error"` (the batch as a whole still returns 200).

```json
{ "requests": [ { "url": "https://a.example", "mode": "links" },
                { "url": "https://b.example", "mode": "structured" } ] }
```
`400` if the batch exceeds `batch_max_items` (default 64).

### `POST /feed`
Parse an RSS/Atom feed (no rendering, no cache). `{ "url": "…/rss.xml" }` →
`{ "items": [ { "url", "title", "summary", "published" } ] }`.

### `POST /sitemap`
Parse a sitemap XML (`<urlset>`, `<sitemapindex>`, news-sitemap).
`{ "url": "…/news-sitemap.xml" }` → `{ "entries": [ { "url", "lastmod", "title" } ] }`.

### `POST /discover`
Probe a homepage for feed/sitemap URLs. `{ "homepage": "https://site" }` →
`{ "homepage", "rss": [...], "sitemap": [...] }`.

### `GET /metrics`
Per-host fetch counters + latency percentiles:
`{ "total_fetches", "hosts": { "host": { fetches, successes, failures,
renderer_used, cached_returns, fallback_used, latency_ms_p50, latency_ms_p95,
samples, last_seen } } }`.

### Social + trends (optional)
These degrade gracefully when unconfigured.

| Endpoint | Body | Notes |
|---|---|---|
| `POST /social/twitter` | `{username, count}` | Brave Search; `503` without `SEARCH_API_KEY` |
| `POST /social/mastodon` | `{account:"@u@instance", count}` | public API, no key |
| `POST /social/x` | `{username, count, allow_brave}` | nitter → syndication → brave chain; response carries `leg` |
| `POST /social/truth` | `{username, count}` | per-user public RSS |
| `POST /trends/x` | `{region, count}` | scraped trending tags; `source` ∈ trends24/getdaytrends/empty |
| `GET  /trends/corroborated` | — | cross-source headline clusters; empty unless a corroboration store is wired |

Social responses are `{ "posts": [ { "url", "text" } ] }` (X adds `"leg"`).

---

## Poller control service (`:8900`)

Drives `ujin.engine.PollEngine` and streams change events. Run `ujin api`.

| Endpoint | Purpose |
|---|---|
| `GET /health` | `{ "ok": true, "status": "ok", "service": "ujin-poller", "targets": N }` |
| `GET /metrics` | engine stats snapshot (0.4.0: renamed from `/stats`) |
| `GET /targets` | `[ { key, interval, polls, changes, circuit } ]` |
| `POST /targets` | add a target: `{ kind, config, base?, min?, max?, jitter? }` → `{ key }` |
| `DELETE /targets/{key}` | remove a target |
| `POST /sweep` | poll all targets once; `{ changed: [...], targets: N }` |
| `WS /ws` | stream `{ "event":"change", "key", "fingerprint", "ts" }` |

`kind` ∈ `http` · `rss` · `api` · `command` · `site`. `config` is the kind's
constructor args, e.g. `{"url": "https://x"}`, or for `site`:
`{"url": "...", "selectors": ["main"], "render": false}`.

Example:
```bash
curl -X POST localhost:8900/targets -H 'content-type: application/json' \
  -d '{"kind":"http","config":{"url":"https://example.com"},"base":300}'
```

---

## Configuration

`ScrapeConfig` (env-driven via `from_env()`); field → env var:

| env var | default | meaning |
|---|---|---|
| `HTTP_TIMEOUT_SECS` | 15 | per-request HTTP timeout |
| `FETCH_TIMEOUT_SECS` | 30 | obscura render timeout |
| `SCRAPER_USER_AGENT` | ujin UA | User-Agent header |
| `PER_HOST_CONCURRENCY` | 2 | concurrent requests per host |
| `HOST_COOLDOWN_SECS` | 60 | base cooldown after 429/5xx (exponential) |
| `FAST_PATH_MIN_LINKS` | 5 | min links before falling back to obscura/altpath |
| `CACHE_MAX_ENTRIES` / `CACHE_TTL_SECS` | 2048 / 120 | in-memory cache |
| `DISK_CACHE_PATH` | — | SQLite durable cache (empty = off) |
| `PER_HOST_CONFIG_PATH` | — | `per_host.yaml` extractor/strategy overrides |
| `BATCH_MAX_ITEMS` | 64 | `/scrape:batch` cap |
| `OBSCURA_URL` / `OBSCURA_BIN` | — | headless renderer (URL service or binary path) |
| `SEARCH_API_KEY` | — | Brave token for the social legs |
| `NITTER_POOL_PATH` | — | YAML list of nitter mirrors |
| `UJIN_BREAKING_SCORER` | `0` | wire the news-trading scorer (below) |
| `CORROBORATION_*`, `TIER_W_*`, `BREAKING_THRESHOLD` | — | scorer tuning |

### The fallback chain
For a links-mode page the scrape service walks: **HTTP fast path** → (too few
links / 4xx-5xx) **obscura render** → **altpath** (news-sitemap, then a pinned
RSS). `strategy_used` reports which leg answered. Per-host cooldown
short-circuits hosts that recently failed (serving cache if present, else `429`).

### Scoring / news-trading mode
By default a `NullScorer` yields neutral `tier`/`breaking_score` and a generic
churn-based `next_poll_hint_secs`. Set `UJIN_BREAKING_SCORER=1` to wire a
`BreakingScorer` (source tier + lede markers + recency + cross-source
corroboration + X-trend overlap) plus a background X-trends refresh loop and a
corroboration store. The wire shape is identical either way.

---

## Renderer (obscura)

`obscura` is a Rust headless browser bundled as a git submodule at
`ujin/obscura`. ujin invokes it as a **binary** (`obscura fetch <url> --dump
html`); resolution order is `OBSCURA_URL` → `OBSCURA_BIN` → the bundled build
(`ujin/obscura/target/release/obscura`) → `obscura` on `PATH`. When none
resolve, rendering is skipped and the service degrades to HTTP + altpath.

Build it (needs Rust; first build compiles V8, ~15–20 min):
```bash
ujin obscura-build           # git submodule init + cargo build --release
```
Or use the `ujin-full` Docker target / `render` compose profile (see README).
