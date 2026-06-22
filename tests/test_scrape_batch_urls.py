"""Multi-URL batch scrape tests.

Exercise ``ScrapeService.scrape_urls`` (one request, many URLs) and the
``POST /scrape`` ``urls`` dispatch: the per-URL merge into ``batch``, request
order preservation, per-URL error isolation, the bounded concurrency cap, and
single-``url`` backward compatibility. Fakes are duck-typed the same way
test_scrape_service.py wires them — offline and deterministic.
"""
from __future__ import annotations

import asyncio

import pytest

from ujin.cache import HostPolicy, ScrapeCache
from ujin.fetch.http import HttpResponse
from ujin.scrape.config import ScrapeConfig
from ujin.scrape.service import ScrapeResult, ScrapeService

from test_scrape_service import FakeHttp, FakeObscura, _service

# asyncio_mode = "auto" (pyproject) runs the async tests below without a mark,
# and leaves the sync route tests sync.


def _page(host: str, slug: str) -> str:
    """A homepage with three long-headline links (passes the link extractor)."""
    return (
        "<html><head></head><body><main>"
        f'<a href="https://{host}/2026/06/01/{slug}-one">'
        "A sufficiently long and clear headline number one</a>"
        f'<a href="https://{host}/2026/06/01/{slug}-two">'
        "A sufficiently long and clear headline number two</a>"
        f'<a href="https://{host}/2026/06/01/{slug}-three">'
        "A sufficiently long and clear headline number three</a>"
        "</main></body></html>"
    )


URL_A = "https://a.example.com/"
URL_B = "https://b.example.com/"
URL_C = "https://c.example.com/"


def _ok(url: str) -> ScrapeResult:
    return ScrapeResult(
        url=url, kind="links", fingerprint="fp", fetched_at=1.0,
        cached=False, age_secs=0.0, used_renderer=False, strategy_used="http",
    )


# ── service-level: scrape_urls orchestration ─────────────────────────────────


async def test_scrape_urls_returns_one_result_per_url_in_order():
    """A real fan-out over FakeHttp yields one links result per URL, in order."""
    routes = {
        URL_A: HttpResponse(url=URL_A, status=200, body=_page("a.example.com", "a"), final_url=URL_A),
        URL_B: HttpResponse(url=URL_B, status=200, body=_page("b.example.com", "b"), final_url=URL_B),
    }
    svc = _service(FakeHttp(routes), config=ScrapeConfig(fast_path_min_links=1))
    results = await svc.scrape_urls([URL_A, URL_B], mode="links")

    assert [r.url for r in results] == [URL_A, URL_B]
    assert all(r.kind == "links" for r in results)
    assert results[0].links and results[1].links


async def test_scrape_urls_isolates_a_failing_url():
    """One URL raising must not sink the others; order is preserved."""
    svc = _service(FakeHttp({}))

    async def fake_scrape(url, **kwargs):
        if url.endswith("/bad"):
            raise RuntimeError("kaboom")
        return _ok(url)

    svc.scrape = fake_scrape  # type: ignore[assignment]
    urls = ["https://x.test/a", "https://x.test/bad", "https://x.test/b"]
    results = await svc.scrape_urls(urls)

    assert [r.url for r in results] == urls            # order + one per URL
    assert results[0].kind == "links"
    assert results[1].kind == "error"
    assert results[1].strategy_used == "error"
    assert "kaboom" in (results[1].note or "")
    assert results[2].kind == "links"


async def test_scrape_urls_bounds_concurrency():
    """No more than `max_concurrency` per-URL fetches run at once."""
    svc = _service(FakeHttp({}))
    inflight = 0
    observed_max = 0

    async def fake_scrape(url, **kwargs):
        nonlocal inflight, observed_max
        inflight += 1
        observed_max = max(observed_max, inflight)
        await asyncio.sleep(0)  # let every *eligible* task reach this point
        inflight -= 1
        return _ok(url)

    svc.scrape = fake_scrape  # type: ignore[assignment]
    urls = [f"https://x.test/{i}" for i in range(10)]
    results = await svc.scrape_urls(urls, max_concurrency=3)

    assert [r.url for r in results] == urls
    assert observed_max <= 3       # never exceeded the configured cap
    assert observed_max == 3       # ...and the cap was actually reached


async def test_scrape_urls_serial_when_cap_is_one():
    """max_concurrency=1 forces strictly serial execution."""
    svc = _service(FakeHttp({}))
    inflight = 0
    observed_max = 0

    async def fake_scrape(url, **kwargs):
        nonlocal inflight, observed_max
        inflight += 1
        observed_max = max(observed_max, inflight)
        await asyncio.sleep(0)
        inflight -= 1
        return _ok(url)

    svc.scrape = fake_scrape  # type: ignore[assignment]
    await svc.scrape_urls([f"https://x.test/{i}" for i in range(5)], max_concurrency=1)
    assert observed_max == 1


