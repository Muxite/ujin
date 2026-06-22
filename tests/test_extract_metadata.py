"""Page metadata extraction — the `extract_metadata` parser plus the `metadata`
scrape mode (single-`mode` and multi-extract `extracts`).

Offline and deterministic: the parser runs over a corpus fixture
(`tests/fixtures/html/metadata.html`) and inline snippets; the service paths
reuse the duck-typed fakes from test_scrape_service.py.
"""
from __future__ import annotations

import pytest

from ujin.extract import extract_metadata
from ujin.fetch.http import HttpResponse

from test_scrape_service import FakeHttp, FakeObscura, _service

_HOME = "https://meta.example.com/"


# ── extract_metadata: the parser ─────────────────────────────────────────────

def test_acceptance_empty_and_whitespace_return_empty_dict():
    assert extract_metadata("") == {}
    assert extract_metadata("   ") == {}
    assert extract_metadata(None) == {}  # type: ignore[arg-type]


def test_title_description_and_canonical_present():
    html = (
        "<html><head><title>Hello World</title>"
        '<meta name="description" content="A short summary.">'
        '<link rel="canonical" href="https://x.test/p"></head></html>'
    )
    out = extract_metadata(html)
    assert out["title"] == "Hello World"
    assert out["description"] == "A short summary."
    assert out["canonical"] == "https://x.test/p"


def test_language_from_html_lang_attribute():
    out = extract_metadata('<html lang="fr"><head><title>Bonjour</title></head></html>')
    assert out["language"] == "fr"


def test_opengraph_and_twitter_collected_under_subdicts():
    html = (
        "<html><head>"
        '<meta property="og:title" content="OG Title">'
        '<meta property="og:type" content="article">'
        '<meta name="twitter:card" content="summary">'
        '<meta name="twitter:site" content="@acme">'
        "</head></html>"
    )
    out = extract_metadata(html)
    assert out["og"] == {"title": "OG Title", "type": "article"}
    assert out["twitter"] == {"card": "summary", "site": "@acme"}


def test_relative_canonical_favicon_and_og_image_resolved_against_base_url():
    html = (
        "<html><head>"
        '<link rel="canonical" href="/article/p">'
        '<link rel="icon" href="favicon.ico">'
        '<meta property="og:image" content="img/lead.jpg">'
        '<meta name="twitter:image" content="img/card.jpg">'
        "</head></html>"
    )
    out = extract_metadata(html, base_url="https://x.test/dir/page")
    assert out["canonical"] == "https://x.test/article/p"
    assert out["favicon"] == "https://x.test/dir/favicon.ico"
    assert out["og"]["image"] == "https://x.test/dir/img/lead.jpg"
    assert out["twitter"]["image"] == "https://x.test/dir/img/card.jpg"


def test_relative_urls_kept_as_is_without_base_url():
    html = '<html><head><link rel="canonical" href="/p"></head></html>'
    assert extract_metadata(html)["canonical"] == "/p"


def test_non_url_og_fields_are_not_resolved():
    html = '<html><head><meta property="og:title" content="/looks/like/a/path"></head></html>'
    out = extract_metadata(html, base_url="https://x.test/")
    assert out["og"]["title"] == "/looks/like/a/path"  # title is not a URL field


def test_author_published_modified_when_present():
    html = (
        "<html><head>"
        '<meta name="author" content="Jane Reporter">'
        '<meta property="article:published_time" content="2026-06-22T18:30:00Z">'
        '<meta property="article:modified_time" content="2026-06-22T20:05:00Z">'
        "</head></html>"
    )
    out = extract_metadata(html)
    assert out["author"] == "Jane Reporter"
    assert out["published"] == "2026-06-22T18:30:00Z"
    assert out["modified"] == "2026-06-22T20:05:00Z"


def test_title_and_description_fall_back_to_opengraph():
    html = (
        "<html><head>"
        '<meta property="og:title" content="Only OG Title">'
        '<meta property="og:description" content="Only OG description">'
        "</head></html>"
    )
    out = extract_metadata(html)
    assert out["title"] == "Only OG Title"
    assert out["description"] == "Only OG description"


def test_explicit_title_and_description_win_over_opengraph():
    html = (
        "<html><head><title>Real Title</title>"
        '<meta name="description" content="Real description">'
        '<meta property="og:title" content="OG Title">'
        '<meta property="og:description" content="OG description">'
        "</head></html>"
    )
    out = extract_metadata(html)
    assert out["title"] == "Real Title"
    assert out["description"] == "Real description"


def test_blank_content_and_missing_attrs_are_skipped():
    html = (
        "<html><head><title>   </title>"
        '<meta name="description" content="">'
        '<meta name="keywords">'        # no content attr
        '<link rel="canonical">'        # no href
        "</head></html>"
    )
    out = extract_metadata(html)
    assert out == {}  # nothing usable → empty dict, no blank keys


@pytest.mark.parametrize("bad", [
    "<html",                                    # unterminated
    "<html><head><title>x",                     # unclosed title/head/html
    "<meta property='og:title' content='x'",    # bare unclosed meta
    "<<<>>>",                                   # garbage
    "<html><head></head></html>",               # no metadata at all
])
def test_malformed_or_metadata_free_never_raises(bad):
    out = extract_metadata(bad)
    assert isinstance(out, dict)


