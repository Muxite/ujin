"""The remaining :8901 routes (/feed /sitemap /discover, batch limits, 429),
the breaking-tier scorer, article index heuristics, and sitemap parsing."""
from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

import ujin.scrape.routes as routes_mod  # noqa: E402
from ujin.scrape.app import create_scrape_app  # noqa: E402
from ujin.scrape.config import ScrapeConfig  # noqa: E402


@pytest.fixture
def client():
    app = create_scrape_app(ScrapeConfig())
    c = TestClient(app)
    c.__enter__()
    yield c, app
    c.__exit__(None, None, None)


def test_feed_route(client, monkeypatch):
    c, _ = client

    class _Item:
        url, title, summary, published = "https://n.test/a", "t", "s", "2026-06-09"

    async def fake_parse(url):
        return [_Item()]

    monkeypatch.setattr(routes_mod, "parse_feed", fake_parse)
    body = c.post("/feed", json={"url": "https://n.test/feed.xml"}).json()
    assert body["items"][0]["url"] == "https://n.test/a"

    async def boom(url):
        raise RuntimeError("bad feed")

    monkeypatch.setattr(routes_mod, "parse_feed", boom)
    assert c.post("/feed", json={"url": "https://n.test/feed.xml"}).status_code == 502
    assert c.post("/feed", json={"url": ""}).status_code == 400


def test_sitemap_route(client, monkeypatch):
    c, _ = client

    class _Entry:
        url, lastmod, title = "https://n.test/a", "2026-06-09", "story"

    async def fake_fetch(http, url):
        return [_Entry()]

    monkeypatch.setattr(routes_mod, "fetch_sitemap", fake_fetch)
    body = c.post("/sitemap", json={"url": "https://n.test/sitemap.xml"}).json()
    assert body["entries"][0]["lastmod"] == "2026-06-09"

    async def boom(http, url):
        raise RuntimeError("xml error")

    monkeypatch.setattr(routes_mod, "fetch_sitemap", boom)
    assert c.post("/sitemap", json={"url": "https://x"}).status_code == 502
    assert c.post("/sitemap", json={"url": ""}).status_code == 400


def test_discover_route(client, monkeypatch):
    c, _ = client

    class _Found:
        homepage = "https://n.test/"
        rss = ["https://n.test/feed.xml"]
        sitemap = ["https://n.test/sitemap.xml"]

    async def fake_discover(http, homepage):
        return _Found()

    monkeypatch.setattr(routes_mod, "discover_sources", fake_discover)
    body = c.post("/discover", json={"homepage": "https://n.test/"}).json()
    assert body["rss"] == ["https://n.test/feed.xml"]
    assert c.post("/discover", json={"homepage": ""}).status_code == 400


def test_scrape_cooldown_429_and_502(client):
    c, app = client

    class _Cooling:
        async def scrape(self, url, **kw):
            from ujin.scrape.service import HostCooldown

            raise HostCooldown("host on cooldown for 42s")

    app.state.service = _Cooling()
    r = c.post("/scrape", json={"url": "https://n.test/"})
    assert r.status_code == 429

    class _Broken:
        async def scrape(self, url, **kw):
            raise RuntimeError("fetch failed")

    app.state.service = _Broken()
    assert c.post("/scrape", json={"url": "https://n.test/"}).status_code == 502


def test_batch_limits_and_inline_errors(client):
    c, app = client

    class _Flaky:
        async def scrape_batch(self, items):
            return [RuntimeError("dead origin")]

    app.state.service = _Flaky()
    body = c.post("/scrape:batch",
                  json={"requests": [{"url": "https://n.test/"}]}).json()
    assert body["results"][0]["kind"] == "error"
    assert "dead origin" in body["results"][0]["note"]

    # over the configured max
    too_many = [{"url": f"https://n.test/{i}"} for i in range(10_000)]
    assert c.post("/scrape:batch", json={"requests": too_many}).status_code == 400
    # empty is fine
    assert c.post("/scrape:batch", json={"requests": []}).json() == {"results": []}


