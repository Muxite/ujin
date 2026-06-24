"""Coverage-tighten tests.

Targets the remaining uncovered lines/branches reported by ``make cov``:
  - ujin/trends/tier.py       line 84 (_recency_score future timestamp)
  - ujin/sources/discover.py  54->47, 70->61 (normalize_url returning None)
  - ujin/scrape/host_overrides.py  106-108 (yaml ImportError)
  - ujin/service.py           102->104 (async prev_cb awaited)
  - ujin/scrape/app.py        95-99 (_refresh_trends_loop success path)
  - ujin/scrape/service.py    many branches (article profile, enrich, bias,
                              direct-sitemap/rss, extract-with-profile)

All tests are offline and deterministic.
"""
from __future__ import annotations

import sys
import asyncio

import pytest

from conftest import FakeBrowser, FakeHttp, FakeObscura
from ujin.cache import HostPolicy, ScrapeCache
from ujin.fetch.http import HttpResponse
from ujin.scrape.config import ScrapeConfig
from ujin.scrape.host_overrides import (
    ArticleProfile,
    ExtractProfile,
    HostOverride,
    HostOverrideRegistry,
)
from ujin.scrape.service import ScrapeService


# ── helpers ──────────────────────────────────────────────────────────────────

def _resp(url, body, status=200):
    return HttpResponse(url=url, status=status, body=body, final_url=url)


def _service(http, **kw):
    kw.setdefault("obscura", FakeObscura())
    kw.setdefault("cache", ScrapeCache())
    kw.setdefault("policy", HostPolicy(cooldown_secs=60))
    kw.setdefault("config", ScrapeConfig())
    return ScrapeService(http=http, **kw)


def _index_html(n=8, host="https://news.example.com"):
    rows = "".join(
        f'<article><h2><a href="{host}/2026/06/09/story-{i}">'
        f"A sufficiently long headline number {i}</a></h2></article>"
        for i in range(n)
    )
    return f"<html><body><main>{rows}</main></body></html>"


HOME = "https://news.example.com/"


# ── ujin/trends/tier.py  line 84 ─────────────────────────────────────────────

def test_recency_score_naive_datetime_gets_utc_stamp():
    """_recency_score line 84: naive datetime (no tzinfo) has UTC applied."""
    from ujin.trends.tier import _recency_score

    # "2020-01-01T00:00:00" has no timezone → dt.tzinfo is None → line 84
    # now=0 means age_secs is large negative (future relative to epoch) → 1.0
    # OR very large positive → exp(-big) ≈ 0
    # Use a recent-ish past time so we get a non-trivial result but definitely
    # hits line 84 (tzinfo is None branch)
    result = _recency_score("2020-01-01T00:00:00", now=1577836800.0)
    # The datetime 2020-01-01T00:00:00 UTC == 1577836800; age = 0 → exp(0) = 1.0
    assert abs(result - 1.0) < 0.01


# ── ujin/sources/discover.py  54->47, 70->61 ─────────────────────────────────

def test_from_html_rss_link_unresolvable_href_skipped():
    """discover.py 54->47: normalize_url returns None for '#fragment' → skipped."""
    from ujin.sources.discover import _from_html

    html = (
        '<html><head>'
        '<link rel="alternate" type="application/rss+xml" href="#fragment">'
        '</head></html>'
    )
    result = _from_html(html, base="https://site.example.com/")
    assert result == []


def test_from_robots_sitemap_unresolvable_url_skipped():
    """discover.py 70->61: normalize_url returns None for 'javascript:void(0)' → skipped."""
    from ujin.sources.discover import _from_robots

    robots = "Sitemap: javascript:void(0)\nSitemap: https://site.example.com/sitemap.xml\n"
    result = _from_robots(robots, "https://site.example.com")
    # only the valid sitemap survives
    assert result == ["https://site.example.com/sitemap.xml"]
    assert not any("javascript" in u for u in result)


# ── ujin/scrape/host_overrides.py  106-108 ───────────────────────────────────

def test_host_overrides_from_file_yaml_importerror(tmp_path, monkeypatch):
    """host_overrides.py 106-108: yaml ImportError → empty registry returned."""
    p = tmp_path / "per_host.yaml"
    p.write_text("hosts: {}\n")

    # Force 'import yaml' to fail inside from_file by hiding yaml in sys.modules
    monkeypatch.setitem(sys.modules, "yaml", None)

    reg = HostOverrideRegistry.from_file(str(p))
    assert reg._by_host == {}


