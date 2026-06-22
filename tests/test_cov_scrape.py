"""Coverage tests for ujin/scrape/{app,build,config,host_overrides,service}.py.

All tests run fully offline — injected fakes / direct construction, no live network.
"""
from __future__ import annotations

import time

import pytest
from conftest import FakeHttp, FakeObscura

from ujin.cache import CachedEntry, HostPolicy, ScrapeCache
from ujin.fetch.http import HttpResponse
from ujin.scrape.config import ScrapeConfig
from ujin.scrape.host_overrides import (
    ArticleProfile,
    ExtractProfile,
    HostOverride,
    HostOverrideRegistry,
)
from ujin.scrape.service import ScrapeService

pytestmark = pytest.mark.asyncio

HOME = "https://news.example.com/"


def _resp(url, body="", status=200, **kw):
    return HttpResponse(url=url, status=status, body=body, final_url=url, **kw)


def _service(http, **kw):
    kw.setdefault("obscura", FakeObscura())
    kw.setdefault("cache", ScrapeCache())
    kw.setdefault("policy", HostPolicy())
    kw.setdefault("config", ScrapeConfig())
    return ScrapeService(http=http, **kw)


def _index_html(n=8, host="https://news.example.com"):
    rows = "".join(
        f'<article><h2><a href="{host}/2026/06/09/story-{i}">'
        f"Headline number {i} is long enough to count"
        "</a></h2></article>"
        for i in range(n)
    )
    return f"<html><body><main>{rows}</main></body></html>"


# ─── config.py: _coerce / from_env ─────────────────────────────────────────

def test_from_env_coerces_bool_true():
    for v in ("1", "true", "TRUE", "yes", "on"):
        cfg = ScrapeConfig.from_env({"UJIN_BROWSER_ENABLED": v})
        assert cfg.browser_enabled is True, f"expected True for {v!r}"


def test_from_env_coerces_bool_false():
    cfg = ScrapeConfig.from_env({"UJIN_BROWSER_ENABLED": "false"})
    assert cfg.browser_enabled is False


def test_from_env_coerces_int():
    cfg = ScrapeConfig.from_env({"CACHE_MAX_ENTRIES": "4096"})
    assert cfg.cache_max_entries == 4096


def test_from_env_coerces_float():
    cfg = ScrapeConfig.from_env({"BREAKING_THRESHOLD": "0.55"})
    assert cfg.breaking_threshold == pytest.approx(0.55)


def test_from_env_str_passthrough():
    cfg = ScrapeConfig.from_env({"SCRAPER_USER_AGENT": "mybot/2.0"})
    assert cfg.user_agent == "mybot/2.0"


# ─── host_overrides.py ────────────────────────────────────────────────────────

def test_from_file_nonexistent_returns_empty(tmp_path):
    reg = HostOverrideRegistry.from_file(str(tmp_path / "no_such.yaml"))
    assert reg.is_empty()
    assert reg.hosts() == []


def test_from_dict_skips_non_dict_hosts():
    reg = HostOverrideRegistry.from_dict(
        {
            "hosts": {
                "example.com": "not-a-dict",
                "other.com": {"strategy": "http"},
            }
        }
    )
    assert "example.com" not in reg.hosts()
    assert "other.com" in reg.hosts()


def test_lookup_suffix_match():
    reg = HostOverrideRegistry({"nytimes.com": HostOverride(strategy="http")})
    ov = reg.lookup("https://www.nytimes.com/article/1")
    assert ov.strategy == "http"


def test_lookup_no_match_returns_default():
    reg = HostOverrideRegistry({"nytimes.com": HostOverride(strategy="http")})
    assert reg.lookup("https://wapo.com/article/1").strategy == "auto"


def test_lookup_host_suffix_match():
    reg = HostOverrideRegistry({"nytimes.com": HostOverride(strategy="http")})
    assert reg.lookup_host("markets.nytimes.com").strategy == "http"


def test_lookup_host_no_match_returns_default():
    reg = HostOverrideRegistry({"nytimes.com": HostOverride(strategy="http")})
    assert reg.lookup_host("wapo.com").strategy == "auto"


def test_is_empty_and_hosts():
    empty = HostOverrideRegistry({})
    assert empty.is_empty()
    assert empty.hosts() == []

    reg = HostOverrideRegistry({"example.com": HostOverride()})
    assert not reg.is_empty()
    assert reg.hosts() == ["example.com"]


