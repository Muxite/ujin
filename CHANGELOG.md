# Changelog

## [Unreleased]

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
## [Unreleased]

### Added
- **Test coverage for social sources and jobs client** — fixture-driven offline unit tests for `mastodon.py` (47%→100%), `twitter.py` (44%→100%), `jobs/client.py` (67%→100%), and `sitemap.py` (79%→100%); total suite coverage rises to 88.9% (floor 85%).
- **Coverage gap-fill** — offline tests for `poll/__init__` lazy imports, `_nitter.nitter_posts` (success/failure/cooldown paths), and `_syndication.syndication_posts` (JSON/HTML/error paths); closes the 87%→88% gap flagged in prior review.

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