# ── ujin/service.py  102->104 ────────────────────────────────────────────────

def test_service_wire_sync_callback_noncoroutine_branch(monkeypatch):
    """service.py 102->104: prev_cb is sync → iscoroutine(r) is False → branch
    102->104 taken (no await, falls through to hub.broadcast)."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ujin.engine import PollEngine
    from ujin.poll.command import CommandPollable
    from ujin.service import create_app

    called: list[str] = []

    def _sync_on_change(key: str, result) -> None:
        # Sync callback: calling it returns None, not a coroutine
        called.append(key)

    # Pre-build an engine with one target that has a sync on_change
    pre_engine = PollEngine()
    pre_engine.add(CommandPollable(argv=["printf", "x"]), on_change=_sync_on_change)

    # Redirect PollEngine() inside create_app to return our pre-built engine
    monkeypatch.setattr("ujin.engine.PollEngine", lambda: pre_engine)

    app = create_app(run_engine=False)
    with TestClient(app) as client:
        r = client.post("/sweep")
        assert r.status_code == 200

    # Sync callback was called; iscoroutine(None) is False → 102->104 branch taken
    assert called


# ── ujin/scrape/app.py  95-99 ────────────────────────────────────────────────

def test_scrape_app_trends_refresh_loop_success_path(monkeypatch):
    """scrape/app.py 95-99: _refresh_trends_loop runs fetch_x_trends successfully."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ujin.scrape.app import create_scrape_app
    from ujin.scrape.config import ScrapeConfig

    class _FakeTrendItem:
        def __init__(self, tag: str) -> None:
            self.tag = tag

    class _FakeTrendsResult:
        items = [_FakeTrendItem("#AI"), _FakeTrendItem(""), _FakeTrendItem("#Python")]
        source = "test"

    async def _fake_fetch_x_trends(region: str, count: int):
        return _FakeTrendsResult()

    # Patch the live symbol inside the social package so the lifespan's local
    # 'from ..sources.social import fetch_x_trends' picks it up.
    monkeypatch.setattr("ujin.sources.social.fetch_x_trends", _fake_fetch_x_trends)
    monkeypatch.setattr("ujin.sources.social.x_trends.fetch_x_trends", _fake_fetch_x_trends)

    cfg = ScrapeConfig(enable_breaking_scorer=True)
    app = create_scrape_app(cfg)

    with TestClient(app) as client:
        r = client.get("/health")
        assert r.status_code == 200


# ── ujin/scrape/service.py  article-profile branches (650, 651->653) ─────────

async def test_article_profile_succeeds_skips_generic_extractor():
    """scrape/service.py 651->653: apply_article_profile returns an Article
    (non-None) → extract_article is NOT called (False branch of line 651)."""
    art_html = (
        "<html><body>"
        '<div class="article-body">'
        "<p>This is the article body text that is long enough to pass the 30 char threshold here.</p>"
        "</div>"
        "</body></html>"
    )
    overrides = HostOverrideRegistry({
        "news.example.com": HostOverride(
            extract=ExtractProfile(
                article=ArticleProfile(body=".article-body")
            )
        )
    })
    art_url = "https://news.example.com/article"
    http = FakeHttp({art_url: _resp(art_url, art_html)})
    svc = _service(http, overrides=overrides)

    # _extract_mode (via scrape_multi) hits line 649 → profile matches →
    # apply_article_profile returns Article → article is not None → 651->653
    results = await svc.scrape_multi(art_url, modes=["article"])
    r = results["article"]
    assert r.kind == "article"
    assert r.article is not None


