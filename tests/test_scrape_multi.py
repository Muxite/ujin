"""Multi-extract orchestration tests.

Exercise ScrapeService.scrape_multi (one fetch, several modes) and the
`POST /scrape` `modes` dispatch: the per-mode merge, per-mode error isolation,
and single-mode parity with the classic `scrape()` path. Fakes are duck-typed
the same way test_scrape_service.py wires them — offline and deterministic.
"""
from __future__ import annotations

import pytest

from ujin.cache import HostPolicy, ScrapeCache
from ujin.fetch.http import HttpResponse
from ujin.scrape.config import ScrapeConfig
from ujin.scrape.service import ScrapeService

from test_scrape_service import FakeHttp, FakeObscura, _service

# asyncio_mode = "auto" (pyproject) runs the async tests below without a mark,
# and leaves the sync route tests sync.


# A page that yields links (>=30-char headlines), structured data (og:title),
# and a parseable body all at once.
_MULTI_HTML = (
    "<html><head>"
    '<meta property="og:title" content="Hello Multi">'
    "</head><body><main>"
    '<a href="https://news.example.com/2026/06/01/the-senate-passes-a-spending-bill">'
    "The Senate passes a sweeping spending bill tonight</a>"
    '<a href="https://news.example.com/2026/06/01/markets-rally-on-fresh-jobs-data">'
    "Markets rally sharply on fresh jobs data release</a>"
    '<a href="https://news.example.com/2026/06/01/storm-system-moves-up-the-coast">'
    "A powerful storm system moves up the eastern coast</a>"
    "</main></body></html>"
)

_HOME = "https://news.example.com/"


def _multi_service():
    routes = {_HOME: HttpResponse(url=_HOME, status=200, body=_MULTI_HTML, final_url=_HOME)}
    return _service(FakeHttp(routes))


async def test_multi_mode_returns_one_result_per_mode():
    svc = _multi_service()
    results = await svc.scrape_multi(_HOME, modes=["links", "structured", "html"])

    assert set(results) == {"links", "structured", "html"}

    assert results["links"].kind == "links"
    assert len(results["links"].links) == 3

    assert results["structured"].kind == "structured"
    assert results["structured"].structured["opengraph"]["og:title"] == "Hello Multi"

    assert results["html"].kind == "html"
    assert results["html"].html == _MULTI_HTML
    assert results["html"].fingerprint  # sha256 of the body


async def test_multi_mode_isolates_a_failing_mode(monkeypatch):
    """One mode's extractor blowing up must not sink the other modes."""
    import ujin.scrape.service as service_mod

    def _boom(*a, **k):
        raise ValueError("article extractor exploded")

    monkeypatch.setattr(service_mod, "extract_article", _boom)

    svc = _multi_service()
    results = await svc.scrape_multi(_HOME, modes=["links", "article", "structured"])

    # The failing mode is isolated as kind="error" with the exception in note.
    assert results["article"].kind == "error"
    assert "article extractor exploded" in (results["article"].note or "")
    # The other modes still produced real results.
    assert results["links"].kind == "links" and results["links"].links
    assert results["structured"].kind == "structured"


async def test_multi_mode_dedups_and_preserves_order():
    svc = _multi_service()
    results = await svc.scrape_multi(_HOME, modes=["structured", "structured", "links"])
    assert list(results) == ["structured", "links"]


async def test_multi_mode_single_mode_parity_with_scrape():
    """A single-element modes list reproduces what `scrape(mode=...)` returns."""
    single = await _multi_service().scrape(_HOME, mode="structured")
    multi = await _multi_service().scrape_multi(_HOME, modes=["structured"])
    assert single.kind == multi["structured"].kind
    assert single.structured == multi["structured"].structured
    assert single.fingerprint == multi["structured"].fingerprint


