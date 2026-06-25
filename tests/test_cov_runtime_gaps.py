"""Close coverage gaps in fetch/runtime/adaptive/diff/cache modules.

All tests are offline and deterministic (no network, fake clocks injected).
"""
from __future__ import annotations

import asyncio
import random
from unittest.mock import AsyncMock, patch

import pytest

from tests.conftest import FakeClock, FakePage
from ujin.adapt.concurrency import TokenBucket
from ujin.engine import PollEngine
from ujin.poll.base import PollResult


# ── helpers ───────────────────────────────────────────────────────────────── #

def _engine(clock: FakeClock, **kw) -> PollEngine:
    return PollEngine(
        token_bucket=TokenBucket(rate=1e9, burst=1e9, clock=clock),
        clock=clock, sleep=clock.sleep, rng=random.Random(42), **kw,
    )


# ── adapt/backoff — line 46 ───────────────────────────────────────────────── #

def test_backoff_reset_clears_failures():
    """Backoff.reset() zeroes the failure counter — covers line 46."""
    from ujin.adapt.backoff import Backoff
    b = Backoff()
    b.on_failure()
    b.on_failure()
    assert b.failures == 2
    b.reset()
    assert b.failures == 0


# ── adapt/concurrency — line 31 ───────────────────────────────────────────── #

def test_token_bucket_rejects_non_positive_rate():
    """TokenBucket with rate ≤ 0 raises ValueError — covers line 31."""
    with pytest.raises(ValueError, match="rate must be > 0"):
        TokenBucket(rate=0.0, burst=1.0)


def test_token_bucket_rejects_negative_rate():
    """Negative rate also hits the same guard."""
    with pytest.raises(ValueError, match="rate must be > 0"):
        TokenBucket(rate=-3.0, burst=1.0)


# ── adapt/rate — 224->226, 243->249, 279->281 ─────────────────────────────── #

def _make_limiter(base_interval: float = 0.0):
    from ujin.adapt.rate import LearnedRateLimiter
    from ujin.adapt.site_store import SiteStore
    clock = FakeClock()
    store = SiteStore(path=":memory:", clock=clock)
    lim = LearnedRateLimiter(
        store, clock=clock, sleep=clock.sleep, base_interval=base_interval,
    )
    return lim, clock


def test_rate_observe_without_status_skips_status_branch():
    """observe() with status=None — covers branch 224->226 (if status is not None: False)."""
    lim, _ = _make_limiter()
    # status defaults to None; covers the False branch of "if status is not None:"
    lim.observe("host.example")


def test_rate_observe_4xx_non_error_skips_all_controller_branches():
    """status=400 → is_429=False, is_error=False, is_clean=False — covers 243->249."""
    lim, _ = _make_limiter()
    # 400 < 500 so not is_error; 400 >= 400 so not is_clean; not 429 so not is_429
    record = lim.observe("host.example", status=400)
    assert record is not None


async def test_rate_gate_release_when_inflight_is_zero():
    """_gate_release with inflight=0 — covers 279->281 (if gov.inflight > 0: False)."""
    lim, _ = _make_limiter()
    gov = lim._gov("zero.example")
    assert gov.inflight == 0
    # calling release when nothing was acquired: inflight stays 0, notify still fires
    await lim._gate_release("zero.example")
    assert gov.inflight == 0


# ── diff/events — line 74 ─────────────────────────────────────────────────── #

async def test_webhook_sink_logs_warning_on_error_status():
    """WebhookSink logs warning on HTTP ≥ 400 response — covers line 74."""
    from ujin.diff.events import WebhookSink

    class _FakeResult:
        payload = {}
        fingerprint = "fp-abc"
        ts = 0.0

    class _FakeResp:
        status = 422
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    class _FakeSession:
        def post(self, *a, **kw): return _FakeResp()
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    with patch("aiohttp.ClientSession", return_value=_FakeSession()):
        sink = WebhookSink("http://fake.invalid/hook")
        # Must log a warning but must NOT raise
        await sink("the-key", _FakeResult())


# ── diff/region — line 30 ─────────────────────────────────────────────────── #

def test_extract_regions_empty_selector_list_returns_early():
    """Empty selectors → {} — covers line 30."""
    from ujin.diff.region import extract_regions
    assert extract_regions("<html><body>hi</body></html>", []) == {}


# ── cache/disk — lines 183-184 ────────────────────────────────────────────── #