# ─── build.py: close_scrape_components and build_scrape_components ────────────

class _FakeHttp:
    async def start(self): pass
    async def close(self): pass


class _FakeDisk:
    def __init__(self, entries=None, raise_on_flush=False):
        self._entries = list(entries or [])
        self._raise = raise_on_flush
        self.flushed = False
        self.closed = False

    def load_all(self):
        return iter(self._entries)

    def flush_from(self, items):
        if self._raise:
            raise OSError("disk flush error")
        self.flushed = True

    def close(self):
        self.closed = True


class _FakeBrowserFetcher:
    def __init__(self, raise_on_close=False):
        self._raise = raise_on_close
        self.closed = False

    async def close(self):
        if self._raise:
            raise RuntimeError("browser close failed")
        self.closed = True


async def _make_comps(disk=None, browser=None):
    from ujin.scrape.build import ScrapeComponents
    from ujin.scrape.metrics import HostMetrics

    return ScrapeComponents(
        http=_FakeHttp(),
        obscura=object(),
        cache=ScrapeCache(),
        policy=HostPolicy(),
        metrics=HostMetrics(),
        overrides=HostOverrideRegistry(),
        disk=disk,
        browser=browser,
    )


async def test_close_components_disk_flushes_and_closes():
    from ujin.scrape.build import close_scrape_components

    disk = _FakeDisk()
    comps = await _make_comps(disk=disk)
    await close_scrape_components(comps)
    assert disk.flushed
    assert disk.closed


async def test_close_components_disk_flush_exception_does_not_raise():
    from ujin.scrape.build import close_scrape_components

    disk = _FakeDisk(raise_on_flush=True)
    comps = await _make_comps(disk=disk)
    await close_scrape_components(comps)


async def test_close_components_browser_closed():
    from ujin.scrape.build import close_scrape_components

    browser = _FakeBrowserFetcher()
    comps = await _make_comps(browser=browser)
    await close_scrape_components(comps)
    assert browser.closed


async def test_close_components_browser_close_exception_does_not_raise():
    from ujin.scrape.build import close_scrape_components

    browser = _FakeBrowserFetcher(raise_on_close=True)
    comps = await _make_comps(browser=browser)
    await close_scrape_components(comps)


async def test_build_components_disk_loads_entries(monkeypatch):
    from ujin.scrape.build import build_scrape_components

    entry = CachedEntry(
        url="https://example.com/",
        fingerprint="fp",
        payload={"links": []},
        fetched_at=time.monotonic(),
    )

    class _MockDisk:
        def __init__(self, path): pass
        def load_all(self): return [("links:https://example.com/", entry)]

    monkeypatch.setattr("ujin.scrape.build.DiskCache", _MockDisk)
    cfg = ScrapeConfig(disk_cache_path="/fake/cache.db")
    comps = await build_scrape_components(cfg)
    assert comps.cache.get("links:https://example.com/") is not None
    await comps.http.close()


async def test_build_components_disk_init_failure_continues(monkeypatch):
    from ujin.scrape.build import build_scrape_components

    class _FailDisk:
        def __init__(self, path):
            raise OSError("init failed")

    monkeypatch.setattr("ujin.scrape.build.DiskCache", _FailDisk)
    cfg = ScrapeConfig(disk_cache_path="/fake/cache.db")
    comps = await build_scrape_components(cfg)
    assert comps.disk is None
    await comps.http.close()


async def test_build_components_browser_not_available(monkeypatch):
    import ujin.fetch.browser as _browser_mod

    from ujin.scrape.build import build_scrape_components

    monkeypatch.setattr(_browser_mod, "browser_available", lambda engine: False)
    cfg = ScrapeConfig(browser_enabled=True)
    comps = await build_scrape_components(cfg)
    assert comps.browser is None
    await comps.http.close()


async def test_build_components_browser_available(monkeypatch):
    import ujin.fetch.browser as _browser_mod

    from ujin.scrape.build import build_scrape_components

    class _FakeBF:
        def __init__(self, **kw): pass

    monkeypatch.setattr(_browser_mod, "browser_available", lambda engine: True)
    monkeypatch.setattr(_browser_mod, "BrowserFetcher", _FakeBF)
    cfg = ScrapeConfig(browser_enabled=True)
    comps = await build_scrape_components(cfg)
    assert isinstance(comps.browser, _FakeBF)
    await comps.http.close()


