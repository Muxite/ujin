# ujin

The **ultimate scraper-poller**. Three parts that share one toolkit:

1. **Adaptive multi-role poller** — poll *anything* (HTTP pages, RSS, JSON APIs,
   GraphQL endpoints, shell commands, Python callables, scoped page regions, or
   browser-driven interaction recipes) on a cadence that **adapts** to change,
   with **jitter** so aggregate load stays smooth.
2. **Rich scrape service** — one-shot rendering + extraction with an
   HTTP → obscura → sitemap → RSS fallback chain, per-host cooldown,
   fingerprinted change detection, structured-data + HTML-table + image +
   head-metadata + feed-URL extraction, and optional social/trends sources —
   all behind a small HTTP API.
3. **Declarative jobs control plane** — `source → transforms → sinks → schedule`
   driven by REST or file-based config: shared `defaults:` deep-merged under
   every job, reusable `include:`/`use:` fragment files, and `matrix:`/`for_each:`
   fan-out to turn one template into N jobs; or mount a single **INGEST-PLAN**
   file with `--plan` / `$UJIN_INGEST_PLAN` for one-file multi-job deploys.
   Validate and inspect offline with `ujin plan validate` / `ujin plan render`
   before committing to a deploy.

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

## Quick start — 60 seconds

```bash
pip install -e ".[web]"      # HTTP/RSS/API roles + scrape toolkit
ujin doctor                  # what's installed and what each backend unlocks
ujin init                    # writes a commented starter targets.yaml
ujin sweep targets.yaml      # one pass; prints which targets changed
```

`ujin init` scaffolds a ready-to-run `targets.yaml` (HTTP page, RSS feed, JSON
API, shell command). `ujin sweep` polls them once and exits; `ujin serve` runs
the same file as an adaptive, jittered daemon. `ujin --help` lists every command;
each subcommand's `--help` carries usage examples.

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

# Opt-in learned pacing: per-host interval + concurrency that calibrate from
# observed status/latency and survive a restart (default off, in-process store):
#   PollEngine(adaptive=True)                       # in-memory SiteStore
#   PollEngine(adaptive=True, site_store_path="state.db")  # persist + warm-restart
#
# Opt-in robots.txt auto-respect (requires adaptive=True):
#   PollEngine(adaptive=True, respect_robots=True)
#     — builds a RobotsCache; Crawl-delay floors the learned per-host interval;
#       any URL whose path is Disallow-ed is silently skipped (not a failure).
#   robots_ttl=3600.0   configures the robots.txt cache TTL (default 1 h)
#   robots_fetcher=fn   inject an async fetcher for fully-offline tests
```

## Quick start — scraping

```python
from ujin.scrape.service import ScrapeService
# or just run the HTTP service:  ujin scrape-serve   (POST /scrape, /feed, ...)
```
```bash
curl -X POST localhost:8901/scrape -H 'content-type: application/json' \
  -d '{"url":"https://apnews.com","mode":"links"}'

# multi-extract: fetch once, get several modes back under `extracts`
# (`tables` parses every <table> into header-keyed row dicts in `tables`;
#  `images` parses every <img> into normalized dicts in `images`;
#  `metadata` returns a flat head-metadata summary in `metadata`;
#  `feeds` returns declared <link rel="alternate"> feed URLs in `feeds`;
#  `contacts` returns emails, phones, and social/profile links in `contacts`)
curl -X POST localhost:8901/scrape -H 'content-type: application/json' \
  -d '{"url":"https://apnews.com","modes":["links","structured","tables","images","metadata","feeds","contacts","html"]}'

# multi-URL batch: scrape many URLs concurrently, one result per URL under `batch`
curl -X POST localhost:8901/scrape -H 'content-type: application/json' \
  -d '{"urls":["https://apnews.com","https://www.reuters.com/world/"],"mode":"links"}'
```

## Quick start — jobs and file-driven config

```bash
ujin jobs-serve --plan examples/ingest-plan.yaml   # :8902 — many jobs from one file
ujin plan validate examples/ingest-plan.yaml        # validate plan before deploying (exit 0 = ok)
ujin plan render   examples/ingest-plan.yaml        # print fully-expanded job specs (--json for CI)

