"""Image extraction — the `extract_images` parser plus the `images` scrape
mode (single-`mode` and multi-extract `extracts`).

Offline and deterministic: the parser runs over a corpus fixture
(`tests/fixtures/html/images.html`) and inline snippets; the service paths
reuse the duck-typed fakes from test_scrape_service.py.
"""
from __future__ import annotations

import pytest

from ujin.extract import extract_images
from ujin.fetch.http import HttpResponse

from test_scrape_service import FakeHttp, FakeObscura, _service

_HOME = "https://imgs.example.com/"


# ── extract_images: the parser ───────────────────────────────────────────────

def test_acceptance_empty_and_bare_img():
    # The two acceptance probes: never raises, always a list.
    assert extract_images("") == []
    out = extract_images("<img>")
    assert isinstance(out, list)


def test_relative_src_is_resolved_against_base_url():
    out = extract_images('<img src="pics/a.jpg" alt="A">', base_url="https://x.test/dir/page")
    assert out == [{"src": "https://x.test/dir/pics/a.jpg", "alt": "A"}]


def test_relative_src_kept_as_is_without_base_url():
    out = extract_images('<img src="pics/a.jpg">')
    assert out == [{"src": "pics/a.jpg", "alt": ""}]


def test_absolute_src_is_left_untouched():
    out = extract_images('<img src="https://cdn.test/h.png">', base_url="https://x.test/")
    assert out[0]["src"] == "https://cdn.test/h.png"


def test_lazy_data_src_wins_over_data_uri_placeholder():
    html = (
        '<img src="data:image/gif;base64,R0lGODlhAQABAAAAACw=" '
        'data-src="/real.jpg" alt="lazy">'
    )
    out = extract_images(html, base_url="https://x.test/")
    assert out == [{"src": "https://x.test/real.jpg", "alt": "lazy"}]


def test_lazy_data_original_is_honored():
    out = extract_images('<img data-original="/orig.jpg">', base_url="https://x.test/")
    assert out[0]["src"] == "https://x.test/orig.jpg"


def test_first_srcset_candidate_wins_descriptor_dropped():
    html = '<img srcset="/s.jpg 480w, /l.jpg 1024w" alt="r">'
    out = extract_images(html, base_url="https://x.test/")
    assert out == [{"src": "https://x.test/s.jpg", "alt": "r"}]


def test_lone_data_uri_is_kept_when_no_other_src_exists():
    # No other candidate → the data: URI is the only source available.
    uri = "data:image/png;base64,iVBORw0KGgo="
    out = extract_images(f'<img src="{uri}" alt="inline">')
    assert out == [{"src": uri, "alt": "inline"}]


def test_identical_src_is_deduped_in_document_order():
    html = (
        '<img src="/a.jpg" alt="first">'
        '<img src="/b.jpg" alt="middle">'
        '<img src="/a.jpg" alt="dup">'
    )
    out = extract_images(html, base_url="https://x.test/")
    assert [i["src"] for i in out] == ["https://x.test/a.jpg", "https://x.test/b.jpg"]
    # First occurrence wins (its alt is kept).
    assert out[0]["alt"] == "first"


def test_width_height_title_are_optional_and_typed():
    html = '<img src="/a.jpg" width="640" height="480px" title="Cap">'
    out = extract_images(html)
    rec = out[0]
    assert rec["width"] == 640 and isinstance(rec["width"], int)
    assert rec["height"] == 480           # trailing "px" tolerated
    assert rec["title"] == "Cap"


def test_bad_dimensions_and_blank_title_are_omitted():
    html = '<img src="/a.jpg" width="wide" height="-5" title="   ">'
    out = extract_images(html)
    assert out == [{"src": "/a.jpg", "alt": ""}]   # no width/height/title keys


@pytest.mark.parametrize("bad", [
    "",
    "   ",
    "<html><body><p>no images here</p></body></html>",
    "<img>",                                   # no source
    "<img src=>",                              # empty src
    "<img srcset>",                            # valueless srcset
    "<img src='/a.jpg'",                       # unclosed
])
def test_malformed_or_empty_never_raises(bad):
    out = extract_images(bad)
    assert isinstance(out, list)