# ─── app.py ───────────────────────────────────────────────────────────────────

fastapi = pytest.importorskip("fastapi")


def test_create_app_with_explicit_scorer():
    """Passing scorer= covers the 'scorer is not None' branch (line 78)."""
    from fastapi.testclient import TestClient

    from ujin.scrape.app import create_scrape_app
    from ujin.scrape.scoring import NullScorer

    app = create_scrape_app(ScrapeConfig(), scorer=NullScorer())
    with TestClient(app):
        pass


def test_create_app_with_nitter_pool(monkeypatch):
    """nitter_pool_path set → NitterPool.from_yaml wired (lines 66-69)."""
    import ujin.sources.social as _social

    from fastapi.testclient import TestClient

    from ujin.scrape.app import create_scrape_app

    class _FakePool:
        mirrors = [object()]

    class _FakeNitterPool:
        @classmethod
        def from_yaml(cls, path):
            return _FakePool()

    monkeypatch.setattr(_social, "NitterPool", _FakeNitterPool)
    cfg = ScrapeConfig(nitter_pool_path="/fake/pool.yaml")
    app = create_scrape_app(cfg)
    with TestClient(app):
        pass


def test_create_app_breaking_scorer(monkeypatch):
    """enable_breaking_scorer wires BreakingScorer + trends task (lines 80-108, 155-159)."""
    import ujin.sources.social as _social

    from fastapi.testclient import TestClient

    from ujin.scrape.app import create_scrape_app

    async def _fake_trends(region, limit):
        raise RuntimeError("no network in tests")

    monkeypatch.setattr(_social, "fetch_x_trends", _fake_trends)
    cfg = ScrapeConfig(enable_breaking_scorer=True)
    app = create_scrape_app(cfg)
    with TestClient(app):
        pass


def test_serve_calls_uvicorn(monkeypatch):
    """serve() calls uvicorn.run (lines 178-180)."""
    uvicorn = pytest.importorskip("uvicorn")
    from ujin.scrape.app import serve

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, host, port: calls.append((host, port)))
    serve(host="127.0.0.1", port=9999, config=ScrapeConfig())
    assert calls == [("127.0.0.1", 9999)]


# ─── service.py ───────────────────────────────────────────────────────────────

async def test_auto_mode_html_succeeds_no_altpath():
    """mode=auto with successful HTTP covers should_try_alt=False (line 204)."""
    http = FakeHttp({HOME: _resp(HOME, _index_html())})
    svc = _service(http)
    r = await svc.scrape(HOME, mode="auto")
    assert r.kind == "links"


async def test_article_mode_with_article_profile():
    """Override with ArticleProfile → apply_article_profile called (lines 259, 262->264)."""
    url = f"{HOME}article/custom"
    art_html = (
        "<html><body>"
        "<div class='article-body'>"
        "<p>This is the main body of the article with sufficient content for extraction.</p>"
        "<p>Another paragraph that provides additional context about important events.</p>"
        "</div>"
        "</body></html>"
    )
    profile = ExtractProfile(article=ArticleProfile(body="div.article-body"))
    reg = HostOverrideRegistry({"news.example.com": HostOverride(extract=profile)})
    http = FakeHttp({url: _resp(url, art_html)})
    svc = _service(http, overrides=reg)
    r = await svc.scrape(url, mode="article")
    assert r.kind == "article"
    assert r.article is not None


async def test_cooldown_serves_cached_article():
    """mode=article on cooled-down host with cache covers line 844."""
    url = f"{HOME}article/1"
    cache = ScrapeCache()
    cache.put(
        f"article:{url}",
        CachedEntry(
            url=url, fingerprint="fp", payload={"article": None},
            fetched_at=time.monotonic(),
        ),
    )

    class _AlwaysCool(HostPolicy):
        def cooldown_remaining(self, url):
            return 999.0

    svc = _service(FakeHttp({}), cache=cache, policy=_AlwaysCool())
    r = await svc.scrape(url, mode="article")
    assert r.cached is True
    assert r.strategy_used == "cache"


