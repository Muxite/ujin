# Changelog

## [Unreleased]

### Added
- **test(poll-coverage)**: Added 10 offline unit tests covering previously-uncovered error/edge branches in the live poll subsystem — empty-argv `ValueError` (command.py:25), `asyncio.TimeoutError` timeout path (command.py:42-44), generic subprocess exception (command.py:47-48), feedparser `ImportError` (rss.py:23-24), `parse_feed` exception (rss.py:29-30), `decide_changed(None, ...)` short-circuit (base.py:79), bytes/bytearray fingerprint branch (base.py:26), `aiohttp` `ImportError` (api.py:53-54), request-level network exception (api.py:72-73), and `render=True` ObscuraFetcher path (site.py:54-58); all five targeted files reach 100 % line coverage and total suite coverage rises from 95.43 % to 95.50 %.

## 0.11.0 — 2026-06-22

### Added
- **Opt-in strategy-feedback loop in the scrape service** — `ScrapeConfig(learn_strategy=True, strategy_db=...)` constructs a durable `ujin.adapt.StrategyFeedback` (built/closed by `build_scrape_components`; empty `strategy_db` → ephemeral `:memory:`). When on, the `auto` backend path biases the first `(backend, render_mode)` it tries toward the host's proven-best `recommend()`, skips a recommendation flagged by `is_penalized()` (via an optional injected `SiteStore`), and records every fetch outcome with `record(host, backend, render_mode, ok, latency)` so the loop closes. Strictly additive and off by default — a no-config scrape is byte-identical to before.
- **`PollEngine(respect_robots=True)`** — when `adaptive=True` is also set, automatically builds a `RobotsCache` (injectable `robots_fetcher`, configurable `robots_ttl`, 1 h default) and wires it into the engine's `robots=` hook on `LearnedRateLimiter`: `Crawl-delay` becomes a hard floor on the learned per-host interval, and any URL whose path is disallowed is silently skipped — counted as a poll but not a failure so backoff and penalty logic are unaffected. Off by default; the pre-existing engine/poll path is byte-identical when the flag is unset.
- **test(cli)**: Added 9 tests covering previously-uncovered `ujin/cli.py` paths — `_version()` exception/metadata-fallback/`"unknown"` branches (lines 48-59), the YAML-error-without-`problem_mark` branch (line 99), `_cmd_obscura_build` success and missing-Cargo.toml paths (lines 200-216), and `_cmd_watch` callback/webhook/selector+render paths (lines 299-319); `cli.py` coverage rises from 82 % to 99 % and total suite coverage from ~95 % to 95.43 %.

## 0.10.0 — 2026-06-22

### Added
- **Opt-in adaptive poll engine** — `PollEngine(adaptive=True)` wires the
  already-shipped Track-1 governor into the live loop: the engine constructs a
  per-process `SiteStore` + `LearnedRateLimiter`, paces each poll through the
  async `acquire(host)` gate, floors every target's next interval by
  `interval_for(host)`, and persists each response via `observe(...)` so a 429
  durably backs the host off and a restarted process resumes calibrated. The
  store path (`site_store_path`, default in-process `:memory:`),
  `adaptive_base_interval`, and an optional `robots` adapter are configurable, and
  the limiter shares the engine's injectable `clock`/`sleep`. Strictly additive
  and off by default — with the flag unset no `SiteStore`/limiter is built, no
  extra I/O happens, and the poll path is byte-identical to before.
- **scrape multi-URL batch**: `POST /scrape` now accepts an optional `urls` list so one request scrapes several URLs and returns one result per URL under a new additive `batch` list (in request order); the top-level fields mirror the first URL's result. The URLs are fetched concurrently with a bounded concurrency cap — `ScrapeService.scrape_urls()` fans out the per-URL `scrape()` calls under an `asyncio.Semaphore` (`batch_max_concurrency`, env `BATCH_MAX_CONCURRENCY`, default 8) and isolates per-URL failures as `kind='error'` entries so one failing URL never sinks the batch. The batch form is single-`mode` (the `modes` multi-extract map and `page_size`/`cursor` pagination are not applied per URL) and is bounded by `batch_max_items` (default 64). Omitting `urls` keeps the classic single-`url` behaviour byte-for-byte unchanged.
- **docs/ADAPTIVE.md** — end-to-end user guide for the durable adaptive-learning
  subsystem (SiteStore/HostRecord, derive_signals/PolicySignals/SignalAdvisor,
  StrategyFeedback/StrategyOutcome, LearnedRateLimiter, ujin.robots); surfaces all
  adaptive symbols in the README.md feature list.

## 0.9.0 — 2026-06-22

