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
| `url` | string | — | absolute URL (required unless `urls` is set) |
| `urls` | list of string | `null` | multi-URL batch: scrape several URLs, one result per URL (see below) |
| `mode` | `links`\|`article`\|`auto`\|`combined`\|`structured`\|`tables`\|`images` | `links` | what to extract (single mode) |
| `modes` | list of `links`\|`article`\|`auto`\|`structured`\|`tables`\|`images`\|`html` | `null` | multi-extract: several modes over one fetch (see below) |
| `force_refresh` | bool | `false` | bypass cache + revalidation |
| `enrich_html_top_n` | int 0–20 | `0` | (combined) fan out article fetches for the top-N HTML-only links |

Modes:
- **links** — headline link-set for a homepage/section page.
- **article** — cleaned body text for a single article URL.
- **auto** — pick based on page shape.
- **combined** — RSS + HTML in parallel, merged by canonical URL.
- **structured** — JSON-LD / OpenGraph / microdata, returned in `structured`.
- **tables** — every HTML `<table>` parsed into header-keyed row dicts, returned in `tables`.
- **images** — every `<img>` as a normalized dict (absolute `src`, `alt`,
  optional `width`/`height`/`title`), returned in `images`. Relative srcs are
  resolved against the page URL; lazy-load `data-src`/`data-original` and the
  first `srcset` candidate are honored, a `data:` placeholder is skipped when a
  real src exists, and identical srcs are de-duplicated in document order.
- **html** — the raw fetched HTML, returned in `html` (multi-extract only).

Response (`ScrapeResponse`, abridged):
```json
{ "url": "...", "kind": "links|article|structured|tables|images|html|empty|error",
  "fingerprint": "sha256…", "fetched_at": 1780000000.0,
  "cached": false, "age_secs": 0.0, "used_renderer": false,
  "strategy_used": "http|http_304|obscura|browser|sitemap_news|rss|combined|cache",
  "links": [ { "url": "...", "text": "...", "summary": "", "published": "",
               "seen_in": ["rss","html"], "tier": "generic",
               "breaking_score": 0.0, "score_components": {} } ],
  "article": null, "structured": null, "tables": null, "images": null,
  "html": null, "final_url": null, "note": null,
  "next_poll_hint_secs": 60.0, "max_breaking_score": 0.0,
  "extracts": null, "batch": null }
```
- `fingerprint` is a stable SHA-256 over the normalized payload — compare across
  calls to detect real change.
- `next_poll_hint_secs` is the scraper's suggested wait before re-polling.
- `tier` / `breaking_score` / `score_components` are neutral unless a
  `BreakingScorer` is wired (see [Scoring](#scoring--news-trading-mode)).

#### Multi-extract (`modes`)
Set `modes` (instead of, or alongside, `mode`) to run several extract modes over
a **single fetch** and get a result per mode:
```json
{ "url": "https://apnews.com", "modes": ["links", "structured", "html"] }
```
The page is fetched once, each mode is extracted from that same body, and the
per-mode `ScrapeResponse`s come back under a new `extracts` map keyed by mode:
```json
{ "kind": "links", "links": [ … ],            // top-level mirrors the FIRST listed mode
  "extracts": {
    "links":      { "kind": "links",      "links": [ … ], "extracts": null },
    "structured": { "kind": "structured", "structured": { … }, "extracts": null },
    "html":       { "kind": "html",       "html": "<html>…", "extracts": null } } }
```
- Each mode is isolated: a mode whose extractor fails appears with
  `kind:"error"` (its message in `note`) and never fails the others.
- Duplicate modes are de-duplicated, first-seen order preserved; the top-level
  fields echo the first listed mode so single-mode clients still see a coherent
  body. Nested `extracts` are always `null` (one level only).
- `combined` is single-`mode` only (it runs its own RSS+HTML fan-out) and is not
  accepted in `modes`. Pagination (`page_size`/`cursor`) is ignored for
  multi-extract requests. Omitting `modes` leaves the single-`mode` path and its
  response byte-for-byte unchanged.

#### Multi-URL batch (`urls`)
Set `urls` (instead of `url`) to scrape several URLs in **one request** and get
one result per URL:
```json
{ "urls": ["https://apnews.com", "https://www.reuters.com/world/"], "mode": "links" }
```
The URLs are fetched concurrently under a bounded concurrency cap
(`BATCH_MAX_CONCURRENCY`, default 8). The per-URL `ScrapeResponse`s come back in
request order under a new `batch` list, and the top-level fields mirror the
**first** URL's result so a naive client still sees a coherent single-page body:
```json
{ "url": "https://apnews.com", "kind": "links", "links": [ … ],  // mirrors the FIRST URL
  "batch": [
    { "url": "https://apnews.com",            "kind": "links", "links": [ … ], "batch": null },
    { "url": "https://www.reuters.com/world/", "kind": "links", "links": [ … ], "batch": null } ] }
```
- Each URL is isolated: a URL whose fetch/parse fails appears with `kind:"error"`
  (its message in `note`) and never fails the others. Order is always preserved.
- Every URL is scraped with the request's `mode`, `force_refresh`, `render`,
  `actions`, and `enrich_html_top_n`. The batch form is single-`mode` — the
  `modes` multi-extract map and `page_size`/`cursor` pagination are not applied
  per URL — and nested `batch` is always `null` (one level only).
- `400` if the list exceeds `batch_max_items` (default 64). Omitting `urls`
  leaves the single-`url` path and its response byte-for-byte unchanged.
- Use `POST /scrape:batch` instead when you need *different* `mode`/`force_refresh`
  per item; use `urls` here when scraping many URLs the same way.

Errors: `400` empty url (no `url` and no `urls`) · `429` host on cooldown with no
cache (`HostCooldown`) · `502` fetch/render/parse failure.

### `POST /scrape:batch`
Fan out many scrapes concurrently. Per-item failures come back inline as
`kind:"error"` (the batch as a whole still returns 200). Each item honours only
`url`, `mode`, and `force_refresh`; per-item `render`/`actions`/`page_size`/
`cursor`/`enrich_html_top_n` are ignored in batch mode — use single `POST /scrape`
for those.

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
| `GET /content?key=…` | the body ujin last fetched for a target (reuse it instead of re-hitting the origin): `{ key, changed, fingerprint, ts, status, body }`; `404` until that target has polled |
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
| `BATCH_MAX_ITEMS` | 64 | batch size cap (`/scrape:batch` and `POST /scrape` `urls`) |
| `BATCH_MAX_CONCURRENCY` | 8 | max scrapes run concurrently inside one batch fan-out |
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