async def test_walk_altpath_sitemap_and_rss_exceptions(monkeypatch):
    """sitemap raises AND rss raises in _walk_altpath_chain (lines 722-723, 728-733)."""
    import ujin.scrape.service as _svc_mod

    async def _sitemap_boom(*a): raise RuntimeError("sitemap boom")
    async def _rss_boom(*a): raise RuntimeError("rss boom")

    monkeypatch.setattr(_svc_mod, "try_sitemap_news", _sitemap_boom)
    monkeypatch.setattr(_svc_mod, "try_rss_fallback", _rss_boom)

    reg = HostOverrideRegistry(
        {"news.example.com": HostOverride(rss_url="https://news.example.com/feed.xml")}
    )
    http = FakeHttp({HOME: _resp(HOME, "<html><a href='/one'>link</a></html>")})
    svc = _service(http, obscura=FakeObscura(html=None), overrides=reg)

    with pytest.raises(RuntimeError, match="fetch failed for"):
        await svc.scrape(HOME, mode="links")


async def test_direct_sitemap_pinned_exception_autodiscovery_wins(monkeypatch):
    """Pinned sitemap raises → auto-discovery returns links (lines 759-766)."""
    import ujin.scrape.service as _svc_mod
    from ujin.extract.links import NormalizedLink
    from ujin.fetch.altpath import AltPathResult

    sm_url = "https://news.example.com/custom-sitemap.xml"

    async def _auto_sitemap(http, url):
        return AltPathResult(
            strategy="sitemap_news",
            links=[NormalizedLink(url="https://news.example.com/2026/auto", text="Auto discovered")],
        )

    monkeypatch.setattr(_svc_mod, "try_sitemap_news", _auto_sitemap)

    reg = HostOverrideRegistry(
        {"news.example.com": HostOverride(strategy="sitemap_news", sitemap_url=sm_url)}
    )
    http = FakeHttp({sm_url: RuntimeError("timeout")})
    svc = _service(http, overrides=reg)

    r = await svc.scrape(HOME, mode="links")
    assert r.strategy_used == "sitemap_news"
    assert len(r.links) == 1


async def test_direct_sitemap_pinned_exception_autodiscovery_none(monkeypatch):
    """Pinned raises + auto returns None → _direct_sitemap returns None → 149->153."""
    import ujin.scrape.service as _svc_mod

    sm_url = "https://news.example.com/custom-sitemap.xml"

    async def _auto_none(http, url): return None

    monkeypatch.setattr(_svc_mod, "try_sitemap_news", _auto_none)

    reg = HostOverrideRegistry(
        {"news.example.com": HostOverride(strategy="sitemap_news", sitemap_url=sm_url)}
    )
    http = FakeHttp({sm_url: RuntimeError("timeout")})
    svc = _service(http, obscura=FakeObscura(html=_index_html()))
    r = await svc.scrape(HOME, mode="links")
    assert r.strategy_used == "obscura"


async def test_rss_override_alt_none_falls_through(monkeypatch):
    """rss override + parse_feed returns [] → _direct_rss returns None → 155->162."""
    import ujin.scrape.service as _svc_mod

    async def _empty_parse(url): return []
    monkeypatch.setattr(_svc_mod, "parse_feed", _empty_parse)

    reg = HostOverrideRegistry(
        {
            "news.example.com": HostOverride(
                strategy="rss", rss_url="https://news.example.com/feed.xml"
            )
        }
    )
    http = FakeHttp({HOME: _resp(HOME, _index_html())})
    svc = _service(http, overrides=reg)
    r = await svc.scrape(HOME, mode="links")
    assert r.strategy_used == "http"


async def test_sitemap_override_skipped_for_article_mode():
    """sitemap_news override + mode=article skips sitemap (branch on line 147)."""
    url = f"{HOME}article/1"
    art_html = "<html><body><article><p>Long enough article content for testing purposes.</p></article></body></html>"
    reg = HostOverrideRegistry(
        {"news.example.com": HostOverride(strategy="sitemap_news")}
    )
    http = FakeHttp({url: _resp(url, art_html)})
    svc = _service(http, overrides=reg)
    r = await svc.scrape(url, mode="article")
    assert r.strategy_used == "http"


async def test_http_200_empty_body_falls_through_to_obscura():
    """status=200 but empty body → neither if/elif → tries obscura (690->699)."""
    http = FakeHttp({HOME: _resp(HOME, "", status=200)})
    svc = _service(http, obscura=FakeObscura(html=_index_html()))
    r = await svc.scrape(HOME, mode="links")
    assert r.strategy_used == "obscura"


