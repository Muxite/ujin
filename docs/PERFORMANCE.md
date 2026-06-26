# Performance: measured hot paths and tuning knobs

Measured by the suite in `benchmarks/` (Linux, Python 3.12, local machine —
treat as relative orders of magnitude, not SLAs). Re-measure with
`make bench`; re-record baselines with `make bench-record`.

## Baseline numbers (2026-06)

| Path | Median | Notes |
|---|---|---|
| memory cache put+get | ~1.2 µs | `ScrapeCache` LRU under lock |
| cursor encode+decode | ~1.6 µs | pagination cursors are free |
| `fingerprint_links` (24 links) | ~3.6 µs | |
| `fingerprint` 10 KB body | ~6 µs | sha256-bound |
| `ScrapeService` cache-hit scrape | ~7 µs | full service path, no fetch |
| `fingerprint` JSON payload (500 items) | ~0.23 ms | dominated by canonical JSON encode, not the hash |
| `fingerprint` 1 MB body | ~0.53 ms | |
| disk-cache (SQLite) put (commit) | ~20 µs | WAL + `synchronous=NORMAL` (was ~1.3 ms with rollback journal) |
| disk-cache (SQLite) put+get via `to_thread` | ~0.12 ms | was ~1.45 ms; the commit fsync was the dominant cost |
| HTTP leg, 32 parallel GETs (local origin) | ~4.4 ms | per-host semaphore at 8 |
| `extract_headline_links` (news front page) | ~1.3 ms | the CPU hot path |
| full links-mode scrape (mocked fetch, 60-story page) | ~4.6 ms | HTTP→extract→cache; extraction runs **once** (was 3×) |
| engine sweep, 1 000 no-op targets | ~7.8 ms | scheduler overhead ≈ 8 µs/target |

## What this means

- **Scheduler overhead is negligible.** At ~8 µs/target/sweep, even 10k
  targets cost ~80 ms of pure engine time per pass. Real cost is always the
  fetch.