async def test_multi_mode_article_extracts_body(html_corpus):
    """A real article body comes back under the `article` mode entry."""
    art_url = "https://news.example.com/2026/06/01/full-story-here"
    routes = {art_url: HttpResponse(url=art_url, status=200,
                                    body=html_corpus["article"], final_url=art_url)}
    svc = _service(FakeHttp(routes))
    results = await svc.scrape_multi(art_url, modes=["article", "html"])
    assert results["article"].kind == "article"
    assert results["article"].article is not None
    assert results["article"].article.text
    assert results["html"].kind == "html"


async def test_multi_mode_article_empty_on_index_page():
    """An index-shaped page yields no article → the `article` entry is `empty`."""
    svc = _multi_service()  # _HOME is "/", which the article extractor treats as index
    results = await svc.scrape_multi(_HOME, modes=["article"])
    assert results["article"].kind == "empty"
    assert results["article"].article is None


async def test_multi_mode_empty_body_yields_empty_per_mode():
    """A 4xx with no renderer leaves html=None → every mode is `empty`, not error."""
    routes = {_HOME: HttpResponse(url=_HOME, status=403, body="", final_url=_HOME)}
    svc = _service(FakeHttp(routes), obscura=FakeObscura(html=None))
    results = await svc.scrape_multi(_HOME, modes=["links", "structured"])
    assert results["links"].kind == "empty"
    assert results["structured"].kind == "empty"


async def test_multi_mode_fetch_exception_maps_every_mode_to_error():
    """If the fetch itself raises, each requested mode comes back isolated."""
    svc = _multi_service()

    async def _raise(*a, **k):
        raise RuntimeError("fetch layer down")

    svc._fetch_html = _raise  # type: ignore[assignment]
    results = await svc.scrape_multi(_HOME, modes=["links", "html"])
    assert results["links"].kind == "error"
    assert results["html"].kind == "error"
    assert "fetch layer down" in (results["links"].note or "")


async def test_multi_mode_empty_modes_falls_back_to_links():
    svc = _multi_service()
    results = await svc.scrape_multi(_HOME, modes=[])
    assert list(results) == ["links"]
    assert results["links"].kind == "links"


# ── route-level dispatch ─────────────────────────────────────────────────────


def _real_service_client():
    from fastapi.testclient import TestClient

    from ujin.scrape.app import create_scrape_app

    app = create_scrape_app(ScrapeConfig())
    client = TestClient(app)
    client.__enter__()
    routes = {_HOME: HttpResponse(url=_HOME, status=200, body=_MULTI_HTML, final_url=_HOME)}
    app.state.service = ScrapeService(
        http=FakeHttp(routes),
        obscura=FakeObscura(),
        cache=ScrapeCache(),
        policy=HostPolicy(cooldown_secs=60),
        # 3 headlines is enough for the single-mode links fast path (so it does
        # not escalate to the unavailable obscura renderer in the parity test).
        config=ScrapeConfig(fast_path_min_links=1),
    )
    return client


def test_route_multi_mode_returns_extracts_map():
    fastapi = pytest.importorskip("fastapi")  # noqa: F841
    client = _real_service_client()
    try:
        r = client.post("/scrape", json={"url": _HOME, "modes": ["structured", "links", "html"]})
        assert r.status_code == 200
        body = r.json()
        # Top-level mirrors the first requested mode (structured).
        assert body["kind"] == "structured"
        assert body["structured"]["opengraph"]["og:title"] == "Hello Multi"
        # Every requested mode is present in `extracts`, none nested.
        assert set(body["extracts"]) == {"structured", "links", "html"}
        assert body["extracts"]["links"]["kind"] == "links"
        assert len(body["extracts"]["links"]["links"]) == 3
        assert body["extracts"]["html"]["html"] == _MULTI_HTML
        assert body["extracts"]["structured"]["extracts"] is None
    finally:
        client.__exit__(None, None, None)


def test_route_single_mode_has_null_extracts():
    """Classic single-`mode` requests are unchanged: `extracts` stays null."""
    fastapi = pytest.importorskip("fastapi")  # noqa: F841
    client = _real_service_client()
    try:
        r = client.post("/scrape", json={"url": _HOME, "mode": "links"})
        assert r.status_code == 200
        assert r.json()["extracts"] is None
    finally:
        client.__exit__(None, None, None)