# Inject secrets without committing them — file supplies vars not already in the environment:
ujin plan validate examples/ingest-plan.yaml --env-file .env
ujin plan render   examples/ingest-plan.yaml --env-file .env
ujin jobs-serve    --plan examples/ingest-plan.yaml --env-file .env

# Job-id selectors — filter after full resolution (defaults, includes, matrix fan-out):
ujin plan render  examples/ingest-plan.yaml --only 'feed-*'            # matching ids only
ujin plan render  examples/ingest-plan.yaml --only 'feed-*' --exclude feed-tech
ujin jobs-serve   --plan examples/ingest-plan.yaml --only 'feed-*'     # preload subset

# Provenance — show which source file and matrix row produced each expanded job:
ujin plan render  examples/ingest-plan.yaml --explain                  # `# source:` / `# matrix:` comments
ujin plan render  examples/ingest-plan.yaml --explain --json           # machine-readable provenance
```

`--plan` (or `$UJIN_INGEST_PLAN`) mounts a single YAML file that declares many jobs
at once. Three additive layers keep large plans DRY:

- **`defaults:`** — a top-level mapping deep-merged under every job; per-job keys win.
- **`include:`/`use:`** — pull in a fragment file for a shared sink, schedule, or
  transform pipeline; paths resolve relative to the plan file, then `$UJIN_WORKFLOWS_DIR`.
- **`matrix:`/`for_each:`** — a list of variable maps (inline, or a `{ from: <path> }`
  data file resolved like an `include:` fragment); ujin substitutes each into
  `{{ var }}` placeholders across the source/transforms/sinks/schedule, emitting one
  job per entry with a stable, deterministic id so reloads upsert rather than duplicate.
- **`${VAR}` / `${VAR:-default}` / `${VAR:?message}`** — environment substitution
  expanded at file-read time. `${VAR:?message}` raises a clear error (naming the
  variable and including `message`) when `VAR` is unset or empty, so missing secrets
  are caught early rather than silently expanding to an empty string.

`ujin plan validate` and `ujin plan render` accept `--only PATTERN[,PATTERN…]` and
`--exclude PATTERN[,PATTERN…]` (fnmatch-style globs, comma-separated or repeatable)
to filter the resolved jobs **after** all resolution layers. `jobs-serve --plan` also
accepts both flags and reflects only the selected jobs in `GET /health`. A `--only`
pattern that matches nothing exits non-zero with a clean `ujin: …` message.

`ujin plan validate` checks a plan file **or** a workflows directory with the same
loaders as `jobs-serve` — identical ids and error messages — without starting the
server. `ujin plan render` prints every fully-expanded `JobSpec` after all resolution
layers so you can verify matrix fan-out and defaults merges before deploying. Add
`--explain` to annotate each job with its provenance — the originating source file
and, for matrix/for_each-expanded jobs, the matrix entry index and variable map that
produced it (as `#` comments in YAML, or a `provenance` object with `--json`); the
default render output is unchanged without the flag.

Changed the plan file? `POST /plan/reload` re-reads it and upserts the resolved jobs
in-process — no restart — sharing the startup loader (a single bad job is reported,
the rest reload) and refreshing the `/health` plan status; with no plan configured
it is a safe no-op. Add `?prune=true` (or a `{"prune": true}` body) to also remove
plan-owned jobs dropped from the file (returned in a `pruned` list); API- and
preload-created jobs are never pruned. See [docs/INGEST_PLAN.md](docs/INGEST_PLAN.md), [docs/WORKFLOWS.md](docs/WORKFLOWS.md),
and the [external-config recipe](docs/recipes/external-config-flow.md).

## Watch a page for change

```bash
ujin watch https://example.com --selector main --webhook https://hooks/me
```
Fingerprints only the regions matched by your selectors, so cosmetic churn
elsewhere doesn't trip the watcher. Drives the same adaptive engine.

## Jobs — configure almost any task over REST

The unified control plane turns "watch this source, filter it, send it somewhere,
on a timer" into a single durable, restart-surviving job — **source → transforms →
sinks → schedule** — with no code for the common cases. See [docs/JOBS.md](docs/JOBS.md).

