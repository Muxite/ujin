"""Coverage-gap tests: routes_social, routes, build, metrics, sources/rss.

Closes the remaining sub-95% lines in each targeted module.  All tests are
offline and deterministic — no live network.
"""
from __future__ import annotations

import asyncio
import time

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from ujin.scrape.app import create_scrape_app  # noqa: E402
from ujin.scrape.config import ScrapeConfig  # noqa: E402

# ── shared helpers ─────────────────────────────────────────────────────────────

class _Post:
    def __init__(self, url, text):
        self.url, self.text = url, text


@pytest.fixture
def social_client():
    app = create_scrape_app(ScrapeConfig())
    c = TestClient(app)
    c.__enter__()
    yield c
    c.__exit__(None, None, None)


# ── routes_social.py: empty-input 400 guards (lines 60, 76, 93) ───────────────

def test_mastodon_empty_account_400(social_client):
    """Empty account → 400 (line 60)."""
    assert social_client.post("/social/mastodon", json={"account": ""}).status_code == 400


def test_x_empty_username_400(social_client):
    """Empty username for /social/x → 400 (line 76)."""
    assert social_client.post("/social/x", json={"username": ""}).status_code == 400


def test_truth_empty_username_400(social_client):
    """Empty username for /social/truth → 400 (line 93)."""
    assert social_client.post("/social/truth", json={"username": ""}).status_code == 400


# ── routes_social.py: corroboration score branches (lines 137, 141) ────────────

def test_corroborated_full_score_floor(social_client):
    """Cluster with n >= config.max_hosts_for_full_score gets floor=1.0 (line 137).

    Default ScrapeConfig has corroboration_max_hosts_for_full_score=5.
    Build a 5-host cluster so n==5 >= max_h==5.
    """
    from ujin.trends import CorroborationStore

    store = CorroborationStore(
        window_secs=1800,
        max_entries=100,
        min_hosts_for_corroboration=3,
        max_hosts_for_full_score=5,
    )
    now = time.time()
    for host in ("a.t", "b.t", "c.t", "d.t", "e.t"):
        store.add("five host breaking story tonight", host=host, ts=now)

    social_client.app.state.corroboration = store
    body = social_client.get("/trends/corroborated").json()
    assert body["clusters"], "expected at least one cluster"
    top = body["clusters"][0]
    assert len(top["hosts"]) == 5
    assert top["breaking_score_floor"] == pytest.approx(1.0)


def test_corroborated_below_min_hosts_floor_zero(social_client):
    """Cluster with n < config.corroboration_min_hosts gets floor=0.0 (line 141).

    Store uses min_hosts=2 so 2-host clusters appear in clusters().
    Config has corroboration_min_hosts=3 (default), so n=2 < 3 → else branch.
    """
    from ujin.trends import CorroborationStore

    store = CorroborationStore(
        window_secs=1800,
        max_entries=100,
        min_hosts_for_corroboration=2,
        max_hosts_for_full_score=5,
    )
    now = time.time()
    for host in ("x.test", "y.test"):
        store.add("minor two-source story", host=host, ts=now)

    social_client.app.state.corroboration = store
    body = social_client.get("/trends/corroborated").json()
    assert body["clusters"], "expected the 2-host cluster"
    item = body["clusters"][0]
    assert len(item["hosts"]) == 2
    # config min_h=3 > n=2 → floor=0.0 (line 141)
    assert item["breaking_score_floor"] == pytest.approx(0.0)


# ── scrape/build.py: learn_strategy branch (lines 98-102) ─────────────────────

class _FakeHttpForBuild:
    async def start(self): pass
    async def close(self): pass


@pytest.mark.asyncio
async def test_build_components_learn_strategy(monkeypatch):
    """learn_strategy=True wires StrategyFeedback (lines 98-102)."""
    import ujin.adapt.strategy as _strat_mod
    from ujin.scrape.build import build_scrape_components

    class _FakeStrategy:
        def __init__(self, store):
            pass

        def close(self):
            pass

    monkeypatch.setattr(_strat_mod, "StrategyFeedback", _FakeStrategy)
    cfg = ScrapeConfig(learn_strategy=True, strategy_db=":memory:")
    comps = await build_scrape_components(cfg)
    assert isinstance(comps.strategy, _FakeStrategy)
    await comps.http.close()


# ── scrape/build.py: strategy close (lines 124-128) ───────────────────────────