async def test_scrape_urls_preserves_order_under_out_of_order_completion():
    """Results follow request order even when later URLs finish first."""
    svc = _service(FakeHttp({}))

    async def fake_scrape(url, **kwargs):
        # The first URL finishes last.
        await asyncio.sleep(0.02 if url.endswith("/0") else 0.0)
        return _ok(url)

    svc.scrape = fake_scrape  # type: ignore[assignment]
    urls = [f"https://x.test/{i}" for i in range(4)]
    results = await svc.scrape_urls(urls, max_concurrency=4)
    assert [r.url for r in results] == urls


async def test_scrape_urls_forwards_request_params():
    """Per-request params reach each per-URL scrape() call unchanged."""
    svc = _service(FakeHttp({}))
    seen: list[dict] = []

    async def fake_scrape(url, **kwargs):
        seen.append({"url": url, **kwargs})
        return _ok(url)

    svc.scrape = fake_scrape  # type: ignore[assignment]
    await svc.scrape_urls(
        [URL_A, URL_B], mode="article", force_refresh=True,
        enrich_html_top_n=3, render="browser", actions=[{"action": "scroll"}],
    )
    assert {s["url"] for s in seen} == {URL_A, URL_B}
    for s in seen:
        assert s["mode"] == "article"
        assert s["force_refresh"] is True
        assert s["enrich_html_top_n"] == 3
        assert s["render"] == "browser"
        assert s["actions"] == [{"action": "scroll"}]


# ── route-level: POST /scrape with `urls` ────────────────────────────────────


def _batch_route_client(config: ScrapeConfig | None = None):
    from fastapi.testclient import TestClient

    from ujin.scrape.app import create_scrape_app

    cfg = config or ScrapeConfig(fast_path_min_links=1)
    app = create_scrape_app(cfg)
    client = TestClient(app)
    client.__enter__()
    routes = {
        URL_A: HttpResponse(url=URL_A, status=200, body=_page("a.example.com", "a"), final_url=URL_A),
        URL_B: HttpResponse(url=URL_B, status=200, body=_page("b.example.com", "b"), final_url=URL_B),
        # URL_C deliberately absent → fetch fails → isolated error entry.
    }
    app.state.service = ScrapeService(
        http=FakeHttp(routes),
        obscura=FakeObscura(),
        cache=ScrapeCache(),
        policy=HostPolicy(cooldown_secs=60),
        config=ScrapeConfig(fast_path_min_links=1),
    )
    return client


def test_route_urls_returns_batch():
    pytest.importorskip("fastapi")
    client = _batch_route_client()
    try:
        r = client.post("/scrape", json={"urls": [URL_A, URL_B], "mode": "links"})
        assert r.status_code == 200
        body = r.json()
        # Top-level mirrors the FIRST requested URL.
        assert body["url"] == URL_A
        assert body["kind"] == "links"
        # `batch` carries one response per URL, in request order, none nested.
        assert [e["url"] for e in body["batch"]] == [URL_A, URL_B]
        assert body["batch"][0]["kind"] == "links"
        assert body["batch"][0]["links"]
        assert body["batch"][1]["kind"] == "links"
        assert body["batch"][0]["batch"] is None
        assert body["batch"][1]["batch"] is None
    finally:
        client.__exit__(None, None, None)


def test_route_single_url_has_null_batch():
    """Classic single-`url` requests are unchanged: `batch` stays null."""
    pytest.importorskip("fastapi")
    client = _batch_route_client()
    try:
        r = client.post("/scrape", json={"url": URL_A, "mode": "links"})
        assert r.status_code == 200
        body = r.json()
        assert body["batch"] is None
        assert body["kind"] == "links"
        assert body["links"]
    finally:
        client.__exit__(None, None, None)


def test_route_urls_isolates_failing_url():
    """A failing URL comes back as a kind='error' entry; the others succeed."""
    pytest.importorskip("fastapi")
    client = _batch_route_client()
    try:
        r = client.post("/scrape", json={"urls": [URL_A, URL_C, URL_B], "mode": "links"})
        assert r.status_code == 200
        body = r.json()
        assert [e["url"] for e in body["batch"]] == [URL_A, URL_C, URL_B]
        assert body["batch"][0]["kind"] == "links"
        assert body["batch"][1]["kind"] == "error"
        assert body["batch"][1]["strategy_used"] == "error"
        assert body["batch"][2]["kind"] == "links"
    finally:
        client.__exit__(None, None, None)


def test_route_urls_exceeds_batch_max_items_400():
    pytest.importorskip("fastapi")
    client = _batch_route_client(ScrapeConfig(fast_path_min_links=1, batch_max_items=2))
    try:
        r = client.post("/scrape", json={"urls": [URL_A, URL_B, URL_C], "mode": "links"})
        assert r.status_code == 400
        assert "exceeds max" in r.json()["detail"]
    finally:
        client.__exit__(None, None, None)


def test_route_empty_urls_falls_back_to_single_url_400():
    """An empty `urls` list is not a batch request; with no `url` it 400s."""
    pytest.importorskip("fastapi")
    client = _batch_route_client()
    try:
        r = client.post("/scrape", json={"urls": [], "mode": "links"})
        assert r.status_code == 400
    finally:
        client.__exit__(None, None, None)
