"""Altpath fallback chain: sitemap-news probing and RSS fallback."""
from __future__ import annotations

import pytest

from conftest import FakeHttp
from ujin.fetch.altpath import (
    _is_article_url,
    try_rss_fallback,
    try_sitemap_news,
)
from ujin.fetch.http import HttpResponse


ORIGIN = "https://news.example.com"

SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://news.example.com/2026/06/09/a</loc></url>
  <url><loc>https://news.example.com/2026/06/09/b</loc></url>
  <url><loc>https://news.example.com/2026/06/09/a</loc></url>
</urlset>
"""

SITEMAP_INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://news.example.com/sitemap-1.xml</loc></url>
  <url><loc>https://news.example.com/sitemap-2.xml</loc></url>
</urlset>
"""


def _resp(url: str, body: str, status: int = 200) -> HttpResponse:
    return HttpResponse(url=url, status=status, body=body, final_url=url)


async def test_first_matching_path_wins_and_dedupes():
    url = f"{ORIGIN}/news-sitemap.xml"
    http = FakeHttp({url: _resp(url, SITEMAP)})
    result = await try_sitemap_news(http, f"{ORIGIN}/")
    assert result is not None
    assert result.strategy == "sitemap_news"
    # duplicate /a deduped
    assert len(result.links) == 2
    # probing stopped at the first hit
    assert http.calls == [url]


async def test_probes_paths_in_order_until_hit():
    last = f"{ORIGIN}/sitemap.xml"
    http = FakeHttp({last: _resp(last, SITEMAP)})
    result = await try_sitemap_news(http, f"{ORIGIN}/some/page")
    assert result is not None
    # earlier candidates were tried first
    assert http.calls[0] == f"{ORIGIN}/news-sitemap.xml"
    assert http.calls[-1] == last


async def test_non_xml_body_skipped():
    url = f"{ORIGIN}/news-sitemap.xml"
    http = FakeHttp({url: _resp(url, "<html>404 lol</html>")})
    assert await try_sitemap_news(http, f"{ORIGIN}/") is None


async def test_sitemap_index_entries_skipped():
    url = f"{ORIGIN}/news-sitemap.xml"
    http = FakeHttp({url: _resp(url, SITEMAP_INDEX)})
    # all entries point at .xml files -> not articles -> keep walking -> None
    assert await try_sitemap_news(http, f"{ORIGIN}/") is None


async def test_http_errors_during_probe_are_swallowed():
    url = f"{ORIGIN}/sitemap.xml"
    http = FakeHttp({
        f"{ORIGIN}/news-sitemap.xml": RuntimeError("conn reset"),
        url: _resp(url, SITEMAP),
    })
    result = await try_sitemap_news(http, f"{ORIGIN}/")
    assert result is not None and len(result.links) == 2


async def test_all_paths_404_returns_none():
    assert await try_sitemap_news(FakeHttp({}), f"{ORIGIN}/") is None


# ── rss fallback ─────────────────────────────────────────────────────────────

class _Item:
    def __init__(self, url, title=""):
        self.url = url
        self.title = title


async def test_rss_fallback_builds_links():
    async def feed(url):
        return [_Item("https://news.example.com/2026/06/09/a", "Story A"),
                _Item("https://news.example.com/2026/06/09/b", "Story B"),
                _Item("https://news.example.com/2026/06/09/a", "dup")]

    result = await try_rss_fallback("https://news.example.com/feed.xml", feed)
    assert result is not None
    assert result.strategy == "rss"
    assert len(result.links) == 2
    assert result.links[0].text == "Story A"


async def test_rss_fallback_none_url():
    assert await try_rss_fallback(None, None) is None


async def test_rss_fallback_parse_error_swallowed():
    async def feed(url):
        raise ValueError("bad xml")

    assert await try_rss_fallback("https://x.test/feed", feed) is None


async def test_rss_fallback_empty_feed():
    async def feed(url):
        return []

    assert await try_rss_fallback("https://x.test/feed", feed) is None


# ── article-url heuristic ────────────────────────────────────────────────────

@pytest.mark.parametrize("url,expected", [
    ("https://x.test/", False),
    ("https://x.test/world", False),
    ("https://x.test/2026/06/09/story", True),
    ("https://x.test/news/article-12345678901234", True),
    ("https://x.test/a/b", False),          # two short non-numeric segments
])
def test_is_article_url(url, expected):
    assert _is_article_url(url) is expected