async def test_http_fetch_exception_tries_obscura():
    """HTTP get raises → exception handler fires → tries obscura (lines 694-695 area)."""
    http = FakeHttp({HOME: RuntimeError("connection refused")})
    svc = _service(http, obscura=FakeObscura(html=_index_html()))
    r = await svc.scrape(HOME, mode="links")
    assert r.strategy_used == "obscura"


# ─── _filter_sitemap_entries branches ─────────────────────────────────────────

class _SE:
    def __init__(self, url, title=""):
        self.url = url
        self.title = title


def _svc_with_profile(profile):
    reg = HostOverrideRegistry({"news.example.com": HostOverride(extract=profile)})
    return ScrapeService(
        http=FakeHttp({}),
        obscura=FakeObscura(),
        cache=ScrapeCache(),
        policy=HostPolicy(),
        overrides=reg,
    )


def test_filter_sitemap_url_must_match_filters_non_matching():
    """url_path_must_match regex compiles and drops non-matching paths (786-789, 799)."""
    profile = ExtractProfile(url_path_must_match=r"^/article/")
    svc = _svc_with_profile(profile)
    entries = [
        _SE("https://news.example.com/article/story-1"),
        _SE("https://news.example.com/sports/game"),
    ]
    links = svc._filter_sitemap_entries("https://news.example.com/", entries)
    assert len(links) == 1
    assert "article" in links[0].url


def test_filter_sitemap_invalid_regex_passes_all():
    """Bad regex in url_path_must_match is silently ignored (lines 788-789)."""
    profile = ExtractProfile(url_path_must_match=r"[invalid")
    svc = _svc_with_profile(profile)
    entries = [_SE("https://news.example.com/story/1")]
    links = svc._filter_sitemap_entries("https://news.example.com/", entries)
    assert len(links) == 1


def test_filter_sitemap_duplicate_url_deduped():
    """Duplicate canonical URLs are skipped (line 796)."""
    svc = ScrapeService(
        http=FakeHttp({}), obscura=FakeObscura(),
        cache=ScrapeCache(), policy=HostPolicy(),
    )
    dup = "https://news.example.com/2026/06/story"
    entries = [_SE(dup), _SE(dup)]
    links = svc._filter_sitemap_entries("https://news.example.com/", entries)
    assert len(links) == 1


def test_filter_sitemap_title_deny_pattern():
    """title_deny_patterns drops entries with matching title (line 804)."""
    profile = ExtractProfile(title_deny_patterns=("sports", "lottery"))
    svc = _svc_with_profile(profile)
    entries = [
        _SE("https://news.example.com/politics/story", "Breaking political news"),
        _SE("https://news.example.com/games/123", "Sports: Championship Final"),
    ]
    links = svc._filter_sitemap_entries("https://news.example.com/", entries)
    assert len(links) == 1
    assert "politics" in links[0].url


def test_filter_sitemap_entry_limit_300():
    """Results capped at 300 (line 808 break)."""
    svc = ScrapeService(
        http=FakeHttp({}), obscura=FakeObscura(),
        cache=ScrapeCache(), policy=HostPolicy(),
    )
    entries = [_SE(f"https://news.example.com/{i}") for i in range(400)]
    links = svc._filter_sitemap_entries("https://news.example.com/", entries)
    assert len(links) == 300


# ─── _filter_named_links branches ──────────────────────────────────────────────

def test_filter_named_links_path_deny():
    """url_path_deny_patterns drops matching link (line 829)."""
    from ujin.extract.links import NormalizedLink

    profile = ExtractProfile(url_path_deny_patterns=("/sports/",))
    reg = HostOverrideRegistry({"news.example.com": HostOverride(extract=profile)})
    svc = ScrapeService(
        http=FakeHttp({}), obscura=FakeObscura(),
        cache=ScrapeCache(), policy=HostPolicy(), overrides=reg,
    )
    links = [
        NormalizedLink(url="https://news.example.com/politics/story", text="Politics"),
        NormalizedLink(url="https://news.example.com/sports/game", text="Sports"),
    ]
    result = svc._filter_named_links("https://news.example.com/", links)
    assert len(result) == 1
    assert "politics" in result[0].url