def test_disk_cache_flush_from_skips_unpicklable_payload(tmp_path):
    """flush_from silently drops a payload that can't be pickled — covers 183-184."""
    from ujin.cache.disk import DiskCache
    from ujin.cache.store import CachedEntry

    cache = DiskCache(tmp_path / "gap.db")
    bad = CachedEntry(
        url="http://example.com",
        fingerprint="fp",
        payload=lambda: None,   # lambdas cannot be pickled
        fetched_at=0.0,
    )
    cache.flush_from([("bad-key", bad)])
    assert not cache.contains("bad-key")
    cache.close()


# ── engine — lines 84-85, 218-219, 251-252, 302-303, 315->exit ─────────────── #

def test_robots_adaptor_is_allowed_before_policy_fetched():
    """is_allowed when no policy cached yet → returns True — covers lines 84-85."""
    from ujin.engine import _RobotsAdaptor
    from ujin.robots import RobotsCache
    adaptor = _RobotsAdaptor(RobotsCache())
    # _policies dict is empty; policy is None → returns True
    assert adaptor.is_allowed("example.com", "/foo") is True
    assert adaptor.is_allowed("other.com", "/") is True


def test_origin_for_url_with_scheme_but_no_hostname():
    """URL with '://' but missing hostname falls through to _host_for — covers lines 218-219."""
    class _P:
        key = "svc"
        url = "http://"   # scheme present, hostname is empty string / None

    result = PollEngine._origin_for(_P())
    assert isinstance(result, str)


def test_origin_for_key_without_scheme():
    """Key with no '://' — covers branch 214->218 (if '://' in raw: False)."""
    class _P:
        key = "simple-label"
        # No url attribute; key has no "://"

    result = PollEngine._origin_for(_P())
    assert result == "https://simple-label"


async def test_poll_once_pollable_exception_gives_failure_result(fake_clock):
    """poll() raises → PollResult.failure returned, not propagated — covers 251-252."""
    class _BoomPollable:
        key = "raiser"
        async def poll(self, prev): raise RuntimeError("network exploded")

    eng = _engine(fake_clock)
    target = eng.add(_BoomPollable())
    result = await eng.poll_once(target)
    assert not result.ok
    assert "RuntimeError" in (result.error or "")


async def test_fire_on_change_exception_does_not_propagate(fake_clock):
    """on_change raises → exception logged, not re-raised — covers 302-303."""
    class _SimplePollable:
        key = "simple"
        async def poll(self, prev): return PollResult(ok=True, changed=True)

    def _bad_cb(key, result):
        raise ValueError("callback boom")

    eng = _engine(fake_clock)
    target = eng.add(_SimplePollable(), on_change=_bad_cb)
    # _fire must swallow the exception
    await eng._fire(target, PollResult(ok=True, changed=True))


async def test_engine_run_exits_immediately_when_stop_already_set(fake_clock):
    """stop event pre-set → while loop exits on first check — covers 315->exit."""
    eng = _engine(fake_clock)
    stop = asyncio.Event()
    stop.set()
    await eng.run(stop=stop)   # should return without blocking


# ── fetch/browser — 263-264, 281->261, 354-358, 383-384, 416->exit ──────────── #

async def test_grow_loop_timeout_stops_iteration(fake_clock):
    """Clock past deadline on first iteration — covers lines 263-264."""
    from ujin.fetch.browser import _ActionCtx, _grow_loop

    page = FakePage(
        selectors={"btn": 1},
        present={"btn": True},
        enabled={"btn": True},
    )
    ctx = _ActionCtx(sleep=fake_clock.sleep, clock=fake_clock)
    # timeout_ms=0 → deadline = clock() + 0 = clock(); first loop check: clock() >= clock() → True
    result = await _grow_loop(
        ctx, page,
        button="btn", results=".item",
        max_iters=50, timeout_ms=0, settle_ms=100,
    )
    assert result["stopped"] == "timeout"


async def test_grow_loop_scroll_without_results_selector(fake_clock):
    """scroll_to_bottom with results=None — covers branch 281->261."""
    from ujin.fetch.browser import _ActionCtx, _grow_loop

    page = FakePage()
    ctx = _ActionCtx(sleep=fake_clock.sleep, clock=fake_clock)
    result = await _grow_loop(
        ctx, page,
        button=None, results=None,
        max_iters=2, timeout_ms=9999, settle_ms=0,
    )
    assert result["iterations"] == 2
    # verify scroll_to_bottom was called (the else branch)
    assert any(e[0] == "scroll_to_bottom" for e in page.log)