def test_scrape_invalid_cursor_400(client):
    c, app = client
    from ujin.extract.links import NormalizedLink
    from ujin.scrape.service import ScrapeResult

    class _Svc:
        async def scrape(self, url, **kw):
            return ScrapeResult(
                url=url, kind="links", fingerprint="fp", fetched_at=1.0,
                cached=False, age_secs=0.0, used_renderer=False,
                links=[NormalizedLink(url="https://n.test/a",
                                      text="a sufficiently long headline")],
            )

    app.state.service = _Svc()
    r = c.post("/scrape", json={"url": "https://n.test/", "page_size": 1,
                                "cursor": "%%%not-base64%%%"})
    assert r.status_code == 400


# ── breaking-tier scorer ─────────────────────────────────────────────────────

def test_tier_of_defaults_and_config():
    from ujin.trends.tier import tier_of

    assert tier_of("www.apnews.com", None) == "wire"
    assert tier_of("reuters.com", None) == "wire"
    assert tier_of("blog.example.com", None) == "mainstream"
    assert tier_of("anything.com", "specialty") == "specialty"


def test_lede_and_recency_and_trend_components():
    from ujin.trends.tier import (
        _lede_score,
        _recency_score,
        _trend_overlap_score,
    )

    assert _lede_score("BREAKING: markets fall") == 1.0
    assert _lede_score("JUST IN: something") == 1.0
    assert _lede_score("MARKETS TUMBLE WORLDWIDE today") == 0.6  # all-caps run
    assert _lede_score("a quiet feature story") == 0.0
    assert _lede_score("") == 0.0

    import time as _t
    from datetime import datetime, timezone

    now = _t.time()
    fresh = datetime.fromtimestamp(now - 60, tz=timezone.utc).isoformat()
    stale = datetime.fromtimestamp(now - 86400, tz=timezone.utc).isoformat()
    assert _recency_score(fresh, now=now) > 0.8
    assert _recency_score(stale, now=now) < 0.01
    assert _recency_score(None) == 0.0
    assert _recency_score("not-a-date") == 0.0
    assert _recency_score(
        datetime.fromtimestamp(now + 100, tz=timezone.utc).isoformat(),
        now=now) == 1.0  # future-dated clamps to 1

    assert _trend_overlap_score("fed rate decision", ["fed"]) == 0.5
    assert _trend_overlap_score("fed rate decision", ["fed", "rate"]) == 0.75
    assert _trend_overlap_score("nothing here", ["fed"]) == 0.0
    assert _trend_overlap_score("anything", []) == 0.0


def test_breaking_score_composition():
    from ujin.trends.tier import Weights, breaking_score

    score, comps = breaking_score(
        url="https://apnews.com/article/x",
        title="BREAKING: major event",
        tier_label="wire",
        trend_terms=["major"],
        weights=Weights(),
    )
    assert comps["source_rank"] == pytest.approx(0.20)   # wire = 1.0 * w
    assert comps["lede_marker"] == pytest.approx(0.10)
    assert comps["corroboration"] == 0.0                 # no store wired
    assert comps["trend_overlap"] == pytest.approx(0.05)
    assert score == pytest.approx(sum(comps.values()))


# ── article index heuristics ─────────────────────────────────────────────────

def test_article_rejects_index_urls(html_corpus):
    from ujin.extract.article import extract_article

    # section-front URL pattern → None even with article-ish HTML
    assert extract_article(html_corpus["article"], url="https://n.test/world") is None
    assert extract_article("", url="https://n.test/2026/06/09/x") is None


def test_looks_like_index_body():
    from ujin.extract.article import _looks_like_index_body

    assert _looks_like_index_body("World", "short\nshort\nshort") is True
    long_text = ("A paragraph that runs well past two hundred characters " * 6)
    assert _looks_like_index_body("A real specific headline",
                                  long_text * 3) is False


# ── sitemap parsing ──────────────────────────────────────────────────────────

def test_parse_sitemap_xml_fixture():
    from pathlib import Path

    from ujin.sources.sitemap import parse_sitemap_xml

    xml = (Path(__file__).parent / "fixtures" / "feeds" / "sitemap_news.xml").read_text()
    entries = parse_sitemap_xml(xml)
    assert len(entries) == 2
    assert entries[0].url.endswith("/markets-rally-on-rate-decision")
    # news-sitemap title flows through
    assert any("Markets rally" in (e.title or "") for e in entries)


def test_parse_sitemap_xml_garbage():
    from ujin.sources.sitemap import parse_sitemap_xml

    assert parse_sitemap_xml("<html>not xml</html>") == []
    assert parse_sitemap_xml("") == []
