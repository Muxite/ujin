"""Feed discovery extraction — the `extract_feeds` parser plus the `feeds`
scrape mode (single-`mode` and multi-extract `extracts`).

Offline and deterministic: the parser runs over a corpus fixture
(`tests/fixtures/html/feeds.html`) and inline snippets; the service paths
reuse the duck-typed fakes from test_scrape_service.py.
"""
from __future__ import annotations

import pytest

from ujin.extract import extract_feeds
from ujin.fetch.http import HttpResponse

from test_scrape_service import FakeHttp, FakeObscura, _service

_HOME = "https://feeds.example.com/"


# ── extract_feeds: the parser ─────────────────────────────────────────────────

def test_acceptance_empty_and_whitespace_return_empty_list():
    assert extract_feeds("") == []
    assert extract_feeds("   ") == []
    assert extract_feeds(None) == []  # type: ignore[arg-type]


def test_rss_link_is_discovered():
    html = (
        "<html><head>"
        '<link rel="alternate" type="application/rss+xml" href="/feed.rss">'
        "</head></html>"
    )
    out = extract_feeds(html)
    assert len(out) == 1
    assert out[0]["href"] == "/feed.rss"
    assert out[0]["type"] == "application/rss+xml"
    assert "title" not in out[0]


def test_atom_link_is_discovered():
    html = (
        "<html><head>"
        '<link rel="alternate" type="application/atom+xml" href="/atom.xml">'
        "</head></html>"
    )
    out = extract_feeds(html)
    assert out == [{"href": "/atom.xml", "type": "application/atom+xml"}]


def test_json_feed_link_is_discovered():
    html = (
        "<html><head>"
        '<link rel="alternate" type="application/feed+json" href="/feed.json">'
        "</head></html>"
    )
    out = extract_feeds(html)
    assert out == [{"href": "/feed.json", "type": "application/feed+json"}]


def test_title_included_when_present_and_non_blank():
    html = (
        "<html><head>"
        '<link rel="alternate" type="application/rss+xml" title="My Feed" href="/f.rss">'
        "</head></html>"
    )
    out = extract_feeds(html)
    assert out[0]["title"] == "My Feed"


def test_blank_title_not_included():
    html = (
        "<html><head>"
        '<link rel="alternate" type="application/rss+xml" title="  " href="/f.rss">'
        "</head></html>"
    )
    out = extract_feeds(html)
    assert "title" not in out[0]


def test_relative_href_resolved_against_base_url():
    html = (
        "<html><head>"
        '<link rel="alternate" type="application/rss+xml" href="/feed.rss">'
        "</head></html>"
    )
    out = extract_feeds(html, base_url="https://x.test/dir/page")
    assert out[0]["href"] == "https://x.test/feed.rss"


def test_relative_href_without_base_url_kept_as_is():
    html = '<link rel="alternate" type="application/rss+xml" href="/feed.rss">'
    out = extract_feeds(html)
    assert out[0]["href"] == "/feed.rss"


def test_absolute_href_unchanged_regardless_of_base_url():
    html = (
        "<html><head>"
        '<link rel="alternate" type="application/rss+xml" href="https://x.test/f.rss">'
        "</head></html>"
    )
    out = extract_feeds(html, base_url="https://other.test/")
    assert out[0]["href"] == "https://x.test/f.rss"


def test_multiple_feed_types_all_returned_in_document_order():
    html = (
        "<html><head>"
        '<link rel="alternate" type="application/rss+xml" href="/a.rss">'
        '<link rel="alternate" type="application/atom+xml" href="/a.atom">'
        '<link rel="alternate" type="application/feed+json" href="/a.json">'
        "</head></html>"
    )
    out = extract_feeds(html)
    assert [e["href"] for e in out] == ["/a.rss", "/a.atom", "/a.json"]
    assert [e["type"] for e in out] == [
        "application/rss+xml", "application/atom+xml", "application/feed+json"
    ]


def test_identical_hrefs_deduped_first_occurrence_wins():
    html = (
        "<html><head>"
        '<link rel="alternate" type="application/rss+xml" title="First" href="/f.rss">'
        '<link rel="alternate" type="application/atom+xml" href="/f.atom">'
        '<link rel="alternate" type="application/rss+xml" title="Dup" href="/f.rss">'
        "</head></html>"
    )
    out = extract_feeds(html, base_url="https://x.test/")
    hrefs = [e["href"] for e in out]
    assert hrefs.count("https://x.test/f.rss") == 1
    # First occurrence wins
    rss = next(e for e in out if e["href"] == "https://x.test/f.rss")
    assert rss.get("title") == "First"


def test_non_feed_alternates_ignored():
    html = (
        "<html><head>"
        '<link rel="alternate" type="text/css" href="/print.css">'
        '<link rel="alternate" hreflang="fr" href="/fr/">'
        '<link rel="stylesheet" href="/styles.css">'
        '<link rel="canonical" href="https://x.test/">'
        "<title>No feeds here</title>"
        "</head></html>"
    )
    assert extract_feeds(html) == []


def test_link_in_body_ignored():
    html = (
        "<html><head></head><body>"
        '<link rel="alternate" type="application/rss+xml" href="/body.rss">'
        "</body></html>"
    )
    assert extract_feeds(html) == []


def test_case_insensitive_type_normalized_to_lowercase():
    html = (
        "<html><head>"
        '<link rel="alternate" type="Application/RSS+XML" href="/f.rss">'
        "</head></html>"
    )
    out = extract_feeds(html)
    assert out[0]["type"] == "application/rss+xml"


