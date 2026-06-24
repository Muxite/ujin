"""Coverage gap-fill: proxy pool, scrape metrics, route edge-paths, rss, session store.

Targets:
  ujin/proxy/pool.py       lines 45, 49->exit, 59->exit
  ujin/scrape/metrics.py   72->74, 101, 105-106
  ujin/scrape/routes.py    55, 159-164, 231, 346-347
  ujin/sources/rss.py      30
  ujin/session/store.py    42-43
"""
from __future__ import annotations

import pytest

# ── ujin/proxy/pool.py ───────────────────────────────────────────────────────


from ujin.proxy.pool import ProxyPool  # noqa: E402


def test_proxy_pool_acquire_returns_none_when_all_benched():
    """Line 45: for loop exhausts all proxies (all benched) → None returned."""
    pool = ProxyPool(["http://a", "http://b"], cooldown_secs=3600)
    pool.record_failure("http://a")
    pool.record_failure("http://b")
    assert pool.acquire() is None


def test_proxy_pool_record_success_unknown_url_is_noop():
    """Line 49->exit: for loop in record_success exits without finding the URL."""
    pool = ProxyPool(["http://a"])
    pool.record_success("http://unknown")   # must not raise
    assert pool.healthy() == ["http://a"]   # original proxy untouched


def test_proxy_pool_record_failure_unknown_url_is_noop():
    """Line 59->exit: for loop in record_failure exits without finding the URL."""
    pool = ProxyPool(["http://a"])
    pool.record_failure("http://unknown")   # must not raise
    assert pool.healthy() == ["http://a"]   # original proxy untouched


# ── ujin/scrape/metrics.py ───────────────────────────────────────────────────


from ujin.scrape.metrics import HostMetrics, _percentile  # noqa: E402


def test_metrics_negative_latency_not_appended():
    """Branch 72->74: negative latency_ms skips the deque append."""
    m = HostMetrics()
    m.record("http://x.test/", success=True, latency_ms=-1.0)
    snap = m.snapshot()
    assert snap["hosts"]["x.test"]["samples"] == 0
    assert snap["hosts"]["x.test"]["latency_ms_p50"] is None


def test_percentile_empty_returns_none():
    """Line 101: empty sample list → None."""
    assert _percentile([], 50) is None
    assert _percentile([], 95) is None


def test_percentile_multi_sample():
    """Lines 105-106: nearest-rank calculation over more than one sample."""
    assert _percentile([1.0, 2.0, 3.0], 50) == pytest.approx(2.0)
    assert _percentile([10.0, 20.0, 30.0, 40.0], 75) == pytest.approx(30.0)


# ── ujin/scrape/routes.py ────────────────────────────────────────────────────

fastapi = pytest.importorskip("fastapi")
from fastapi import HTTPException as _HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import ujin.scrape.routes as routes_mod  # noqa: E402
from ujin.scrape.app import create_scrape_app  # noqa: E402
from ujin.scrape.config import ScrapeConfig  # noqa: E402
from ujin.scrape.service import ScrapeResult  # noqa: E402


@pytest.fixture
def route_client():
    app = create_scrape_app(ScrapeConfig())
    c = TestClient(app)
    c.__enter__()
    yield c, app
    c.__exit__(None, None, None)


def test_result_to_response_with_article(route_client):
    """Line 55: _result_to_response builds ArticlePayload when article is set."""
    c, app = route_client

    class _Article:
        url = "https://n.test/story"
        title = "Big Story"
        text = "Body text here."
        byline = "Jane Doe"
        published = "2026-06-24"
        language = "en"
        top_image = None

    class _SvcWithArticle:
        async def scrape(self, url, **kw):
            return ScrapeResult(
                url=url, kind="article", fingerprint="fp", fetched_at=1.0,
                cached=False, age_secs=0.0, used_renderer=False,
                strategy_used="http", article=_Article(),
            )

    app.state.service = _SvcWithArticle()
    body = c.post("/scrape", json={"url": "https://n.test/story",
                                   "mode": "article"}).json()
    assert body["article"]["title"] == "Big Story"
    assert body["article"]["byline"] == "Jane Doe"