@pytest.mark.asyncio
async def test_close_components_strategy_closed():
    """close_scrape_components calls strategy.close() (lines 124-128)."""
    from ujin.cache import HostPolicy, ScrapeCache
    from ujin.scrape.build import ScrapeComponents, close_scrape_components
    from ujin.scrape.host_overrides import HostOverrideRegistry
    from ujin.scrape.metrics import HostMetrics

    closed = []

    class _FakeStrategy:
        def close(self):
            closed.append(True)

    comps = ScrapeComponents(
        http=_FakeHttpForBuild(),
        obscura=object(),
        cache=ScrapeCache(),
        policy=HostPolicy(),
        metrics=HostMetrics(),
        overrides=HostOverrideRegistry(),
        disk=None,
        strategy=_FakeStrategy(),
    )
    await close_scrape_components(comps)
    assert closed == [True]


@pytest.mark.asyncio
async def test_close_components_strategy_exception_swallowed():
    """strategy.close() exception is swallowed, not raised (lines 125-128)."""
    from ujin.cache import HostPolicy, ScrapeCache
    from ujin.scrape.build import ScrapeComponents, close_scrape_components
    from ujin.scrape.host_overrides import HostOverrideRegistry
    from ujin.scrape.metrics import HostMetrics

    class _BadStrategy:
        def close(self):
            raise RuntimeError("db locked")

    comps = ScrapeComponents(
        http=_FakeHttpForBuild(),
        obscura=object(),
        cache=ScrapeCache(),
        policy=HostPolicy(),
        metrics=HostMetrics(),
        overrides=HostOverrideRegistry(),
        disk=None,
        strategy=_BadStrategy(),
    )
    await close_scrape_components(comps)  # must not raise


# ── scrape/metrics.py: latency guard (branch 72->74) and _percentile (101, 105-106) ─

def test_host_metrics_negative_latency_not_appended():
    """latency_ms < 0 skips the deque append (branch 72->74)."""
    from ujin.scrape.metrics import HostMetrics

    m = HostMetrics()
    m.record("https://a.test/page", success=True, latency_ms=-1.0)
    snap = m.snapshot()
    assert snap["hosts"]["a.test"]["samples"] == 0
    assert snap["hosts"]["a.test"]["latency_ms_p50"] is None


def test_percentile_single_sample():
    """_percentile returns the sole value for a 1-element list (line 101)."""
    from ujin.scrape.metrics import _percentile

    assert _percentile([7.0], 0) == pytest.approx(7.0)
    assert _percentile([7.0], 50) == pytest.approx(7.0)
    assert _percentile([7.0], 100) == pytest.approx(7.0)


def test_percentile_multi_sample_nearest_rank():
    """_percentile computes nearest-rank for >=2 samples (lines 105-106)."""
    from ujin.scrape.metrics import _percentile

    samples = sorted([10.0, 20.0, 30.0, 40.0, 50.0])
    assert _percentile(samples, 0) == pytest.approx(10.0)
    assert _percentile(samples, 50) == pytest.approx(30.0)
    assert _percentile(samples, 100) == pytest.approx(50.0)


def test_host_metrics_snapshot_p50_p95_populated():
    """Snapshot with multiple recordings yields non-None p50/p95 (hits 105-106 via snapshot)."""
    from ujin.scrape.metrics import HostMetrics

    m = HostMetrics()
    for i in range(10):
        m.record(
            "https://news.test/",
            success=(i % 2 == 0),
            latency_ms=float(i * 10),
            used_renderer=(i == 0),
            cached=(i == 1),
            strategy="http",
        )
    snap = m.snapshot()
    h = snap["hosts"]["news.test"]
    assert h["fetches"] == 10
    assert h["latency_ms_p50"] is not None
    assert h["latency_ms_p95"] is not None


# ── scrape/routes.py: article payload (line 55) ──────────────────────────────

class _ArticleService:
    """Returns a ScrapeResult with a non-None article."""

    async def scrape(self, url, *, mode="links", force_refresh=False,
                     enrich_html_top_n=0, render="auto", actions=None):
        from ujin.scrape.service import ScrapeResult

        class _Art:
            url = "https://x.test/article/1"
            title = "Big headline"
            text = "Body text."
            byline = "Author A"
            published = "2026-01-01"
            language = "en"
            top_image = None

        return ScrapeResult(
            url=url, kind="article", fingerprint="fp2", fetched_at=1.0,
            cached=False, age_secs=0.0, used_renderer=False,
            strategy_used="http", links=[], article=_Art(),
        )


def _client_with_svc(svc):
    app = create_scrape_app(ScrapeConfig())
    client = TestClient(app)
    client.__enter__()
    app.state.service = svc
    return client