```bash
ujin jobs-serve                              # :8902, durable sqlite jobstore
ujin jobs-serve examples/jobs.crossref.yaml  # preload jobs from YAML
```

```bash
# Poll the Crossref API for new papers — filtered, deduped, jittered. Pure config.
curl -X POST localhost:8902/jobs -H 'content-type: application/json' -d '{
  "name": "crossref",
  "source": {"kind": "api", "config": {
    "url": "https://api.crossref.org/works?query=quantum&rows=50",
    "json_path": "message.items"}},
  "transforms": [{"kind": "select", "config": {"where": {"type": "journal-article"},
                                               "fields": ["DOI","title"]}},
                 {"kind": "dedupe", "config": {"key": "DOI"}}],
  "sinks": [{"kind": "jsonl", "config": {"path": "/data/crossref.jsonl"}}],
  "schedule": {"mode": "adaptive", "base": 3600, "min": 600, "max": 86400}
}'
```

Prefer files over API calls? Drop workflow definitions into the mounted
workflows directory (`$UJIN_WORKFLOWS_DIR`, default `/workflows`; override per run
with `--workflows`) — each file is one workflow, the **filename stem is its id**,
and ujin sets it up on startup, runs it, and hands back what it obtained at
`GET /jobs/{id}/content` (latest) and `/jobs/{id}/results` (recent buffer). Keep
many similar workflows DRY with an optional top-level `defaults:` block (deep-merged
under each job, per-job keys win) and `include:`/`use:` fragment files for shared
sinks, schedules, or transform pipelines; or fan one template into many with a
`matrix:`/`for_each:` key — a list of variable maps (inline, or a `{ from: <path> }`
data file), each substituted into the `source`/`transforms`/`sinks`/`schedule`, with
a stable per-entry id so reloads upsert rather than duplicate.
See [docs/WORKFLOWS.md](docs/WORKFLOWS.md).

Prefer one file over a directory? Mount a single **INGEST-PLAN** declaring many
jobs at once — a top-level list of jobs, or a mapping with `jobs:` plus a shared
`defaults:` block — and point ujin at it with `--plan` or `$UJIN_INGEST_PLAN`. It
reuses the same `defaults:`/`include:`/`matrix:` layers and stable ids, so
re-loading upserts; a malformed plan or job is reported in `GET /health` without
aborting startup. See [docs/INGEST_PLAN.md](docs/INGEST_PLAN.md).

Need something the built-ins don't cover? Drop a Python file into the mounted
`/plugins` volume and it becomes a `plugin:<name>` source/transform/sink — see
[docs/PLUGINS.md](docs/PLUGINS.md). Sibling projects can also extend ujin in-tree
with first-class kinds — see [docs/CAPABILITIES.md](docs/CAPABILITIES.md).

## Browser automation — click "Load more" until it runs out

JS-driven pages, infinite scroll, and "Load more" buttons need a real browser.
A `browser` source runs a declarative **interaction recipe** (Playwright default,
Selenium alternate), then feeds the fully-loaded HTML to the same extractors. The
`load_more` action clicks until the list is exhausted — so you harvest *everything*,
not just page one. Pair it with the `chunk` transform to hand an LLM digestible
bites. Full guide: [docs/BROWSER.md](docs/BROWSER.md) + [recipes](docs/recipes/README.md).

```bash
docker compose --profile browser up ujin-jobs-browser   # browsers baked into ujin-browser image
```

```jsonc
// harvest every publication behind a "Load 20 more" button, 25 per LLM call
{ "name": "fels-pubs",
  "source": { "kind": "browser", "config": {
    "url": "https://www.ece.ubc.ca/~ssfels/",
    "actions": [ { "action": "load_more", "button": "button.load-more",
                   "results": ".publication-item", "max_clicks": 200 } ],
    "extract": "links" } },
  "transforms": [ { "kind": "chunk", "config": { "size": 25 } } ],
  "sinks": [ { "kind": "forward", "config": { "url": "http://llm/ingest" } } ] }
```

The scrape service can also pin `render: "browser"` with `actions`, and supports
`page_size`+`cursor` pagination so callers pull a big result set N items at a time.

## CLI