def test_corpus_page_extracts_full_metadata(html_corpus):
    out = extract_metadata(html_corpus["metadata"], base_url="https://news.example.com/sec/")
    assert out["title"] == "Senate passes spending bill — Example News"  # &mdash; unescaped
    assert out["description"] == "The chamber approved the measure late Tuesday."
    assert out["author"] == "Jane Reporter"
    assert out["language"] == "en-US"
    assert out["canonical"] == "https://news.example.com/article/spending-bill"
    assert out["favicon"] == "https://news.example.com/favicon.ico"  # rel="icon" wins
    assert out["published"] == "2026-06-22T18:30:00Z"
    assert out["modified"] == "2026-06-22T20:05:00Z"
    assert out["og"]["title"] == "Senate passes spending bill"
    assert out["og"]["type"] == "article"
    assert out["og"]["image"] == "https://news.example.com/sec/media/lead.jpg"  # resolved
    assert out["og"]["url"] == "https://news.example.com/article/spending-bill"
    assert out["twitter"]["card"] == "summary_large_image"
    assert out["twitter"]["image"] == "https://news.example.com/sec/media/card.jpg"


# ── scrape mode: service paths ───────────────────────────────────────────────

_META_HTML = (
    "<html lang='en'><head><title>Lead Story</title>"
    "<meta name='description' content='A summary.'>"
    "<link rel='canonical' href='/story'>"
    "<meta property='og:image' content='/lead.jpg'>"
    "</head><body><p>body</p></body></html>"
)
_EXPECTED = {
    "title": "Lead Story",
    "language": "en",
    "description": "A summary.",
    "canonical": _HOME + "story",
    "og": {"image": _HOME + "lead.jpg"},
}


def _meta_service(**kwargs):
    routes = {_HOME: HttpResponse(url=_HOME, status=200, body=_META_HTML, final_url=_HOME)}
    return _service(FakeHttp(routes), **kwargs)


async def test_single_mode_metadata_returns_normalized_dict():
    res = await _meta_service().scrape(_HOME, mode="metadata")
    assert res.kind == "metadata"
    assert res.metadata == _EXPECTED
    assert res.fingerprint  # sha256 over the metadata dict


async def test_single_mode_metadata_parity_with_multi_extract():
    single = await _meta_service().scrape(_HOME, mode="metadata")
    multi = await _meta_service().scrape_multi(_HOME, modes=["metadata"])
    assert single.kind == "metadata" == multi["metadata"].kind
    assert single.metadata == multi["metadata"].metadata
    assert single.fingerprint == multi["metadata"].fingerprint


async def test_multi_extract_returns_metadata_and_structured():
    results = await _meta_service().scrape_multi(_HOME, modes=["metadata", "structured"])
    assert set(results) == {"metadata", "structured"}
    assert results["metadata"].kind == "metadata"
    assert results["metadata"].metadata == _EXPECTED


async def test_single_mode_metadata_served_from_cache_on_cooldown():
    from ujin.cache import HostPolicy

    svc = _meta_service(policy=HostPolicy(cooldown_secs=60))
    first = await svc.scrape(_HOME, mode="metadata")
    assert first.kind == "metadata"
    svc._policy.record_failure(_HOME)  # arm the cooldown
    cached = await svc.scrape(_HOME, mode="metadata")
    assert cached.cached is True
    assert cached.kind == "metadata"
    assert cached.metadata == first.metadata


# ── route-level dispatch ─────────────────────────────────────────────────────

def _meta_app():
    from ujin.cache import HostPolicy, ScrapeCache
    from ujin.scrape.app import create_scrape_app
    from ujin.scrape.config import ScrapeConfig
    from ujin.scrape.service import ScrapeService

    app = create_scrape_app(ScrapeConfig())
    routes = {_HOME: HttpResponse(url=_HOME, status=200, body=_META_HTML, final_url=_HOME)}
    service = ScrapeService(
        http=FakeHttp(routes), obscura=FakeObscura(),
        cache=ScrapeCache(), policy=HostPolicy(cooldown_secs=60),
        config=ScrapeConfig(fast_path_min_links=1),
    )
    return app, service


def test_route_single_metadata_mode_returns_dict_under_metadata_field():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    app, service = _meta_app()
    client = TestClient(app)
    client.__enter__()
    try:
        app.state.service = service
        r = client.post("/scrape", json={"url": _HOME, "mode": "metadata"})
        assert r.status_code == 200
        body = r.json()
        assert body["kind"] == "metadata"
        assert body["metadata"] == _EXPECTED
    finally:
        client.__exit__(None, None, None)


def test_route_multi_extract_metadata_and_structured_under_extracts():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    app, service = _meta_app()
    client = TestClient(app)
    client.__enter__()
    try:
        app.state.service = service
        r = client.post("/scrape", json={"url": _HOME, "modes": ["metadata", "structured"]})
        assert r.status_code == 200
        body = r.json()
        assert set(body["extracts"]) == {"metadata", "structured"}
        assert body["extracts"]["metadata"]["kind"] == "metadata"
        assert body["extracts"]["metadata"]["metadata"] == _EXPECTED
    finally:
        client.__exit__(None, None, None)
