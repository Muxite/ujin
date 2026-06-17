# Changelog

## [Unreleased]

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