def test_filter_named_links_title_deny():
    """title_deny_patterns drops matching link text (line 831)."""
    from ujin.extract.links import NormalizedLink

    profile = ExtractProfile(title_deny_patterns=("sponsored",))
    reg = HostOverrideRegistry({"news.example.com": HostOverride(extract=profile)})
    svc = ScrapeService(
        http=FakeHttp({}), obscura=FakeObscura(),
        cache=ScrapeCache(), policy=HostPolicy(), overrides=reg,
    )
    links = [
        NormalizedLink(url="https://news.example.com/real", text="Real news story"),
        NormalizedLink(url="https://news.example.com/promo", text="Sponsored: buy now"),
    ]
    result = svc._filter_named_links("https://news.example.com/", links)
    assert len(result) == 1
    assert result[0].text == "Real news story"


# ─── _extract_with_profile thin profile ────────────────────────────────────────

async def test_extract_with_profile_thin_supplements_with_generic():
    """Profile finds < 5 links → generic extractor supplements (lines 534-543)."""
    html = (
        "<html><body>"
        "<div class='pinned'><a href='https://news.example.com/pinned-1'>Pinned story</a></div>"
        + "".join(
            f'<article><h2><a href="https://news.example.com/2026/06/{i}">'
            f"Generic headline {i} is long enough to not be filtered out"
            "</a></h2></article>"
            for i in range(10)
        )
        + "</body></html>"
    )
    profile = ExtractProfile(link_selectors=("div.pinned a",))
    reg = HostOverrideRegistry({"news.example.com": HostOverride(extract=profile)})
    http = FakeHttp({HOME: _resp(HOME, html)})
    svc = _service(http, overrides=reg)
    r = await svc.scrape(HOME, mode="links")
    assert len(r.links) > 1


# ─── combined mode RSS error / filter ──────────────────────────────────────────

async def test_combined_rss_task_exception_in_note(monkeypatch):
    """RSS task raises → rss_err logged in note (lines 361-362, 422)."""
    import ujin.scrape.service as _svc_mod
    import ujin.sources.rss as _rss_mod

    async def _boom(url): raise RuntimeError("rss network failure")

    monkeypatch.setattr(_rss_mod, "parse_feed", _boom)
    monkeypatch.setattr(_svc_mod, "parse_feed", _boom)

    reg = HostOverrideRegistry(
        {"news.example.com": HostOverride(rss_url="https://news.example.com/feed.xml")}
    )
    http = FakeHttp({HOME: _resp(HOME, _index_html(n=6))})
    svc = _service(http, overrides=reg)
    r = await svc.scrape(HOME, mode="combined")
    assert r.strategy_used == "combined"
    assert "rss_err=RuntimeError" in (r.note or "")


async def test_combined_rss_slop_and_boilerplate_filtered(monkeypatch):
    """RSS items with slop URL and boilerplate title are filtered out (lines 374, 378)."""
    import ujin.scrape.service as _svc_mod
    import ujin.sources.rss as _rss_mod

    class _Item:
        def __init__(self, url, title):
            self.url, self.title, self.summary, self.published = url, title, "", ""

    async def _fake_parse(url):
        return [
            _Item("https://news.example.com/recipes/cake", "Best recipes today"),
            _Item("https://news.example.com/2026/06/real-story", "Advertisement"),
            _Item("https://news.example.com/2026/06/good", "A genuinely good headline story"),
        ]

    monkeypatch.setattr(_rss_mod, "parse_feed", _fake_parse)
    monkeypatch.setattr(_svc_mod, "parse_feed", _fake_parse)

    reg = HostOverrideRegistry(
        {"news.example.com": HostOverride(rss_url="https://news.example.com/feed.xml")}
    )
    http = FakeHttp({HOME: _resp(HOME, "<html></html>")})
    svc = _service(http, overrides=reg)
    r = await svc.scrape(HOME, mode="combined")
    assert r.strategy_used == "combined"
    rss_urls = [l.url for l in r.links if "rss" in l.seen_in]
    assert not any("recipes" in u for u in rss_urls)
    assert not any("real-story" in u for u in rss_urls)


# ─── _enrich_html_only_links branches ──────────────────────────────────────────

