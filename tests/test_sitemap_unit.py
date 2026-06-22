"""Unit tests for sitemap parsing edge-cases and fetch_sitemap().

Covers the remaining gaps:
- <url> block that has no <loc> (line 45 — continue branch)
- sitemap-index fallback with bare <loc> tags (line 59)
- fetch_sitemap() success and error paths (lines 64-67)
"""
from __future__ import annotations

import pytest

from ujin.fetch.http import HttpResponse
from ujin.sources.sitemap import SitemapEntry, fetch_sitemap, parse_sitemap_xml


# --------------------------------------------------------------------------- #
# parse_sitemap_xml edge-cases
# --------------------------------------------------------------------------- #

def test_url_block_without_loc_is_skipped():
    """A <url> block missing <loc> is silently skipped; valid ones are kept."""
    xml = (
        "<urlset>"
        "<url><lastmod>2024-01-01</lastmod></url>"          # no <loc> → skipped
        "<url><loc>https://example.com/a</loc></url>"       # valid
        "</urlset>"
    )
    entries = parse_sitemap_xml(xml)
    assert len(entries) == 1
    assert entries[0].url == "https://example.com/a"


def test_sitemap_index_fallback():
    """When there are no <url> blocks, bare <loc> tags are returned as index entries."""
    xml = (
        "<sitemapindex>"
        "<sitemap><loc>https://example.com/sitemap1.xml</loc></sitemap>"
        "<sitemap><loc>https://example.com/sitemap2.xml</loc></sitemap>"
        "</sitemapindex>"
    )
    entries = parse_sitemap_xml(xml)
    assert len(entries) == 2
    assert entries[0].url == "https://example.com/sitemap1.xml"
    assert entries[1].url == "https://example.com/sitemap2.xml"
    # Index fallback returns no lastmod / title
    assert entries[0].lastmod is None
    assert entries[0].title is None


def test_sitemap_index_single_loc():
    """Single <loc> without <url> wrapper → one index entry."""
    xml = "<sitemapindex><loc>https://example.com/sub.xml</loc></sitemapindex>"
    entries = parse_sitemap_xml(xml)
    assert entries == [SitemapEntry(url="https://example.com/sub.xml")]


# --------------------------------------------------------------------------- #
# fetch_sitemap
# --------------------------------------------------------------------------- #

def _http_resp(url: str, status: int, body: str) -> "HttpResponse":
    return HttpResponse(url=url, status=status, body=body, final_url=url)


class _FakeHttp:
    """Minimal HttpFetcher stub that returns a single pre-loaded response."""

    def __init__(self, url: str, status: int, body: str):
        self._resp = _http_resp(url, status, body)

    async def get(self, url, **kw):
        return self._resp


_SITEMAP_BODY = (
    "<urlset>"
    "<url><loc>https://example.com/a</loc><lastmod>2024-06-01</lastmod></url>"
    "<url><loc>https://example.com/b</loc></url>"
    "</urlset>"
)


async def test_fetch_sitemap_success():
    """200 response with valid XML returns parsed entries."""
    url = "https://example.com/sitemap.xml"
    http = _FakeHttp(url, 200, _SITEMAP_BODY)
    entries = await fetch_sitemap(http, url)
    assert len(entries) == 2
    assert entries[0].url == "https://example.com/a"
    assert entries[0].lastmod == "2024-06-01"
    assert entries[1].lastmod is None


async def test_fetch_sitemap_non200_returns_empty():
    """Non-200 status returns empty list without raising."""
    url = "https://example.com/sitemap.xml"
    http = _FakeHttp(url, 404, "")
    entries = await fetch_sitemap(http, url)
    assert entries == []


async def test_fetch_sitemap_empty_body_returns_empty():
    """200 with empty body returns empty list."""
    url = "https://example.com/sitemap.xml"
    http = _FakeHttp(url, 200, "")
    entries = await fetch_sitemap(http, url)
    assert entries == []


async def test_fetch_sitemap_500_returns_empty():
    """500 response returns empty list."""
    url = "https://example.com/sitemap.xml"
    http = _FakeHttp(url, 500, "internal error")
    entries = await fetch_sitemap(http, url)
    assert entries == []