- **The CPU hot path is link extraction** (~1.3 ms/page). At 100 pages/s
  that's 13% of a core — relevant only for crawl-style bursts; fine for
  polling workloads. The default `mode="links"` scrape now extracts each body
  **exactly once**: `_fetch_html` hands the links it computed for the HTTP
  fast-path decision back to `scrape()`, which reuses them for the
  thin-result/altpath check and the final link-set instead of re-parsing the
  same HTML two more times (a 3×→1× drop on the per-site path — the largest
  single CPU win available, since extraction dominates the scrape's own time).
  `mode="auto"` already extracted once and is unchanged. The
  `scrape_links_extract` benchmark guards this path against re-introducing the
  redundant passes.
- **Fingerprinting JSON is ~40x costlier than hashing bytes** because the
  payload is canonical-JSON-encoded first. For very large API payloads,
  narrow with `json_path` so only the relevant slice is encoded. (Investigated
  cheaper canonicalizations — pre-sorting in Python, streaming `iterencode`,
  C-encoder fast-paths — and **none are both byte-stable and faster**: any
  scheme that touches every node in Python loses to CPython's C encoder doing
  the same traversal, and changing the byte layout would invalidate persisted
  fingerprints. `json_path` narrowing remains the real lever.)
- **The disk cache now commits per put in ~20 µs** (was ~1.3 ms). The
  connection runs in **WAL mode with `synchronous=NORMAL`**, so a commit no
  longer fsyncs the whole database file on every write. This lifts the per-put
  ceiling from ~600 writes/s to ~40k writes/s while preserving the cache's
  durability contract: committed rows survive process death and reopen (only an
  OS/power loss inside the checkpoint window can drop the most recent commits —
  acceptable for a cache, whose runtime source of truth is the memory tier).
  `close()` runs a truncating `wal_checkpoint`, so the on-disk file stays
  self-contained after a clean shutdown. The memory cache is still the right
  default for heavy bursts; per-put disk writes are now cheap enough to be a
  viable durable tier too.
- **A cache hit costs microseconds** — `force_refresh=False` (default) plus
  per-host cooldowns mean repeated agent/MCP calls against the same URL are
  effectively free.

## Tuning knobs

| Knob | Where | Default | Effect |
|---|---|---|---|
| `per_host_concurrency` | `HttpFetcher` / `PER_HOST_CONCURRENCY` | 2 | parallelism against one origin; raise for friendly APIs, never for news sites |
| token bucket `rate`/`burst` | `PollEngine(token_bucket=...)`, targets YAML `rate:`/`burst:` | 10/s | global request smoothing — the main politeness lever |
| `max_concurrency` | `PollEngine` / YAML `concurrency:` | 8 | in-flight polls across all targets |
| `fast_path_min_links` | `ScrapeConfig` | 5 | how thin an HTTP result must be before escalating to obscura |
| `host_cooldown_secs` | `ScrapeConfig` / env | 60 | per-host backoff after 429/5xx (grows 1x→8x) |
| cache `max_entries` / `ttl_secs` | `ScrapeConfig` | 2048 / 120 | memory cache size; raise for wide crawls |
| jitter mode | `engine.add(jitter=...)` | `decorrelated` | spreads poll times; `equal` aligns fleets, `none` is for tests only |
| adaptive `grow`/`shrink` | `engine.add(...)` / job schedule | 1.6 / 0.4 | how fast intervals back off on no-change / tighten on change |

## Multiprocessing (Track 3) gate

Measured by `benchmarks/test_extract_throughput.py` (Linux, Python 3.12, same
machine as the baseline above). Numbers are single-process, CPU-only (fetch
excluded), 500–100 iterations with warmup.

| Extractor | Median | Events/sec | Notes |
|---|---|---|---|
| `extract_structured` (selectolax) | ~0.07 ms | ~14 000/s | JSON-LD + OG + microdata; selectolax is fast |
| `extract_tables` (selectolax) | ~0.28 ms | ~2 900/s | colspan/rowspan expansion |
| `extract_headline_links` (selectolax) | ~1.2 ms | ~815/s | typical links-mode hot path |
| `extract_article` (trafilatura) | ~6.8 ms | ~146/s | trafilatura dominates the full-extraction path |
| **per-poll (all four, fetch excluded)** | **~7.1 ms** | **~140/s** | ceiling for one core running the full extraction stack |

**Go/no-go recommendation — Track 3 is NOT justified for normal polling
workloads and is only justified for sustained crawl bursts.**

- **Threshold**: multiprocessing helps only when the pipeline delivers pages
  faster than the single-process extraction ceiling. That ceiling is **~140
  pages/sec** for full-extraction mode (dominated by trafilatura's ~6.8 ms/page)
  or **~815 pages/sec** for links-only mode.
- **Polling workload** (typical): 1 000 targets at 60 s intervals ≈ 17 pages/sec
  — **14× below** the full-extraction ceiling. Extraction is idle >99% of the
  time. A single process is the bottleneck nowhere; the network and rate-limiter
  always dominate. → **NO-GO.**
- **Crawl workload**: ≥100 parallel HTTP connections × ~0.7 s avg fetch ≈ 140
  pages/sec — right at the full-extraction ceiling. If sustained, one additional
  worker process per ~140 pages/sec of fetch capacity would be needed. → **GO
  only above ~140 pages/sec (full extraction) or ~815 pages/sec (links-only).**

## Plan expansion (ingest-plan, many jobs)

Measured by `benchmarks/test_plan_expansion.py` on the same machine as the
baseline above (Linux, Python 3.12). The `plan_expand_full_120` benchmark
exercises every resolution layer in a single `_load_ingest_plan` call:
top-level `defaults:` deep-merge, `include:` fragment splicing, `matrix:`
fan-out, per-entry `{{ var }}` substitution, and `JobSpec.from_dict`
construction — fanning out to **120 resolved JobSpecs**.

| Benchmark | Median (120 jobs) | Jobs/sec | ms/job | Layers exercised |
|---|---|---|---|---|
| `plan_expand_full_120` | ~15.7 ms | ~7 400 | ~0.13 | defaults + include + matrix + `{{ var }}` |
| `test_expand_matrix_inline_120` | ~14.8 ms | ~8 100 | ~0.12 | defaults + matrix + `{{ var }}` |
| `test_expand_shared_include_120` | ~25.0 ms | ~4 800 | ~0.21 | 120 jobs, shared fragment |

**Per-job expansion cost: ~0.13 ms** (all layers combined).

**Practical large-fan-out guideline:** Plan expansion is a one-shot operation
(startup and explicit reload only — never on the polling hot path). At the
measured ~0.13 ms/job ceiling:

- **100 jobs** → ~13 ms — effectively instant.
- **1 000 jobs** → ~130 ms — comfortably within a startup budget; no special
  concern.
- **5 000 jobs** → ~650 ms — still acceptable for a service startup; consider
  splitting into multiple plan files if reload latency matters.
- **10 000+ jobs** — expansion itself remains sub-second, but YAML parse time
  and I/O for very large files can dominate; benchmark on your hardware if you
  cross this threshold.

The `include:` fragment cache means all 120 jobs sharing one fragment pay the
disk-read cost only once; per-job cost is dominated by `_subst_vars` and
`JobSpec.from_dict`, both of which scale linearly.

## Regression gate

CI runs `pytest benchmarks/` on every push (non-blocking job). Async paths
assert their median stays under **4x** the committed `benchmarks/baseline.json`
— an order-of-magnitude tripwire that survives noisy runners. After an
intentional optimization, re-record with `make bench-record` and commit the
new baseline alongside the change.