async def test_enrich_early_return_when_no_html_only_links(monkeypatch):
    """All links have RSS summary → html_only list empty → early return (line 467)."""
    import ujin.scrape.service as _svc_mod
    import ujin.sources.rss as _rss_mod

    story_url = "https://news.example.com/2026/06/09/story-0"

    class _Item:
        url = story_url
        title = "A long headline that is definitely not filtered out at all"
        summary = "RSS summary already present and long enough to use directly"
        published = ""

    async def _fake_parse(url): return [_Item()]

    monkeypatch.setattr(_rss_mod, "parse_feed", _fake_parse)
    monkeypatch.setattr(_svc_mod, "parse_feed", _fake_parse)

    reg = HostOverrideRegistry(
        {"news.example.com": HostOverride(rss_url="https://news.example.com/feed.xml")}
    )
    html = (
        "<html><body><main><article><h2>"
        f'<a href="{story_url}">A long headline that is definitely not filtered out at all</a>'
        "</h2></article></main></body></html>"
    )
    http = FakeHttp({HOME: _resp(HOME, html)})
    svc = _service(http, overrides=reg)
    r = await svc.scrape(HOME, mode="combined", enrich_html_top_n=5)
    assert r.strategy_used == "combined"


async def test_enrich_exception_returns_none(monkeypatch):
    """Article scrape raises → _enrich catches and returns None (lines 472-473, 489)."""
    art_url = f"{HOME}2026/06/09/bad-article"
    html = (
        "<html><body><main><article><h2>"
        f'<a href="{art_url}">A sufficiently long headline for the bad article</a>'
        "</h2></article></main></body></html>"
    )
    http = FakeHttp({HOME: _resp(HOME, html)})
    svc = _service(http, obscura=FakeObscura(html=None))
    r = await svc.scrape(HOME, mode="combined", enrich_html_top_n=1)
    assert r.strategy_used == "combined"


async def test_enrich_article_empty_skips_summary():
    """Article has no extractable content → art_result.article is None (line 475)."""
    art_url = f"{HOME}2026/06/09/empty-article"
    html = (
        "<html><body><main><article><h2>"
        f'<a href="{art_url}">A long headline for the empty article here</a>'
        "</h2></article></main></body></html>"
    )
    empty_html = "<html><body></body></html>"
    http = FakeHttp({HOME: _resp(HOME, html), art_url: _resp(art_url, empty_html)})
    svc = _service(http, obscura=FakeObscura(html=None))
    r = await svc.scrape(HOME, mode="combined", enrich_html_top_n=1)
    assert r.strategy_used == "combined"
    assert not any("article" in l.seen_in for l in r.links)


async def test_enrich_short_paragraphs_no_summary():
    """All paragraphs < 60 chars → no summary attached (lines 479->477, 481)."""
    art_url = f"{HOME}2026/06/09/short-article"
    html = (
        "<html><body><main><article><h2>"
        f'<a href="{art_url}">A sufficiently long headline for short paragraph article</a>'
        "</h2></article></main></body></html>"
    )
    short_art_html = (
        "<html><body><article>"
        "<p>Too short.</p>"
        "<p>Also short.</p>"
        "</article></body></html>"
    )
    http = FakeHttp({HOME: _resp(HOME, html), art_url: _resp(art_url, short_art_html)})
    svc = _service(http, obscura=FakeObscura(html=None))
    r = await svc.scrape(HOME, mode="combined", enrich_html_top_n=1)
    assert r.strategy_used == "combined"


async def test_enrich_mixed_results_continue_on_no_summary(html_corpus):
    """Some links enriched, some not → continue at line 489 is hit."""
    good_art = f"{HOME}2026/06/08/real-story"
    bad_art = f"{HOME}2026/06/09/empty-article"
    # 4 filler links + good + bad = 6 links, so HTTP fast-path succeeds (>=5)
    filler = "".join(
        f'<article><h2><a href="{HOME}filler/{i}">Filler headline {i} long enough here</a></h2></article>'
        for i in range(4)
    )
    html = (
        "<html><body><main>"
        + filler
        + f'<article><h2><a href="{good_art}">Real story with good content here</a></h2></article>'
        + f'<article><h2><a href="{bad_art}">Empty article with no extractable body</a></h2></article>'
        + "</main></body></html>"
    )
    http = FakeHttp(
        {
            HOME: _resp(HOME, html),
            good_art: _resp(good_art, html_corpus["article"]),
            bad_art: _resp(bad_art, "<html><body></body></html>"),
        }
    )
    svc = _service(http, obscura=FakeObscura(html=None))
    r = await svc.scrape(HOME, mode="combined", enrich_html_top_n=5)
    assert r.strategy_used == "combined"
    assert len(r.links) >= 2