```bash
ujin doctor                  # report installed backends/extras + what each unlocks
ujin init [targets.yaml]     # scaffold a starter targets.yaml (-f to overwrite)
ujin sweep targets.yaml      # poll all targets once; print what changed
ujin serve targets.yaml      # run the poll engine as a daemon
ujin api [targets.yaml]      # poller control service (REST + WS) on :8900
ujin scrape-serve            # rich scrape HTTP service on :8901
ujin jobs-serve [jobs.yaml]  # unified job control plane on :8902
ujin plan validate <path>    # validate a plan file or workflows dir (--json/--strict for CI)
ujin plan render <path>      # resolve and print every expanded job (--json for CI)
ujin mcp-serve               # MCP server for agents (stdio; --http for HTTP)
ujin watch URL --selector …  # watch a page's regions for change
ujin learned site_state.db   # inspect the durable per-host learned state (--json)
ujin obscura-build           # build the bundled headless renderer (needs cargo)
ujin --version               # print the installed version
```

`ujin learned <db>` opens an existing `SiteStore` database read-only and prints,
per host, the learned adaptive state — recommended interval, concurrency factor,
health/cooldown/rate-limited, last observed status/latency, and any observed
`Crawl-delay`. Add `--host HOST` to focus one host, `--strategy-db PATH` to also
show the recommended `(backend, render_mode)`, and `--json` for machine-readable
output. See [docs/ADAPTIVE.md](docs/ADAPTIVE.md#inspecting-learned-state-ujin-learned).

Every command has examples in its `--help`. Config errors are actionable: a
missing/invalid `targets.yaml` names the file (and line), an unknown source kind
lists the valid ones, and a missing required key (e.g. `url`) names it.

## HTTP services

Three FastAPI apps — run any combination. Full reference in
[docs/API.md](docs/API.md) and [docs/JOBS.md](docs/JOBS.md); interactive docs at `/docs` on each.

- **Jobs control plane** (`:8902`): `POST /jobs` (source→transforms→sinks→schedule),
  `GET/DELETE /jobs/{id}`, `/jobs/{id}/run|pause|resume|runs|events`,
  `/jobs/{id}/content` + `/jobs/{id}/results` (hand out obtained data), `WS /jobs/events`,
  `/kinds`, `/metrics`, `POST /plugins/reload`, `POST /plan/reload` (re-read the
  INGEST-PLAN, no restart; `?prune=true` also drops plan-owned jobs gone from the
  file). Durable + plugin-extensible.
  File-driven workflows load from `./workflows` (with `matrix:`/`for_each:` template
  fan-out) — see [docs/WORKFLOWS.md](docs/WORKFLOWS.md).
- **Poller control** (`:8900`): `GET /health /metrics /targets`,
  `GET /content?key=…` (reuse the body ujin last fetched), `POST /targets`,
  `DELETE /targets/{key}`, `POST /sweep`, `WS /ws`.
- **Scrape** (`:8901`): `POST /scrape` (modes `links|article|auto|combined|structured|tables|images|metadata|feeds|contacts`,
  or a `modes` list for multi-extract — several modes over one fetch, results in
  `extracts`; or a `urls` list to scrape many URLs concurrently — one result per
  URL in `batch`), `/scrape:batch`, `/feed`, `/sitemap`, `/discover`, `/capabilities`,
  `/metrics`, plus optional `/social/*` and `/trends/*`.

Set `UJIN_API_KEY` to require `X-API-Key`/Bearer auth on every service
(`/health` stays open).

## MCP — ujin as an agent tool

`ujin mcp-serve` (extra: `ujin[mcp]`) exposes scraping and the job control
plane as MCP tools — `scrape_url`, `discover_site`, `get_capabilities`,
`create_job`, `run_job`, `get_job_results`, … — over stdio for Claude Code /
Claude Desktop or any MCP client: `claude mcp add ujin -- ujin mcp-serve`.
See [docs/MCP.md](docs/MCP.md).

## Docker

```bash
docker compose up --build                   # poller :8900 + scrape :8901 + jobs :8902 (pure-python, fast)
docker compose --profile browser up --build # jobs+scrape on the browser image (:8902 / :8911)
docker compose --profile render up --build  # also build the obscura-enabled service on :8912 (slow)
```
The default `ujin` image is pure-python and builds in seconds (the Rust stage is
skipped). The `ujin-jobs` service mounts `./workflows` (file-driven workflow
definitions), `./plugins` (drop-in custom code), and a named volume for its
durable jobstore. The **`ujin-browser`** image (profile
`browser`) bakes in Playwright + Chromium + Selenium/chromedriver for
interaction-driven scraping (~1.5GB). The `ujin-full` target bakes the obscura
renderer in for JS-heavy / anti-bot pages — its first build compiles V8 (~15–20 min).

```bash
curl localhost:8901/health
curl -X POST localhost:8901/scrape -H 'content-type: application/json' \
  -d '{"url":"https://example.com","mode":"structured"}'
```

## How it works

- **Roles** (`ujin.poll`): `HttpPollable`, `RssPollable`, `ApiPollable`,
  `GraphQLPollable` (POST a GraphQL query; narrows to a dotted `data_path`),
  `CommandPollable`, `CallablePollable`, `SitePollable` (selector-scoped
  change), and `BrowserPollable` (runs a declarative interaction recipe via
  Playwright/Selenium, then feeds the loaded HTML to the extractors —
  see [docs/BROWSER.md](docs/BROWSER.md)). Each returns a `PollResult` with a
  content fingerprint.
- **Adaptive** (`ujin.adapt`): `AdaptiveInterval` grows/shrinks the interval;
  full/equal/decorrelated **jitter**; `TokenBucket` + AIMD smoothing;
  exponential `Backoff` (honors `Retry-After`) and a `CircuitBreaker`.
  `SiteStore`/`HostRecord` persist per-host observations to SQLite; the pure
  `derive_signals(record)` function converts a record into a frozen `PolicySignals`
  struct (recommended interval, health score, cooldown, concurrency factor);
  `SignalAdvisor` is a read-only bridge from store to signals. `StrategyFeedback`
  tracks per-host `(backend, render_mode)` outcome rates for adaptive backend
  selection (opt-in in the scrape service, below). `LearnedRateLimiter` composes all of the above + `ujin.robots`
  `Crawl-delay` into a self-calibrating per-host rate/concurrency governor
  (see [docs/ADAPTIVE.md](docs/ADAPTIVE.md) and [docs/ROBOTS.md](docs/ROBOTS.md)).
  Pass `PollEngine(adaptive=True)` to wire it into the live engine: each host is
  paced by its learned interval and a 429 durably backs it off
  (see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)). Off by default — a no-config
  engine is byte-identical to before.