### Added
- **Learned rate governor** (`ujin/adapt/rate.py`, pure stdlib) —
  `LearnedRateLimiter(store, robots=None, *, base_interval=0.0, clock=..., sleep=...,
  max_concurrency=8)` composes `derive_signals(record)` output
  (`recommended_interval`, `concurrency_factor`, `rate_limited`, `cooldown_secs`) via
  the `SignalAdvisor` bridge with an optional robots `Crawl-delay`, backed by the
  existing `AdaptiveInterval` / `AIMDLimiter` / `TokenBucket` primitives.
  `interval_for(host)` / `concurrency_for(host)` report the effective cadence and
  concurrency (the interval never below `max(observed Crawl-delay,
  robots.crawl_delay(host))`); the async `acquire(host)` gate (also `async with`)
  paces a per-host token bucket and caps in-flight requests; `observe(host,
  status=..., latency=..., error=...)` feeds each response back into the store and
  the in-process controllers so a 429 raises the interval and throttles concurrency
  while clean responses relax both toward baseline. Persisted state warm-starts the
  controllers on restart. Exported additively from `ujin.adapt`; opt-in and wired
  into nothing by default — the scrape/poll path is unchanged unless the limiter is
  explicitly used. `crawl_delay` and the host-policy signals now drive this governor.
- **scrape multi-extract**: `POST /scrape` now accepts an optional `modes` list (`links`/`article`/`auto`/`structured`/`html`) so one request fetches the page once and returns a result per mode under a new additive `extracts` map (keyed by mode); the top-level fields mirror the first listed mode. Backed by `ScrapeService.scrape_multi()`, which runs each mode over the already-fetched body and isolates per-mode failures as `kind='error'` entries so one failing mode never sinks the others. The `html` mode returns the raw fetched HTML in the new `html` response field. Omitting `modes` keeps the classic single-`mode` behaviour byte-for-byte unchanged.

## 0.7.0 — 2026-06-22

- **tests**: Added `tests/test_cov_trends_mcp.py` (35 offline tests) raising per-file coverage of `ujin/trends/corroboration.py`, `ujin/trends/scorer.py`, `ujin/mcp/server.py`, `ujin/service.py`, and `ujin/sources/social/x.py` to ≥99% each, lifting TOTAL coverage from ~89% to 90.7%.

## 0.6.0

Additive only — no public symbol, CLI subcommand, flag, env var, response field,
or Docker target was renamed or removed, so the three consumer-contract surfaces
(awork / hct-site / wordle-max) stay frozen and green.

### Added
- **`SiteStore` / `HostRecord`** (`ujin/adapt/site_store.py`, pure stdlib) — a
  durable per-host observed-state store on SQLite. Persists last status,
  p50/last latency, error count, 429 count, observed `Crawl-delay`, the adaptive
  interval, and `last_seen` so a fresh process resumes calibrated, polite
  polling. `SiteStore(path=':memory:', clock=time.time)`; `get(host)` returns a
  zero-valued `HostRecord` for unknown hosts; `record(host, **signals)` is an
  atomic serialized upsert (counters accumulate, gauges overwrite, `last_seen`
  stamped from the injectable clock); `close()` runs a truncating
  `wal_checkpoint`. Reuses the disk cache's WAL-mode / `synchronous=NORMAL`
  durability pattern. Both names are exported from `ujin.adapt`. This is the
  foundation other Track-1 adaptive units consume.
## 0.6.0 — 2026-06-22

### Added
- **`ujin.robots`**: `RobotsPolicy` parses robots.txt into per-User-agent groups with Allow/Disallow longest-match precedence, `*` and `$` wildcard handling, `Crawl-delay` extraction, and `Sitemap:` directive collection; malformed/empty/missing file → allow-all. `RobotsPolicy.is_allowed(path, agent='*') -> bool` and `RobotsPolicy.crawl_delay(agent='*') -> float | None` are pure methods over already-parsed text. `RobotsCache(ttl, fetcher, clock)` adds a TTL fetch+cache layer with injectable fetcher and clock for deterministic tests; opt-in only — default scrape/poll behavior is unchanged unless `RobotsCache` is explicitly used. `crawl_delay()` values are a future input to the learned-rate-limit system (`ujin.adapt.concurrency`).
## 0.8.0 — 2026-06-22

