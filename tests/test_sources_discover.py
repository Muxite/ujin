"""discover_sources: <link rel=alternate>, robots.txt, well-known probing."""
from __future__ import annotations

from conftest import FakeHttp
from ujin.fetch.http import HttpResponse
from ujin.sources.discover import _from_html, _from_robots, discover_sources

HOME = "https://site.example.com/"
ORIGIN = "https://site.example.com"


def _resp(url, body, status=200):
    return HttpResponse(url=url, status=status, body=body, final_url=url)


HOME_HTML = """<html><head>
<link rel="alternate" type="application/rss+xml" href="/feed.xml">
<link rel="alternate" type="application/atom+xml" href="https://cdn.example.net/atom.xml">
<link rel="alternate" type="text/html" href="/mobile">
<link rel="alternate" type="application/rss+xml">
</head><body></body></html>"""

ROBOTS = """# robots
User-agent: *
Disallow: /admin
Sitemap: https://site.example.com/sitemap-main.xml
sitemap: /sitemap-news.xml
malformed line without colon? no - this has one
"""


async def test_discover_combines_all_three_sources():
    http = FakeHttp({
        HOME: _resp(HOME, HOME_HTML),
        f"{ORIGIN}/robots.txt": _resp("r", ROBOTS),
        f"{ORIGIN}/feed": _resp("f", "<rss/>"),
        f"{ORIGIN}/sitemap.xml": _resp("s", "<urlset/>"),
    })
    found = await discover_sources(http, HOME)
    assert found.homepage == HOME
    # html alternates (relative resolved, html-typed skipped, hrefless skipped)
    assert f"{ORIGIN}/feed.xml" in found.rss
    assert "https://cdn.example.net/atom.xml" in found.rss
    assert not any("/mobile" in u for u in found.rss)
    # robots directives (absolute + origin-relative)
    assert f"{ORIGIN}/sitemap-main.xml" in found.sitemap
    assert f"{ORIGIN}/sitemap-news.xml" in found.sitemap
    # well-known probes that answered 200
    assert f"{ORIGIN}/feed" in found.rss
    assert f"{ORIGIN}/sitemap.xml" in found.sitemap


async def test_discover_all_probes_fail_gracefully():
    found = await discover_sources(FakeHttp({}), HOME)
    assert found.rss == [] and found.sitemap == []


async def test_discover_homepage_error_continues():
    http = FakeHttp({
        HOME: RuntimeError("conn reset"),
        f"{ORIGIN}/rss": _resp("f", "<rss/>"),
    })
    found = await discover_sources(http, HOME)
    assert f"{ORIGIN}/rss" in found.rss


async def test_discover_empty_probe_bodies_rejected():
    http = FakeHttp({f"{ORIGIN}/feed": _resp("f", "   ")})
    found = await discover_sources(http, HOME)
    assert found.rss == []


def test_from_html_ignores_non_feed_alternates():
    assert _from_html("<link rel='alternate' type='text/html' href='/m'>",
                      base=HOME) == []


def test_from_robots_parsing():
    out = _from_robots(ROBOTS, ORIGIN)
    assert f"{ORIGIN}/sitemap-main.xml" in out
    assert f"{ORIGIN}/sitemap-news.xml" in out
    assert len(out) == 2  # comments/other directives ignored