async def test_article_profile_returns_none_falls_back_to_generic(html_corpus):
    """scrape/service.py 650, 651->653: _extract_mode (called by scrape_multi)
    hits apply_article_profile which returns None → extract_article fallback.

    Lines 650 and 651->653 live in _extract_mode, not in scrape(); they are
    only reached via scrape_multi()."""
    overrides = HostOverrideRegistry({
        "news.example.com": HostOverride(
            extract=ExtractProfile(
                article=ArticleProfile(body=".nonexistent-selector-xyz")
            )
        )
    })
    art_url = "https://news.example.com/2026/06/08/quantum"
    http = FakeHttp({art_url: _resp(art_url, html_corpus["article"])})
    svc = _service(http, overrides=overrides)

    # scrape_multi calls _extract_mode for each mode, hitting lines 649-653
    results = await svc.scrape_multi(art_url, modes=["article"])
    r = results["article"]
    # apply_article_profile returns None (.nonexistent absent) → extract_article
    assert r.kind == "article"
    assert r.article is not None


# ── scrape/service.py enrich-html branches (859, 863->861, 865) ───────────────

async def test_enrich_html_article_is_none_silent():
    """scrape/service.py 859: article is None → enrich silently skips link."""
    art_url = "https://news.example.com/2026/06/09/story-0"
    # 8-link index page so the HTML fast-path (fast_path_min_links=5) succeeds
    # without needing an obscura fallback.
    index = _index_html(n=8)  # includes story-0..7; art_url == story-0
    http = FakeHttp({
        HOME: _resp(HOME, index),
        art_url: _resp(art_url, "<html><body></body></html>"),  # no extractable article
    })
    svc = _service(http)
    r = await svc.scrape(HOME, mode="combined", enrich_html_top_n=1)

    assert r.strategy_used == "combined"
    link = next((l for l in r.links if l.url == art_url), None)
    if link is not None:
        assert not link.summary  # empty summary: enrich returned (url, None)


async def test_enrich_html_short_paragraphs_no_summary(monkeypatch):
    """scrape/service.py 863->861, 865: article text exists but all paragraphs
    are < 60 chars → falls through the loop and returns None summary."""
    art_url = "https://news.example.com/2026/06/09/story-0"
    # 8-link index so HTTP fast-path succeeds; story-0 is the enrich target
    index = _index_html(n=8)

    class _ShortArticle:
        text = "Brief.\n\nAlso brief.\n\nQuite short."

    # Monkeypatch extract_article to return a fixed article with short paragraphs
    monkeypatch.setattr("ujin.scrape.service.extract_article",
                        lambda html, url=None: _ShortArticle())

    http = FakeHttp({
        HOME: _resp(HOME, index),
        art_url: _resp(art_url, "<html><body><p>Brief.</p></body></html>"),
    })
    svc = _service(http)
    r = await svc.scrape(HOME, mode="combined", enrich_html_top_n=1)

    assert r.strategy_used == "combined"
    link = next((l for l in r.links if l.url == art_url), None)
    if link is not None:
        assert not link.summary  # short paragraphs → (canon, None) → no summary


# ── scrape/service.py _extract_with_profile (961, 965->964) ──────────────────

async def test_extract_with_profile_returns_early_at_five_links():
    """scrape/service.py 961: profile yields ≥5 links → early return."""
    # Profile selector 'a.story' matches all 6 story links directly
    overrides = HostOverrideRegistry({
        "news.example.com": HostOverride(
            extract=ExtractProfile(link_selectors=("a.story",))
        )
    })
    stories = "".join(
        f'<a class="story" href="{HOME}s{i}">Story {i}: a sufficiently long headline here</a>'
        for i in range(6)
    )
    html = f"<html><body><main>{stories}</main></body></html>"
    http = FakeHttp({HOME: _resp(HOME, html)})
    svc = _service(http, overrides=overrides)

    r = await svc.scrape(HOME, mode="links")
    # All 6 come from profile and ≥5 → returned at line 961 before union
    assert len(r.links) >= 5


async def test_extract_with_profile_deduplicates_generic_overlap():
    """scrape/service.py 965->964: a generic link whose URL is already in seen
    (from profile) is skipped rather than double-appended."""
    overrides = HostOverrideRegistry({
        "news.example.com": HostOverride(
            extract=ExtractProfile(link_selectors=("a.story",))
        )
    })
    # 2 profile (.story) links; 10 links in <main> including those same 2 + 8 more
    story_anchors = "".join(
        f'<a class="story" href="{HOME}s{i}">Story {i}: long enough profile headline</a>'
        for i in range(2)
    )
    main_articles = "".join(
        f'<article><h2><a href="{HOME}s{i}">Story {i}: long enough generic headline</a></h2></article>'
        for i in range(10)
    )
    html = f"<html><body>{story_anchors}<main>{main_articles}</main></body></html>"
    http = FakeHttp({HOME: _resp(HOME, html)})
    svc = _service(http, overrides=overrides)

    r = await svc.scrape(HOME, mode="links")
    # URLs s0 and s1 appear once each (deduplicated); s2-s9 are new
    urls = [l.url for l in r.links]
    assert urls.count(f"{HOME}s0") == 1
    assert urls.count(f"{HOME}s1") == 1