async def test_browser_fetcher_close_no_backend_is_noop():
    """close() when _backend is None — covers 416->exit."""
    from ujin.fetch.browser import BrowserFetcher
    bf = BrowserFetcher()
    assert bf._backend is None
    await bf.close()
    assert bf._backend is None


class _ClosingPage(FakePage):
    """FakePage whose close() raises to exercise the except-pass handler."""
    async def close(self) -> None:
        raise RuntimeError("page.close() exploded")


class _MinimalBackend:
    async def start(self) -> None: pass
    async def close(self) -> None: pass
    async def new_page(self) -> _ClosingPage: return _ClosingPage()


async def test_render_close_exception_is_suppressed(fake_clock):
    """page.close() raises in finally block — covers lines 383-384."""
    from ujin.fetch.browser import BrowserFetcher, BrowserResult
    bf = BrowserFetcher(clock=fake_clock, sleep=fake_clock.sleep)
    bf._backend = _MinimalBackend()
    result = await bf.render("http://example.com")
    assert isinstance(result, BrowserResult)


async def test_ensure_backend_playwright_branch(fake_clock, monkeypatch):
    """_ensure_backend with playwright mocked — covers lines 356-358."""
    import ujin.fetch.browser as bmod

    class _FakePW:
        async def start(self) -> None: pass
        async def close(self) -> None: pass
        async def new_page(self) -> FakePage: return FakePage()

    monkeypatch.setattr(bmod, "browser_available", lambda _e: True)
    monkeypatch.setattr(bmod, "_PlaywrightBackend", lambda _f: _FakePW())

    bf = bmod.BrowserFetcher(engine="playwright", clock=fake_clock, sleep=fake_clock.sleep)
    backend = await bf._ensure_backend()
    assert backend is not None


async def test_ensure_backend_selenium_branch(fake_clock, monkeypatch):
    """_ensure_backend with selenium mocked — covers lines 354-355."""
    import ujin.fetch.browser as bmod

    class _FakeSel:
        async def start(self) -> None: pass
        async def close(self) -> None: pass
        async def new_page(self) -> FakePage: return FakePage()

    monkeypatch.setattr(bmod, "browser_available", lambda _e: True)
    monkeypatch.setattr(bmod, "_SeleniumBackend", lambda _f: _FakeSel())

    bf = bmod.BrowserFetcher(engine="selenium", clock=fake_clock, sleep=fake_clock.sleep)
    backend = await bf._ensure_backend()
    assert backend is not None


# ── fetch/http — 78->exit ─────────────────────────────────────────────────── #

async def test_http_fetcher_start_twice_is_idempotent():
    """Second call to start() when session is already open — covers 78->exit."""
    from ujin.fetch.http import HttpFetcher
    f = HttpFetcher()
    await f.start()
    session_before = f._session
    await f.start()   # session already open → if self._session is None: False
    assert f._session is session_before
    await f.close()


# ── fetch/altpath — 100->73, 131 ─────────────────────────────────────────── #

async def test_sitemap_articles_all_non_http_skips_seen():
    """Sitemap articles with ftp:// URLs → normalize_url returns None → seen stays empty
    → if seen: False → loop continues to next path — covers branch 100->73."""
    from ujin.fetch.altpath import try_sitemap_news
    from ujin.fetch.http import HttpResponse

    # First path returns a sitemap with only ftp:// article URLs (non-http, filtered by normalize_url)
    SITEMAP_FTP = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>ftp://files.example.com/article-1</loc></url>
  <url><loc>ftp://files.example.com/article-2</loc></url>
</urlset>"""

    class _Http:
        def __init__(self): self.calls = []
        async def get(self, url, **_):
            self.calls.append(url)
            if url.endswith("/news-sitemap.xml"):
                return HttpResponse(url=url, status=200, body=SITEMAP_FTP, final_url=url)
            return HttpResponse(url=url, status=404, body="", final_url=url)

    http = _Http()
    result = await try_sitemap_news(http, "https://example.com/home")
    # All ftp:// articles normalize to None → seen empty → loop exhausts all paths → None
    assert result is None
    # First path was tried
    assert any("news-sitemap.xml" in c for c in http.calls)


async def test_rss_fallback_all_items_empty_url_returns_none():
    """RSS items with empty URLs all filtered → links stays empty → return None — covers line 131."""
    from ujin.fetch.altpath import try_rss_fallback

    class _Item:
        url = ""
        title = "stub"

    async def _parse(url):
        return [_Item(), _Item(), _Item()]

    result = await try_rss_fallback("http://example.com/feed.rss", _parse)
    # normalize_url("") → None, then None or "" → "" which is falsy → all filtered
    assert result is None