def test_scrape_article_payload_built():
    """result.article is not None → ArticlePayload constructed (line 55)."""
    client = _client_with_svc(_ArticleService())
    try:
        body = client.post("/scrape", json={"url": "https://x.test/a", "mode": "article"}).json()
        assert body["article"]["title"] == "Big headline"
        assert body["article"]["byline"] == "Author A"
        assert body["article"]["language"] == "en"
    finally:
        client.__exit__(None, None, None)


# ── scrape/routes.py: HostCooldown in scrape() → 429 (lines 229-230) ────────

class _CooldownService:
    async def scrape(self, url, **kw):
        from ujin.scrape.service import HostCooldown
        raise HostCooldown(f"host on cooldown: {url}")


def test_scrape_host_cooldown_429():
    """service.scrape() raises HostCooldown → HTTP 429 (routes.py ~line 229-230)."""
    client = _client_with_svc(_CooldownService())
    try:
        r = client.post("/scrape", json={"url": "https://x.test/"})
        assert r.status_code == 429
    finally:
        client.__exit__(None, None, None)


# ── scrape/routes.py: re-raise HTTPException in scrape() (line 231) ──────────

class _HttpExcService:
    async def scrape(self, url, **kw):
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="service says forbidden")


def test_scrape_reraises_http_exception_from_service():
    """service.scrape() raises HTTPException → re-raised unchanged (line 231)."""
    client = _client_with_svc(_HttpExcService())
    try:
        r = client.post("/scrape", json={"url": "https://x.test/"})
        assert r.status_code == 403
    finally:
        client.__exit__(None, None, None)


# ── scrape/routes.py: _scrape_multi exceptions (lines 159-164) ───────────────

class _MultiService:
    def __init__(self, exc=None):
        self._exc = exc

    async def scrape_multi(self, url, *, modes, force_refresh=False,
                           render="auto", actions=None):
        if self._exc is not None:
            raise self._exc
        from ujin.scrape.service import ScrapeResult
        res = ScrapeResult(
            url=url, kind="links", fingerprint="fp", fetched_at=1.0,
            cached=False, age_secs=0.0, used_renderer=False,
            strategy_used="http", links=[],
        )
        return {m: res for m in modes}


def test_scrape_multi_host_cooldown_429():
    """scrape_multi raises HostCooldown → 429 (lines 159-160)."""
    from ujin.scrape.service import HostCooldown

    client = _client_with_svc(_MultiService(exc=HostCooldown("host busy")))
    try:
        r = client.post("/scrape", json={
            "url": "https://x.test/", "modes": ["links", "article"]
        })
        assert r.status_code == 429
    finally:
        client.__exit__(None, None, None)


def test_scrape_multi_reraises_http_exception():
    """scrape_multi raises HTTPException → re-raised (lines 161-162)."""
    from fastapi import HTTPException

    client = _client_with_svc(
        _MultiService(exc=HTTPException(status_code=403, detail="denied"))
    )
    try:
        r = client.post("/scrape", json={
            "url": "https://x.test/", "modes": ["links"]
        })
        assert r.status_code == 403
    finally:
        client.__exit__(None, None, None)


def test_scrape_multi_generic_error_502():
    """scrape_multi raises generic exception → 502 (lines 163-164)."""
    client = _client_with_svc(_MultiService(exc=RuntimeError("unexpected")))
    try:
        r = client.post("/scrape", json={
            "url": "https://x.test/", "modes": ["links", "article"]
        })
        assert r.status_code == 502
    finally:
        client.__exit__(None, None, None)


# ── scrape/routes.py: discover exception → 502 (lines 346-347) ───────────────

def test_discover_error_502(monkeypatch):
    """discover_sources raises → HTTP 502 (lines 346-347)."""
    import ujin.scrape.routes as _r

    async def _boom(http, homepage):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(_r, "discover_sources", _boom)

    app = create_scrape_app(ScrapeConfig())
    with TestClient(app) as client:
        r = client.post("/discover", json={"homepage": "https://example.com/"})
        assert r.status_code == 502


# ── sources/rss.py: skip entries without a link (line 30) ────────────────────

@pytest.mark.asyncio
async def test_parse_feed_skips_linkless_entries(monkeypatch):
    """Entries with empty or missing 'link' are skipped via continue (line 30)."""
    import feedparser

    from ujin.sources.rss import parse_feed

    class _FakeParsed:
        entries = [
            {"link": "", "title": "empty link entry", "summary": ""},
            {"link": "https://example.com/real", "title": "real story", "summary": "body"},
            {"title": "no link key at all", "summary": "also dropped"},
        ]

    monkeypatch.setattr(feedparser, "parse", lambda url: _FakeParsed())
    items = await parse_feed("https://example.com/feed.xml")
    assert len(items) == 1
    assert items[0].url == "https://example.com/real"
    assert items[0].title == "real story"