# ── scrape/service.py _biased_auto_strategy (1065, 1067, 1071-1073) ──────────

def test_bias_strategy_unknown_backend_returns_none():
    """scrape/service.py 1065: recommended backend not in _BACKEND_TO_OVERRIDE → None."""
    from ujin.adapt import StrategyFeedback

    fb = StrategyFeedback(":memory:")
    fb.record("news.example.com", ("webhook", "json"), ok=True, latency=0.1)
    svc = _service(FakeHttp({}), strategy_feedback=fb)

    result = svc._biased_auto_strategy("https://news.example.com/")
    assert result is None
    fb.close()


def test_bias_strategy_browser_without_fetcher_returns_none():
    """scrape/service.py 1067: browser recommended but service has no browser → None."""
    from ujin.adapt import StrategyFeedback

    fb = StrategyFeedback(":memory:")
    fb.record("news.example.com", ("browser", "html"), ok=True, latency=0.1)
    svc = _service(FakeHttp({}), strategy_feedback=fb, browser=None)

    result = svc._biased_auto_strategy("https://news.example.com/")
    assert result is None
    fb.close()


def test_bias_strategy_exception_returns_none():
    """scrape/service.py 1071-1073: exception in recommend() is caught → None."""

    class _RaisingStrategy:
        def recommend(self, host: str):
            raise RuntimeError("index corrupted")

    svc = _service(FakeHttp({}), strategy_feedback=_RaisingStrategy())

    result = svc._biased_auto_strategy("https://news.example.com/")
    assert result is None


# ── scrape/service.py _record_attempt (1107-1108) ────────────────────────────

def test_record_attempt_exception_is_swallowed():
    """scrape/service.py 1107-1108: strategy.record() raising → exception caught."""

    class _RaisingStrategy:
        def record(self, host, strategy, *, ok, latency):
            raise RuntimeError("db write failed")

    svc = _service(
        FakeHttp({}),
        strategy_feedback=_RaisingStrategy(),
        config=ScrapeConfig(learn_strategy=True),
    )
    # Must not propagate the exception
    svc._record_attempt(
        "https://news.example.com/", ("http", "html"), ok=True, latency=1.0
    )


# ── scrape/service.py _walk_altpath_chain (1260->1267) ───────────────────────

async def test_walk_altpath_rss_returns_none_falls_through(monkeypatch):
    """scrape/service.py 1260->1267: rss_url set but try_rss_fallback returns None."""
    overrides = HostOverrideRegistry({
        "thin.example.com": HostOverride(
            rss_url="https://thin.example.com/feed.xml"
        )
    })

    async def _null_sitemap(http, url):
        return None

    async def _null_rss(rss_url, parse_fn):
        return None

    monkeypatch.setattr("ujin.scrape.service.try_sitemap_news", _null_sitemap)
    monkeypatch.setattr("ujin.scrape.service.try_rss_fallback", _null_rss)

    home = "https://thin.example.com/"
    # Thin HTTP page; obscura also returns thin HTML so html is not None and
    # the scrape completes without RuntimeError even with 0 synth links.
    thin = "<html><body><p>thin</p></body></html>"
    http = FakeHttp({home: _resp(home, thin)})
    svc = _service(http, overrides=overrides, obscura=FakeObscura(html=thin))

    r = await svc.scrape(home, mode="links")
    # altpath returned None (1260->1267 covered); scrape falls through
    assert r.kind in ("links", "empty")


# ── scrape/service.py _direct_sitemap branches ───────────────────────────────

