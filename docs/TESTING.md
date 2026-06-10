# Testing

The suite is **fully offline and deterministic**: ~440 tests in ~5 s, no
internet, no real browsers, no obscura binary. Coverage is gated in CI
(`fail_under = 85`, branch coverage on).

```bash
make install-dev      # pip install -e .[all,dev]
make test             # pytest -q
make cov              # pytest --cov --cov-report=term-missing (the CI gate)
make bench            # benchmarks vs benchmarks/baseline.json
make bench-record     # re-record baselines after an intentional perf change
```

## Shared fixtures (tests/conftest.py)

| fixture / helper | what it gives you |
|---|---|
| `fake_origin` | a **real aiohttp server** on localhost with programmable routes: `fake_origin.add("/p", body=..., status=..., delay=..., etag=...)`; records requests, tracks `max_inflight` for concurrency assertions, answers 304 to matching `If-None-Match` |
| `FakePage` / `fake_page` | full in-memory implementation of the `_Page` protocol (`ujin/fetch/browser.py`) — drives every recipe action (click/fill/load_more/…) without a browser; interactions recorded in `.log` |
| `fake_clock` / `FakeClock` | manual time + instant `sleep`; plugs into the engine (`clock=`) and browser `_ActionCtx` |
| `obscura_stub_bin` / `make_obscura_stub(tmp_path, mode)` | an executable Python script standing in for the obscura binary (`mode`: ok / fail / hang), exported as `OBSCURA_BIN` |
| `html_corpus` | saved pages in `tests/fixtures/html/` (news index, article, JS shell, relative-link soup, malformed) shared with the benchmarks |
| `FakeHttp`, `FakeObscura`, `FakeBrowser` | duck-typed service-layer fakes for `ScrapeService` wiring (`from conftest import FakeHttp`) |

FastAPI surfaces are tested with `fastapi.testclient.TestClient` against the
app factories (`create_scrape_app(ScrapeConfig())`, `create_jobs_app(run_engine=False)`,
`create_app(run_engine=False)`); service-layer stubs are swapped onto
`app.state` after startup. MCP tools are tested through an in-memory MCP
client session (`mcp.shared.memory`).

## Markers

| marker | meaning | how to run |
|---|---|---|
| `browser` | needs a real Playwright/Selenium browser | `playwright install chromium`, then `pytest -m browser tests/integration/` |
| `obscura` | needs the real Rust renderer | `ujin obscura-build`, then `pytest -m obscura` |

Both auto-skip when the backend is absent and are **never run in CI** — the
offline FakePage/stub coverage is the gate; the marked tests are the
drift-check you run locally when touching backend wrappers (which are
`# pragma: no cover` for the offline run).

## Conventions

- No test may touch the network: fetches go to `fake_origin`, feeds/sitemaps
  come from `tests/fixtures/feeds/`, social legs are monkeypatched at their
  module bindings (note `_scrape_combined` imports `parse_feed` at call time —
  patch `ujin.sources.rss.parse_feed` *and* `ujin.scrape.service.parse_feed`).
- Time-dependent logic takes an injected clock; if you need `sleep`, you're
  testing it wrong.
- Engine tests at scale must pass a fast `TokenBucket` — the default 10 req/s
  bucket turns a 1k-target sweep into a 100 s test.
- `tests/test_consumer_contracts.py` pins the surfaces awork / hct-site /
  wordle-max consume (see CONSUMERS.md). If one of these fails, you are about
  to break a downstream submodule bump — change the consumer in lockstep or
  back the change out.
- Coverage gate ratchets only upward (60 → 80 → 85). New modules ship with
  their tests in the same commit.