@pytest.mark.parametrize("bad", [
    "<html",
    "<<<>>>",
    "<html><head><link rel='alternate'",
    "<html><head></head></html>",  # no feeds
    "<html><head><link type='application/rss+xml' href='/f.rss'></head></html>",  # missing rel
])
def test_malformed_or_feed_free_never_raises(bad):
    out = extract_feeds(bad)
    assert isinstance(out, list)


def test_corpus_page_extracts_three_feeds_ignores_non_feed_links(html_corpus):
    out = extract_feeds(html_corpus["feeds"], base_url="https://news.example.com/")
    assert len(out) == 3
    hrefs = [e["href"] for e in out]
    assert "https://news.example.com/feed.rss" in hrefs
    assert "https://news.example.com/feed.atom" in hrefs
    assert "https://news.example.com/feed.json" in hrefs


def test_corpus_page_titles_present_for_named_feeds(html_corpus):
    out = extract_feeds(html_corpus["feeds"], base_url="https://news.example.com/")
    by_type = {e["type"]: e for e in out}
    assert by_type["application/rss+xml"]["title"] == "Example News RSS"
    assert by_type["application/atom+xml"]["title"] == "Example News Atom"
    assert by_type["application/feed+json"]["title"] == "Example News JSON Feed"


# ── scrape mode: service paths ────────────────────────────────────────────────

_FEEDS_HTML = (
    "<html><head>"
    '<link rel="alternate" type="application/rss+xml" title="Site RSS" href="/feed.rss">'
    '<link rel="alternate" type="application/atom+xml" href="/feed.atom">'
    "</head><body><p>body</p></body></html>"
)
_EXPECTED_FEEDS = [
    {"href": _HOME + "feed.rss", "type": "application/rss+xml", "title": "Site RSS"},
    {"href": _HOME + "feed.atom", "type": "application/atom+xml"},
]


def _feeds_service(**kwargs):
    routes = {_HOME: HttpResponse(url=_HOME, status=200, body=_FEEDS_HTML, final_url=_HOME)}
    return _service(FakeHttp(routes), **kwargs)


async def test_single_mode_feeds_returns_list_of_dicts():
    res = await _feeds_service().scrape(_HOME, mode="feeds")
    assert res.kind == "feeds"
    assert res.feeds == _EXPECTED_FEEDS
    assert res.fingerprint  # sha256 over the feeds list


async def test_single_mode_feeds_parity_with_multi_extract():
    single = await _feeds_service().scrape(_HOME, mode="feeds")
    multi = await _feeds_service().scrape_multi(_HOME, modes=["feeds"])
    assert single.kind == "feeds" == multi["feeds"].kind
    assert single.feeds == multi["feeds"].feeds
    assert single.fingerprint == multi["feeds"].fingerprint


async def test_multi_extract_returns_feeds_and_metadata():
    results = await _feeds_service().scrape_multi(_HOME, modes=["feeds", "metadata"])
    assert set(results) == {"feeds", "metadata"}
    assert results["feeds"].kind == "feeds"
    assert results["feeds"].feeds == _EXPECTED_FEEDS


async def test_single_mode_feeds_served_from_cache_on_cooldown():
    from ujin.cache import HostPolicy

    svc = _feeds_service(policy=HostPolicy(cooldown_secs=60))
    first = await svc.scrape(_HOME, mode="feeds")
    assert first.kind == "feeds"
    svc._policy.record_failure(_HOME)  # arm the cooldown
    cached = await svc.scrape(_HOME, mode="feeds")
    assert cached.cached is True
    assert cached.kind == "feeds"
    assert cached.feeds == first.feeds


# ── route-level dispatch ─────────────────────────────────────────────────────

def _feeds_app():
    from ujin.cache import HostPolicy, ScrapeCache
    from ujin.scrape.app import create_scrape_app
    from ujin.scrape.config import ScrapeConfig
    from ujin.scrape.service import ScrapeService

    app = create_scrape_app(ScrapeConfig())
    routes = {_HOME: HttpResponse(url=_HOME, status=200, body=_FEEDS_HTML, final_url=_HOME)}
    service = ScrapeService(
        http=FakeHttp(routes), obscura=FakeObscura(),
        cache=ScrapeCache(), policy=HostPolicy(cooldown_secs=60),
        config=ScrapeConfig(fast_path_min_links=1),
    )
    return app, service


def test_route_single_feeds_mode_returns_list_under_feeds_field():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    app, service = _feeds_app()
    client = TestClient(app)
    client.__enter__()
    try:
        app.state.service = service
        r = client.post("/scrape", json={"url": _HOME, "mode": "feeds"})
        assert r.status_code == 200
        body = r.json()
        assert body["kind"] == "feeds"
        assert body["feeds"] == _EXPECTED_FEEDS
    finally:
        client.__exit__(None, None, None)


def test_route_multi_extract_feeds_and_metadata_under_extracts():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    app, service = _feeds_app()
    client = TestClient(app)
    client.__enter__()
    try:
        app.state.service = service
        r = client.post("/scrape", json={"url": _HOME, "modes": ["feeds", "metadata"]})
        assert r.status_code == 200
        body = r.json()
        assert set(body["extracts"]) == {"feeds", "metadata"}
        assert body["extracts"]["feeds"]["kind"] == "feeds"
        assert body["extracts"]["feeds"]["feeds"] == _EXPECTED_FEEDS
    finally:
        client.__exit__(None, None, None)