### Added
- **`StrategyFeedback` / `StrategyOutcome`** (`ujin/adapt/strategy.py`, pure stdlib) — a durable per-host, per-strategy outcome store on SQLite. A *strategy* is a `(backend, render_mode)` pair. `StrategyFeedback(store=':memory:', clock=time.time)`: `record(host, strategy, *, ok, latency)` is an atomic serialized upsert (counters accumulate, latency gauges overwrite, `last_seen` stamped from the injectable clock); `recommend(host)` returns the highest-success-rate known strategy deterministically (ties broken by attempts then lexicographic order), or `None` for an unseen host; `is_penalized(host, strategy, record)` is a pure no-I/O helper that returns `True` when `derive_signals(record).rate_limited` or `health` is low; `close()` runs a truncating `wal_checkpoint`. Reuses the WAL-mode / `synchronous=NORMAL` durability pattern from `SiteStore`. Both names exported additively from `ujin.adapt`; opt-in only — not wired into any default scrape/poll path. Input layer for future learned strategy-selection.
- **tests**: Added `tests/test_jobs_coverage.py` (38 offline tests) raising per-file coverage of `ujin/jobs/app.py` to 99%, `ujin/jobs/pipeline.py` to 100%, `ujin/jobs/cron.py` to 100%, and `ujin/jobs/transforms.py` to 98%, lifting TOTAL coverage from ~91% to 95%.
- **Host policy signals** (`ujin/adapt/signals.py`, pure stdlib) — a deterministic
  interpretation layer over `SiteStore`/`HostRecord`. `derive_signals(record, *,
  base_interval=0.0, robots_crawl_delay=None)` returns a frozen `PolicySignals`
  (`recommended_interval`, `cooldown_secs`, `should_cooldown`, `rate_limited`,
  `concurrency_factor`, `health` in 0..1) doing no I/O: a 429 (counter or last
  status) sets `rate_limited`, raises the interval and throttles concurrency;
  `recommended_interval` is never below `max(crawl_delay, robots_crawl_delay)`;
  rising `error_count` lowers `health` and raises `cooldown_secs`; a clean record
  is pristine (`health==1.0`, no cooldown, full concurrency, interval ==
  `base_interval`). `SignalAdvisor(store)` is a read-only bridge whose
  `for_host(host)` reads `store.get(host)` and derives signals without mutating it.
  Exported additively from `ujin.adapt`; opt-in and wired into nothing by default —
  it is the input layer the planned strategy-feedback and learned-rate-limit units
  consume.
- **Test coverage for social sources and jobs client** — fixture-driven offline unit tests for `mastodon.py` (47%→100%), `twitter.py` (44%→100%), `jobs/client.py` (67%→100%), and `sitemap.py` (79%→100%); total suite coverage rises to 88.9% (floor 85%).
- **Coverage gap-fill** — offline tests for `poll/__init__` lazy imports, `_nitter.nitter_posts` (success/failure/cooldown paths), and `_syndication.syndication_posts` (JSON/HTML/error paths); closes the 87%→88% gap flagged in prior review.
- **Scrape subsystem coverage** (`tests/test_cov_scrape.py`) — 49 offline fixture-driven tests raising `app.py` 67%→97%, `build.py` 75%→100%, `config.py` 76%→100%, `host_overrides.py` 78%→97%, `service.py` 83%→97%; total suite coverage rises to 91.4% (floor 89%).

## 0.5.0 — 2026-06-17

Feature + performance + developer-experience cycle. Every change is **additive** —
no public symbol, CLI subcommand, flag, env var, response field, or Docker target
was renamed or removed, so the three consumer-contract surfaces
(awork / hct-site / wordle-max) stay frozen and green.

### Added
- **List-reshaping transforms** (`ujin/jobs/transforms.py`, pure stdlib):
  - `flatten` — fan a list payload into one event per item (inverse of `chunk`),
    with an optional `index` field; non-list payloads pass through.
  - `sort` — sort a list payload by a dotted `key` (or natural order), `reverse`
    optional; missing/uncomparable values sort last without raising.
  - `limit` — cap a list payload to the first/last N items (`count`, `from`).
  - `rename` — remap dict keys (`mapping`) across a dict or list-of-dicts;
    `drop_missing` materializes absent keys as null.
- **`csv` sink** (`ujin/jobs/sinks.py`, pure stdlib) — append event rows to a
  CSV/TSV file with auto header on create, explicit-or-inferred (and then
  locked) `columns`, configurable `delimiter` / `path_in_event`; non-dict items
  are skipped and a no-row event is a silent no-op.
- All five kinds are additive, registered as built-ins, discoverable at
  `GET /kinds`, and documented in docs/LIST_TRANSFORMS.md.
- **`ujin doctor`** — reports which fetch backends (http/obscura/playwright/
  selenium) and optional Python extras are installed, what each unlocks, and the
  exact `pip install` to enable a missing one. Reuses
  `ujin/fetch/capabilities.py`.
