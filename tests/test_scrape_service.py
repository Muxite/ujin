"""ScrapeService orchestration tests.

These inject fake HttpFetcher/ObscuraFetcher (duck-typed) so we exercise the
fallback chain, cooldown, 304 revalidation, and NullScorer neutrality without
relying on live network or the HTML-extractor heuristics.
"""
from __future__ import annotations

import pytest

from ujin.cache import CachedEntry, HostPolicy, ScrapeCache
from ujin.fetch.http import HttpResponse
from ujin.scrape.config import ScrapeConfig
from ujin.scrape.service import HostCooldown, ScrapeService

pytestmark = pytest.mark.asyncio


_SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://news.example.com/2026/06/01/story-one</loc></url>
  <url><loc>https://news.example.com/2026/06/01/story-two</loc></url>
  <url><loc>https://news.example.com/2026/06/01/story-three</loc></url>
</urlset>
"""


class FakeHttp:
    """Routes GETs by URL. `.get` mimics HttpFetcher.get."""

    def __init__(self, routes: dict[str, HttpResponse], *, not_modified: bool = False):
        self._routes = routes
        self._not_modified = not_modified
        self.calls: list[str] = []

    async def get(self, url, *, etag=None, last_modified=None, extra_headers=None):
        self.calls.append(url)
        if self._not_modified and etag is not None:
            return HttpResponse(url=url, status=304, body="", not_modified=True,
                                final_url=url)
        resp = self._routes.get(url)
        if resp is None:
            return HttpResponse(url=url, status=404, body="", final_url=url)
        return resp


class FakeObscura:
    def __init__(self, html: str | None = None):
        self._html = html
        self.calls: list[str] = []

    async def render_html(self, url):
        self.calls.append(url)
        if self._html is None:
            raise RuntimeError("obscura unavailable")
        from ujin.fetch.obscura import ObscuraResult

        return ObscuraResult(url=url, html=self._html, elapsed_ms=1)


def _service(http, *, obscura=None, cache=None, policy=None, config=None, browser=None):
    return ScrapeService(
        http=http,
        obscura=obscura or FakeObscura(),
        cache=cache or ScrapeCache(),
        policy=policy or HostPolicy(cooldown_secs=60),
        config=config or ScrapeConfig(),
        browser=browser,
    )


class FakeBrowser:
    """Duck-typed BrowserFetcher: returns canned HTML after 'running' a recipe."""

    def __init__(self, html: str):
        from ujin.fetch.browser import BrowserResult

        self._result = BrowserResult(url="", html=html, elapsed_ms=2)
        self.calls: list[tuple[str, list]] = []

    async def render(self, url, actions=None, *, results_selector=None, ctx=None):
        self.calls.append((url, actions or []))
        self._result.url = url
        self._result.final_url = url
        return self._result


async def test_render_browser_runs_recipe_then_extracts():
    # JSON-LD in the browser-rendered HTML proves the extractor ran on it.
    html = (
        '<html><head><script type="application/ld+json">'
        '{"@type":"Person","name":"S. Fels"}'
        "</script></head><body>loaded</body></html>"
    )
    browser = FakeBrowser(html)
    svc = _service(FakeHttp({}), browser=browser)
    result = await svc.scrape(
        "https://news.example.com/", mode="structured", render="browser",
        actions=[{"action": "load_more", "button": ".m", "results": ".a"}],
    )
    assert browser.calls and browser.calls[0][1][0]["action"] == "load_more"
    assert result.strategy_used == "browser"
    assert result.used_renderer is True
    # the extractor parsed the browser-rendered JSON-LD
    assert result.structured["jsonld"][0]["name"] == "S. Fels"


async def test_http_to_sitemap_altpath_fallback():
    """Homepage HTTP yields too few links → altpath sitemap-news wins."""
    home = "https://news.example.com/"
    routes = {
        # Homepage returns 200 but empty body -> 0 links < fast_path_min_links.
        home: HttpResponse(url=home, status=200, body="<html></html>", final_url=home),
        "https://news.example.com/news-sitemap.xml": HttpResponse(
            url="x", status=200, body=_SITEMAP_XML, final_url="x"
        ),
    }
    http = FakeHttp(routes)
    svc = _service(http)
    result = await svc.scrape(home, mode="links")
    assert result.kind == "links"
    assert result.strategy_used == "sitemap_news"
    assert len(result.links) == 3
    # NullScorer neutrality.
    for link in result.links:
        assert getattr(link, "_tier") == "generic"
        assert getattr(link, "_breaking_score") == 0.0
        assert getattr(link, "_score_components") == {}
    assert result.next_poll_hint_secs == 60.0


async def test_unchanged_doubles_hint_and_marks_cached():
    home = "https://news.example.com/"
    routes = {
        home: HttpResponse(url=home, status=200, body="<html></html>", final_url=home),
        "https://news.example.com/news-sitemap.xml": HttpResponse(
            url="x", status=200, body=_SITEMAP_XML, final_url="x"
        ),
    }
    svc = _service(FakeHttp(routes))
    first = await svc.scrape(home, mode="links")
    assert first.cached is False
    second = await svc.scrape(home, mode="links")
    assert second.cached is True
    assert second.note == "content unchanged"
    assert second.fingerprint == first.fingerprint
    # NullScorer doubles the base hint when unchanged (60 -> 120).
    assert second.next_poll_hint_secs == 120.0


async def test_host_cooldown_without_cache_raises():
    home = "https://down.example.com/"
    policy = HostPolicy(cooldown_secs=60)
    policy.record_failure(home)  # puts host on cooldown
    svc = _service(FakeHttp({}), policy=policy)
    with pytest.raises(HostCooldown):
        await svc.scrape(home, mode="links")


async def test_host_cooldown_serves_cache_when_present():
    home = "https://down.example.com/"
    cache = ScrapeCache()
    cache.put(
        f"links:{home}",
        CachedEntry(url=home, fingerprint="fp", payload={"links": []},
                    fetched_at=__import__("time").monotonic()),
    )
    policy = HostPolicy(cooldown_secs=60)
    policy.record_failure(home)
    svc = _service(FakeHttp({}), cache=cache, policy=policy)
    result = await svc.scrape(home, mode="links")
    assert result.cached is True
    assert result.strategy_used == "cache"
    assert "cooldown" in (result.note or "")


async def test_304_revalidation_serves_cache():
    home = "https://news.example.com/"
    cache = ScrapeCache()
    cache.put(
        f"links:{home}",
        CachedEntry(url=home, fingerprint="fp", payload={"links": []},
                    fetched_at=__import__("time").monotonic(), etag='"abc"'),
    )
    # Fake http returns 304 whenever an etag is sent.
    http = FakeHttp({home: HttpResponse(url=home, status=200, body="x", final_url=home)},
                    not_modified=True)
    svc = _service(http, cache=cache)
    result = await svc.scrape(home, mode="links")
    assert result.cached is True
    assert result.note == "304 Not Modified"


async def test_obscura_fallback_when_http_4xx():
    """HTTP 403 escalates to the obscura renderer; its HTML flows to extraction."""
    home = "https://spa.example.com/"
    obscura = FakeObscura(html="<html><body><main></main></body></html>")
    routes = {home: HttpResponse(url=home, status=403, body="", final_url=home)}
    svc = _service(FakeHttp(routes), obscura=obscura)
    result = await svc.scrape(home, mode="links")
    # Renderer was attempted and its strategy flows back; empty <main> yields 0 links.
    assert obscura.calls == [home]
    assert result.used_renderer is True
    assert result.strategy_used == "obscura"
    assert result.links == []


async def test_structured_mode_extracts_and_survives_304():
    """mode=structured returns the structured dict, and a 304 revalidation
    serves it back as kind=structured (not mis-rendered as links)."""
    home = "https://site.example.com/"
    html = ('<html><head><meta property="og:title" content="Hello">'
            '</head><body></body></html>')
    cache = ScrapeCache()
    # First fetch: real extraction.
    http = FakeHttp({home: HttpResponse(url=home, status=200, body=html,
                                        etag='"v1"', final_url=home)})
    svc = _service(http, cache=cache)
    r1 = await svc.scrape(home, mode="structured")
    assert r1.kind == "structured"
    assert r1.structured["opengraph"]["og:title"] == "Hello"

    # Second fetch: server returns 304 → served from cache, still structured.
    http304 = FakeHttp({home: HttpResponse(url=home, status=200, body=html,
                                           final_url=home)}, not_modified=True)
    svc2 = ScrapeService(http=http304, obscura=FakeObscura(), cache=cache,
                         policy=HostPolicy(cooldown_secs=60), config=ScrapeConfig())
    r2 = await svc2.scrape(home, mode="structured")
    assert r2.cached is True
    assert r2.kind == "structured"
    assert r2.structured["opengraph"]["og:title"] == "Hello"


async def test_fetch_failure_raises_when_nothing_extractable():
    """HTTP 403 + obscura unavailable + no sitemap → fetch failure raises."""
    home = "https://dead.example.com/"
    routes = {home: HttpResponse(url=home, status=403, body="", final_url=home)}
    svc = _service(FakeHttp(routes), obscura=FakeObscura(html=None))
    with pytest.raises(RuntimeError):
        await svc.scrape(home, mode="links")