- **Scrape** (`ujin.scrape`): `ScrapeService` orchestrates fetch + cache +
  extract + the fallback chain; a pluggable `Scorer` ranks links and paces polls
  (`NullScorer` by default, `ujin.trends.BreakingScorer` for news-trading).
  Set `ScrapeConfig(learn_strategy=True, strategy_db="strategy.db")` (env:
  `UJIN_LEARN_STRATEGY` / `UJIN_STRATEGY_DB`) to close the adaptive loop: the
  `auto` backend path biases its first `(backend, render_mode)` toward each host's
  proven-best `StrategyFeedback.recommend()`, skips `is_penalized()` strategies,
  and records every fetch outcome to a durable store (empty `strategy_db` →
  ephemeral `:memory:`). Off by default — a no-config scrape is byte-identical
  to before (see [docs/ADAPTIVE.md](docs/ADAPTIVE.md)).
- **Toolkit**: `ujin.fetch` (HTTP + obscura + altpath), `ujin.extract`
  (article/links/profile/**structured** JSON-LD·OG·microdata/**tables**/**images**/**metadata**/**feeds** `<link rel=alternate>`/**contacts** emails·phones·social links), `ujin.cache`
  (LRU+TTL, SQLite, per-host cooldown), `ujin.sources` (RSS/sitemap/discover +
  `social/`), `ujin.diff` (region diff + webhook sinks), `ujin.session`
  (cookies), `ujin.proxy` (rotation).

The engine takes injectable `clock`/`rng`/`sleep` for deterministic tests.

## Install

```bash
pip install -e .              # core: engine + adapt + callable/command roles (no deps)
pip install -e ".[web]"       # + HTTP/RSS/API roles and the scrape toolkit
pip install -e ".[scrape]"    # + the rich scrape HTTP service
pip install -e ".[mcp]"       # + the MCP server for agents
pip install -e ".[all]"       # everything (web, service, scrape, social, diff, sessions, jobs, browser, mcp)
```
Core is dependency-free; heavier features pull `aiohttp`/`selectolax`/
`trafilatura`/`feedparser`/`fastapi` lazily behind extras. The obscura submodule
is **excluded from the wheel** — `pip install ujin` never triggers a Rust build.

```bash
git submodule update --init --recursive   # fetch the obscura renderer source
ujin obscura-build                         # build it (optional; needs cargo)
```

## Development

```bash
make install-dev   # editable install with everything
make cov           # the CI gate: full offline suite + coverage (fail_under=85)
make bench         # benchmarks vs the committed baseline
```

Docs: [ARCHITECTURE](docs/ARCHITECTURE.md) · [ADAPTIVE](docs/ADAPTIVE.md) ·
[ROBOTS](docs/ROBOTS.md) · [TESTING](docs/TESTING.md) ·
[BACKENDS](docs/BACKENDS.md) (aiohttp vs obscura vs playwright vs selenium) ·
[PERFORMANCE](docs/PERFORMANCE.md) · [CONSUMERS](docs/CONSUMERS.md)
(downstream submodule contracts) · [API](docs/API.md) · [JOBS](docs/JOBS.md) ·
[WORKFLOWS](docs/WORKFLOWS.md) · [INGEST_PLAN](docs/INGEST_PLAN.md) (many jobs from one mounted file) · [PLUGINS](docs/PLUGINS.md) ·
[LIST_TRANSFORMS](docs/LIST_TRANSFORMS.md) (filter/flatten/sort/limit/rename/aggregate/unique/fill/csv) ·
[MARKETPLACE](docs/MARKETPLACE.md) (generic engine; site profiles supplied externally) ·
[BROWSER](docs/BROWSER.md) · [MCP](docs/MCP.md) · [CHANGELOG](CHANGELOG.md)

## Troubleshooting

Run **`ujin doctor`** first — it shows which fetch backends and Python extras are
installed and the exact `pip install` to enable each missing one.

- **`ModuleNotFoundError: aiohttp` (or `fastapi`, `selectolax`, …)** — a feature
  needs an optional extra. `ujin doctor` lists what's missing; install it, e.g.
  `pip install -e ".[web]"` for HTTP/RSS/API roles or `".[scrape]"` for the
  scrape service. Core is dependency-free by design.
- **`ujin: targets file not found`** — pass a real path, or run `ujin init` to
  scaffold one.
- **`ujin: invalid YAML in targets.yaml (line N, column M)`** — a YAML syntax
  error at that location (usually indentation or an unclosed `{`). Each target is
  a single-key mapping: `- http: { url: https://… }`.
- **`unknown source kind 'x'; available: …`** — the kind isn't registered. Use
  one of the listed built-ins, or load your plugin (see docs/PLUGINS.md) and
  reference it as `plugin:<name>`.
- **`missing required config key 'url'`** — that source needs the named key in
  its `config` block.
- **`render: "browser"` / `kind: browser` does nothing** — browser automation is
  off unless `ujin[browser]` is installed and the engine is available
  (`ujin doctor` shows playwright/selenium status). See docs/BROWSER.md.
- **Auth: every service returns 401** — `UJIN_API_KEY` is set, so all endpoints
  except `/health` require `X-API-Key`/`Bearer`. Unset it to run open.

## Lineage

Formerly `scraperv2`, extracted from jennie's scraper-v2. The poller (`poll/`,
`adapt/`, `engine.py`) was built fresh; the scrape service reaches feature/
endpoint parity with scraper-v2 so jennie's irene pipeline can migrate onto ujin.
News-trading scoring (tiering/corroboration/breaking score) stays optional behind
`ujin.trends.BreakingScorer`. Consumed by awork, hct-site, and wordle-max as a
submodule — see [docs/CONSUMERS.md](docs/CONSUMERS.md) before breaking anything.