async def test_direct_sitemap_no_pinned_fallback_to_autodiscovery(monkeypatch):
    """scrape/service.py 1279->1292, 1294: no pinned_url → try_sitemap_news →
    returns None → _direct_sitemap returns None."""
    overrides = HostOverrideRegistry({
        "news.example.com": HostOverride(
            strategy="sitemap_news",
            sitemap_url=None,  # no pinned URL → if pinned: skipped (1279->1292)
        )
    })

    async def _null_sitemap(http, url):
        return None

    monkeypatch.setattr("ujin.scrape.service.try_sitemap_news", _null_sitemap)

    # Rich HTML page so the HTTP fast-path succeeds (>= fast_path_min_links)
    # without needing an obscura fallback after _direct_sitemap returns None.
    http = FakeHttp({HOME: _resp(HOME, _index_html())})
    svc = _service(http, overrides=overrides)

    r = await svc.scrape(HOME, mode="links")
    # _direct_sitemap returned None (1294); scrape fell through to HTTP result
    assert r.kind == "links"


async def test_direct_sitemap_bad_http_status_fallback(monkeypatch):
    """scrape/service.py 1282->1292: pinned URL returns non-200 → fallback to
    auto-discovery which also returns None."""
    sm_url = "https://news.example.com/sitemap.xml"
    overrides = HostOverrideRegistry({
        "news.example.com": HostOverride(
            strategy="sitemap_news",
            sitemap_url=sm_url,
        )
    })

    async def _null_sitemap(http, url):
        return None

    monkeypatch.setattr("ujin.scrape.service.try_sitemap_news", _null_sitemap)

    # Return 503 for the pinned sitemap (1282->1292); rich HTML so HTTP
    # fast-path succeeds after _direct_sitemap returns None.
    http = FakeHttp({
        HOME: _resp(HOME, _index_html()),
        sm_url: _resp(sm_url, "", status=503),
    })
    svc = _service(http, overrides=overrides)

    r = await svc.scrape(HOME, mode="links")
    assert r.kind == "links"


async def test_direct_sitemap_links_empty_after_filter_fallback(monkeypatch):
    """scrape/service.py 1287->1292: pinned sitemap returns 200 but all entries
    are filtered out → empty links list → fallback to auto-discovery."""
    sm_url = "https://news.example.com/sm.xml"
    # Sitemap with only denied paths
    sm_body = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://news.example.com/sports/lottery</loc></url>
  <url><loc>https://news.example.com/ads/promo</loc></url>
</urlset>"""

    overrides = HostOverrideRegistry({
        "news.example.com": HostOverride(
            strategy="sitemap_news",
            sitemap_url=sm_url,
            extract=ExtractProfile(
                url_path_deny_patterns=("/sports/", "/ads/")
            ),
        )
    })

    async def _null_sitemap(http, url):
        return None

    monkeypatch.setattr("ujin.scrape.service.try_sitemap_news", _null_sitemap)

    # Rich HTML fallback so the scrape completes after the sitemap filter
    # removes all entries and auto-discovery also returns None.
    http = FakeHttp({
        HOME: _resp(HOME, _index_html()),
        sm_url: _resp(sm_url, sm_body),
    })
    svc = _service(http, overrides=overrides)

    r = await svc.scrape(HOME, mode="links")
    # All sitemap entries denied (1287->1292); auto-discovery returns None (1294);
    # scrape falls back to HTTP result from the normal fetch path.
    assert r.kind == "links"


# ── scrape/service.py 180->184 ───────────────────────────────────────────────

async def test_direct_sitemap_returns_none_continues_to_rss_check(monkeypatch):
    """scrape/service.py 180->184: override.strategy==sitemap_news,
    _direct_sitemap returns None → execution continues past the if at line 180."""
    overrides = HostOverrideRegistry({
        "news.example.com": HostOverride(
            strategy="sitemap_news",
            sitemap_url=None,  # no pinned URL
        )
    })

    async def _null_sitemap(http, url):
        return None

    monkeypatch.setattr("ujin.scrape.service.try_sitemap_news", _null_sitemap)

    # Rich HTTP page so scrape eventually succeeds via normal fetch
    http = FakeHttp({HOME: _resp(HOME, _index_html())})
    svc = _service(http, overrides=overrides)

    r = await svc.scrape(HOME, mode="links")
    # 180->184 covered: _direct_sitemap None → fell past `if alt is not None:`
    assert r.kind == "links"