- **`ujin init [targets.yaml]`** — scaffolds a commented, ready-to-run starter
  `targets.yaml` (HTTP page, RSS feed, JSON API, shell command). `-f/--force`
  overwrites; refuses to clobber otherwise.
- **`ujin --version`** — prints the installed version.
- Usage examples in `--help` for every subcommand (epilogs), clearer top-level
  help, default-value hints on flags, and `metavar`s.
- README: a 60-second quickstart and a Troubleshooting section mapping each
  common error to its fix; `ujin doctor` referenced from docs/BACKENDS.md.
- CLI tests for `doctor`/`init`/`--version` and every actionable error path.

### Changed
- **Disk cache (SQLite) runs in WAL mode with `synchronous=NORMAL`.** Per-put
  commits no longer fsync the whole database file, lifting the per-put commit
  cost from ~1.3 ms to ~20 µs (~49x) and the put+get-via-`to_thread` roundtrip
  from ~1.45 ms to ~0.12 ms — raising the durable-write ceiling from ~600 to
  ~40k writes/s. The public `DiskCache` API and its durability contract are
  unchanged: committed rows survive process death and reopen (new tests
  `test_disk_durable_across_reopen_without_clean_close`,
  `test_disk_close_checkpoints_wal`). `close()` now runs a truncating
  `wal_checkpoint` so the on-disk file stays self-contained after shutdown.
  New benchmark `test_disk_cache_put` isolates the commit path; the
  `disk_cache_roundtrip` async baseline was re-recorded.
- **Actionable CLI errors** (no tracebacks; clean `ujin: …` messages):
  - missing targets file → names the path + suggests `ujin init`;
  - invalid YAML → names the file **and line/column**;
  - non-mapping document or target entry → explains the expected shape;
  - unknown source kind → lists the valid kinds;
  - missing required config key (e.g. `url`) → names the key.

## 0.4.0 — 2026-06-10

Hardening release: the test/coverage/benchmark infrastructure, API
normalization, the MCP server, and the backend capability matrix.

### Fixed
- **Builtin transforms built through the registry crashed** with
  `'BuildContext' object is not callable` — every workflow/job using
  `select`/`dedupe`/etc. through `JobManager` was broken. (The old tests
  called `jobs.transforms.build_transform` directly and missed it.)
- `render="http"` on a thin page now returns the thin link-set instead of
  discarding the body and failing.
- `.coverage` accidentally tracked in git; now ignored.

### Added
- **MCP server** (`ujin[mcp]` extra): `ujin mcp-serve` exposes
  scrape/jobs as agent tools over stdio or streamable HTTP — `scrape_url`,
  `scrape_feed`, `discover_site`, `get_capabilities`, `get_metrics`, and the
  job lifecycle (`list/get/create/run/pause/resume/get_job_results`).
  See docs/MCP.md.
- **Backend capability matrix** (`ujin/fetch/capabilities.py`) +
  `GET :8901/capabilities` with live availability for
  http/obscura/playwright/selenium. Human version: docs/BACKENDS.md.
- **Benchmark harness** (`benchmarks/`): pytest-benchmark sync paths + a
  custom async runner gated at 4x the committed `baseline.json`
  (`make bench`, `make bench-record`). Findings: docs/PERFORMANCE.md.
- **CI** (GitHub Actions): 3.11/3.12 matrix, offline suite with coverage
  gate (`fail_under=85`, branch coverage), separate benchmark job. No
  obscura build, no browser downloads.
- ~250 new tests (now ~440, fully offline, <10 s) including consumer-contract
  tripwires for awork / hct-site / wordle-max (docs/CONSUMERS.md) and shared
  fixtures (fake aiohttp origin, full-protocol FakePage, obscura stub binary,
  HTML corpus) — docs/TESTING.md.
- New docs: ARCHITECTURE, TESTING, CONSUMERS, BACKENDS, PERFORMANCE, MCP.

### Changed (breaking where noted)
- Health responses normalized to `{ok, status, service, ...}` on all three
  services (additive everywhere; `:8901` keeps `status`).
- **Breaking:** `:8900 GET /stats` renamed to `GET /metrics` (no known
  consumer; the poller control surface had none).
- `UJIN_API_KEY` now guards **all** services (was :8902 only). Opt-in via
  env; `/health` stays open. `ApiKeyMiddleware` moved to `ujin.auth`
  (`ujin.jobs.auth` remains as a shim).
- Stable import surfaces declared: `ujin.scrape` exports `ScrapeService`,
  `ScrapeResult`, `build_scrape_service`; `ujin.jobs` exports `JobManager`;
  everything else is internal.

## 0.3.0

File-driven workflows (setup → collect → serve), plugin system, obscura
submodule, scrape/jobs services, containerization. (Pre-changelog.)