def test_scrape_multi_host_cooldown_returns_429(route_client):
    """Lines 159-160: _scrape_multi catches HostCooldown → 429."""
    c, app = route_client

    class _CooldownSvc:
        async def scrape_multi(self, url, **kw):
            from ujin.scrape.service import HostCooldown
            raise HostCooldown("host on cooldown for 30s")

    app.state.service = _CooldownSvc()
    r = c.post("/scrape", json={"url": "https://n.test/",
                                "modes": ["links", "article"]})
    assert r.status_code == 429
    assert "cooldown" in r.json()["detail"]


def test_scrape_multi_generic_exception_returns_502(route_client):
    """Lines 163-164: _scrape_multi catches generic Exception → 502."""
    c, app = route_client

    class _BrokenSvc:
        async def scrape_multi(self, url, **kw):
            raise ConnectionError("upstream unreachable")

    app.state.service = _BrokenSvc()
    r = c.post("/scrape", json={"url": "https://n.test/",
                                "modes": ["links", "article"]})
    assert r.status_code == 502


def test_scrape_multi_http_exception_reraise(route_client):
    """Lines 161-162: _scrape_multi re-raises HTTPException unchanged."""
    c, app = route_client

    class _HttpExcSvc:
        async def scrape_multi(self, url, **kw):
            raise _HTTPException(status_code=403, detail="forbidden by downstream")

    app.state.service = _HttpExcSvc()
    r = c.post("/scrape", json={"url": "https://n.test/",
                                "modes": ["links", "article"]})
    assert r.status_code == 403
    assert "forbidden" in r.json()["detail"]


def test_scrape_single_http_exception_reraise(route_client):
    """Line 231: single-scrape handler re-raises HTTPException from service."""
    c, app = route_client

    class _HttpExcSvc:
        async def scrape(self, url, **kw):
            raise _HTTPException(status_code=422, detail="unprocessable url")

    app.state.service = _HttpExcSvc()
    r = c.post("/scrape", json={"url": "https://n.test/"})
    assert r.status_code == 422
    assert "unprocessable" in r.json()["detail"]


def test_discover_error_returns_502(route_client, monkeypatch):
    """Lines 346-347: discover catches Exception from discover_sources → 502."""
    c, _ = route_client

    async def _boom(http, homepage):
        raise RuntimeError("DNS failure")

    monkeypatch.setattr(routes_mod, "discover_sources", _boom)
    r = c.post("/discover", json={"homepage": "https://n.test/"})
    assert r.status_code == 502
    assert "DNS failure" in r.json()["detail"]


# ── ujin/sources/rss.py ──────────────────────────────────────────────────────


async def test_parse_feed_skips_entries_without_link(monkeypatch):
    """Line 30: entries with no/empty link are skipped via `continue`."""
    import ujin.sources.rss as rss_mod

    class _FakeEntry(dict):
        pass

    class _FakeParsed:
        entries = [
            _FakeEntry(link="", title="no link"),      # empty string → skipped
            _FakeEntry(title="missing link"),           # no 'link' key → skipped
            _FakeEntry(link="https://n.test/a", title="valid", summary=""),
        ]

    def _fake_parse(url):
        return _FakeParsed()

    monkeypatch.setattr(rss_mod.feedparser, "parse", _fake_parse)
    items = await rss_mod.parse_feed("https://n.test/feed.xml")
    assert len(items) == 1
    assert items[0].url == "https://n.test/a"


# ── ujin/session/store.py ────────────────────────────────────────────────────


async def test_session_store_save_exception_is_swallowed(tmp_path, monkeypatch):
    """Lines 42-43: jar.save() failure is caught and logged, not propagated."""
    from ujin.session.store import SessionStore

    store = SessionStore(path=tmp_path / "cookies.jar")

    def _bad_save(_path):
        raise OSError("disk full")

    monkeypatch.setattr(store._jar, "save", _bad_save)
    store.save()   # must not raise