def test_corpus_page_extracts_expected_images(html_corpus):
    out = extract_images(html_corpus["images"], base_url="https://news.example.com/sec/")
    srcs = [i["src"] for i in out]
    # Relative lead resolved, absolute untouched, lazy + data-original + srcset
    # resolved, the bare <img> dropped, and the duplicate lead de-duplicated.
    assert srcs == [
        "https://news.example.com/media/lead.jpg",
        "https://cdn.example.net/abs/hero.png",
        "https://news.example.com/lazy/real.jpg",
        "https://news.example.com/lazy/original.jpg",
        "https://news.example.com/img/small.jpg",
    ]
    lead = out[0]
    assert lead == {
        "src": "https://news.example.com/media/lead.jpg",
        "alt": "Lead photo", "width": 1024, "height": 576, "title": "On the floor",
    }


# ── scrape mode: service paths ───────────────────────────────────────────────

_IMG_HTML = (
    "<html><body>"
    '<img src="/lead.jpg" alt="Lead" width="800" height="450">'
    '<img src="data:image/gif;base64,R0lGODlh" data-src="/lazy.jpg" alt="Lazy">'
    "</body></html>"
)
_EXPECTED = [
    {"src": _HOME + "lead.jpg", "alt": "Lead", "width": 800, "height": 450},
    {"src": _HOME + "lazy.jpg", "alt": "Lazy"},
]


def _images_service(**kwargs):
    routes = {_HOME: HttpResponse(url=_HOME, status=200, body=_IMG_HTML, final_url=_HOME)}
    return _service(FakeHttp(routes), **kwargs)


async def test_single_mode_images_returns_normalized_dicts():
    res = await _images_service().scrape(_HOME, mode="images")
    assert res.kind == "images"
    assert res.images == _EXPECTED
    assert res.fingerprint  # sha256 over the image list


async def test_single_mode_images_parity_with_multi_extract():
    single = await _images_service().scrape(_HOME, mode="images")
    multi = await _images_service().scrape_multi(_HOME, modes=["images"])
    assert single.kind == "images" == multi["images"].kind
    assert single.images == multi["images"].images
    assert single.fingerprint == multi["images"].fingerprint


async def test_multi_extract_returns_images_and_structured():
    results = await _images_service().scrape_multi(_HOME, modes=["images", "structured"])
    assert set(results) == {"images", "structured"}
    assert results["images"].kind == "images"
    assert results["images"].images == _EXPECTED


async def test_single_mode_images_served_from_cache_on_cooldown():
    from ujin.cache import HostPolicy

    svc = _images_service(policy=HostPolicy(cooldown_secs=60))
    first = await svc.scrape(_HOME, mode="images")
    assert first.kind == "images"
    svc._policy.record_failure(_HOME)  # arm the cooldown
    cached = await svc.scrape(_HOME, mode="images")
    assert cached.cached is True
    assert cached.kind == "images"
    assert cached.images == first.images


# ── route-level dispatch ─────────────────────────────────────────────────────

def _img_app():
    from ujin.cache import HostPolicy, ScrapeCache
    from ujin.scrape.app import create_scrape_app
    from ujin.scrape.config import ScrapeConfig
    from ujin.scrape.service import ScrapeService

    app = create_scrape_app(ScrapeConfig())
    routes = {_HOME: HttpResponse(url=_HOME, status=200, body=_IMG_HTML, final_url=_HOME)}
    service = ScrapeService(
        http=FakeHttp(routes), obscura=FakeObscura(),
        cache=ScrapeCache(), policy=HostPolicy(cooldown_secs=60),
        config=ScrapeConfig(fast_path_min_links=1),
    )
    return app, service


def test_route_single_images_mode_returns_list_under_images_field():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    app, service = _img_app()
    client = TestClient(app)
    client.__enter__()
    try:
        app.state.service = service
        r = client.post("/scrape", json={"url": _HOME, "mode": "images"})
        assert r.status_code == 200
        body = r.json()
        assert body["kind"] == "images"
        assert body["images"] == _EXPECTED
    finally:
        client.__exit__(None, None, None)


def test_route_multi_extract_images_and_structured_under_extracts():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    app, service = _img_app()
    client = TestClient(app)
    client.__enter__()
    try:
        app.state.service = service
        r = client.post("/scrape", json={"url": _HOME, "modes": ["images", "structured"]})
        assert r.status_code == 200
        body = r.json()
        assert set(body["extracts"]) == {"images", "structured"}
        assert body["extracts"]["images"]["kind"] == "images"
        assert body["extracts"]["images"]["images"] == _EXPECTED
    finally:
        client.__exit__(None, None, None)
