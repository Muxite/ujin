# Fetch backends: capabilities and limitations

ujin can fetch a page four ways. The orchestrator (`ScrapeService`) escalates
through them automatically (`render="auto"`), or a caller can pin one
(`render="http" | "obscura" | "browser"`). This document is the human-readable
form of the machine-readable matrix in `ujin/fetch/capabilities.py`, exposed
live at `GET :8901/capabilities` (and via the MCP `get_capabilities` tool). To
see which backends are installed **on your machine** right now — with the pip
command to enable each missing one — run **`ujin doctor`**.

## Comparison

| | **aiohttp (http)** | **obscura** | **playwright** | **selenium-chromedriver** |
|---|---|---|---|---|
| JavaScript | none — raw HTML only | full (static snapshot after JS runs) | full | full |
| Interaction (click / fill / `load_more` / scroll / eval_js / screenshots) | no | no | **yes** | yes |
| Anti-bot evasion | **low** — Python TLS fingerprint, fails JS challenges (WSJ/NYT-class WAFs 403 it) | medium — real JS engine defeats JS challenges, headless signals remain | medium — headless Chromium is fingerprintable (`navigator.webdriver`, canvas) | **low** — the webdriver flag is the most widely detected automation signal |
| Conditional GET (ETag / 304) | **yes** | no | no | no |
| Speed (typical warm latency) | ~50 ms | ~0.8–2 s | ~1.5–4 s | ~2–6 s |
| Local throughput (measured, see `benchmarks/`) | 32 parallel GETs in ~4.4 ms against a local origin | n/a (process/service per render) | n/a | n/a |
| Memory per concurrent page | ~1 MB | ~150 MB | ~300 MB | ~350 MB |
| Sane per-process concurrency | 64 (TCP pool) | ~4 | ~4 contexts | **1** (WebDriver is not thread-safe; ujin marshals it onto a single thread) |
| Install weight | pure pip (`ujin[web]`) | Rust toolchain + `ujin obscura-build` (~15–20 min first build, compiles V8) — or point `OBSCURA_URL` at a running service | `ujin[browser]` + `playwright install chromium` (~280 MB) | `ujin[browser]` + system chrome/chromium + matching chromedriver |
| Ships in Docker target | `ujin` (default) | `ujin-full` | `ujin-browser` | `ujin-browser` |

## Limitations in detail

**aiohttp (`http`)** — the default first leg. Cannot see anything rendered
client-side: an SPA shell comes back as `<div id="root"></div>` and yields
fewer than `fast_path_min_links` links, which is exactly the signal the
orchestrator uses to escalate. Cheapest by ~2 orders of magnitude and the only
backend with ETag/If-Modified-Since revalidation, so polling loops should
always let the HTTP leg run first. Sites whose WAF fingerprints TLS or demands
a JS proof-of-work will 403/429 it no matter what headers you send.

**obscura** — the bundled Rust headless renderer. Executes JavaScript and
returns the settled DOM, so it defeats JS-challenge walls and renders SPAs,
but it is a *static snapshot*: no clicking, no pagination, no login flows. No
cookie persistence between renders. Resolution order: `OBSCURA_URL` (HTTP
service) → `OBSCURA_BIN` → bundled `ujin/obscura/target/release/obscura` →
`obscura` on PATH. It is never built in CI or at pip-install time; when absent,
`obscura_available()` is false and the orchestrator skips the leg.

**playwright** — the default `render="browser"` engine. The only
fully-featured path: interaction recipes (`goto`, `click`, `fill`, `press`,
`wait_for_selector`, `eval_js`, `screenshot`, and crucially `load_more` /
`scroll_to_bottom` for click-to-paginate listings), per-context cookies,
proxies, custom user agents. Costs hundreds of MB per page and seconds per
render — which is why it is **never an automatic fallback**; you opt in with
`render="browser"` or a `browser` job source. Headless Chromium is still
detectable by serious anti-bot vendors (Datadome, PerimeterX): expect
captchas where those run.

**selenium-chromedriver** — the alternate recipe engine, useful where
playwright can't be installed or an existing chromedriver/grid must be reused.
Same recipe primitives, but the blocking WebDriver API is marshalled onto a
single dedicated thread, so one fetcher = one page at a time, and the
`navigator.webdriver` flag makes it the easiest backend to detect. Prefer
playwright unless you have a concrete reason.

## The escalation chain (`render="auto"`)

```
HTTP GET (aiohttp)
  ├─ 304 Not Modified ─────────────► serve cache
  ├─ 200 + ≥ fast_path_min_links ──► extract & return        (strategy: http)
  ├─ 200 + thin page, or 4xx/5xx ──► obscura render          (strategy: obscura)
  │                                    └─ unavailable/failed ─► altpath:
  │                                         news-sitemap ─────► (strategy: sitemap_news)
  │                                         discovered RSS ───► (strategy: rss)
  └─ host on cooldown ─────────────► cache, else 429
browser: only when pinned (render="browser") — never auto
```

Which leg answered comes back in `ScrapeResult.strategy_used`, and per-host
escalations are visible in `GET /metrics`.

## Choosing a backend

- Polling many pages for *change detection* → `http` (let auto-escalation
  handle the stragglers). It's the only leg with 304 revalidation.
- JS-rendered site, read-only → `obscura` (or `OBSCURA_URL` pointing at a
  shared render service so workers stay pure-python).
- Click-to-load listings, logins, infinite scroll → `playwright` with a recipe
  (`actions=[{"action": "load_more", ...}]`).
- Hard anti-bot wall (WSJ-class) → don't fight the front door: the altpath
  chain (news-sitemap / RSS) usually has the same content unprotected, because
  that's how Google News ingests them.
