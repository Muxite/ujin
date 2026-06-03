"""Scrape HTTP surface tests — FastAPI TestClient over create_scrape_app.

This is the wire-parity gate against scrape/models.py. We stub the service
layer on app.state so route shapes are exercised without live network.
"""
from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from ujin.scrape.app import create_scrape_app  # noqa: E402
from ujin.scrape.config import ScrapeConfig  # noqa: E402
from ujin.scrape.service import ScrapeResult  # noqa: E402


class _StubService:
    async def scrape(self, url, *, mode="links", force_refresh=False,
                     enrich_html_top_n=0, render="auto", actions=None,
                     n_links=1):
        from ujin.extract.links import NormalizedLink

        links = [
            NormalizedLink(url=f"https://x.test/a{i}",
                           text=f"A headline that is long enough #{i}")
            for i in range(n_links)
        ]
        return ScrapeResult(
            url=url, kind="links", fingerprint="fp", fetched_at=1.0,
            cached=False, age_secs=0.0, used_renderer=(render == "browser"),
            strategy_used=("browser" if render == "browser" else "http"),
            links=links, next_poll_hint_secs=60.0,
        )

    async def scrape_batch(self, items):
        return [await self.scrape(u, mode=m, force_refresh=f) for (u, m, f) in items]


class _StubMetrics:
    def snapshot(self):
        return {"total_fetches": 3, "hosts": {"x.test": {"fetches": 3}}}


class _StubCache:
    def stats(self):
        return {"entries": 1, "hits": 0, "misses": 0}


def _client_with_stubs():
    app = create_scrape_app(ScrapeConfig())

    # Replace the lifespan-wired state with stubs after startup.
    client = TestClient(app)
    client.__enter__()  # trigger lifespan startup
    app.state.service = _StubService()
    app.state.metrics = _StubMetrics()
    app.state.cache = _StubCache()
    return client, app


def test_health_shape():
    client, _ = _client_with_stubs()
    try:
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert "obscura_available" in body
        assert "cache" in body
    finally:
        client.__exit__(None, None, None)


def test_scrape_shape():
    client, _ = _client_with_stubs()
    try:
        r = client.post("/scrape", json={"url": "https://x.test", "mode": "links"})
        assert r.status_code == 200
        body = r.json()
        assert body["kind"] == "links"
        assert body["fingerprint"] == "fp"
        assert body["strategy_used"] == "http"
        assert len(body["links"]) == 1
        # Additive scoring fields present with neutral defaults.
        link = body["links"][0]
        assert link["tier"] in {"mainstream", "generic"}
        assert link["breaking_score"] == 0.0
        assert body["next_poll_hint_secs"] == 60.0
    finally:
        client.__exit__(None, None, None)


class _PagedStub:
    """Returns N links with a controllable fingerprint (for cursor-drift tests)."""

    def __init__(self, n=5, fingerprint="fp"):
        self.n = n
        self.fingerprint = fingerprint

    async def scrape(self, url, *, mode="links", force_refresh=False,
                     enrich_html_top_n=0, render="auto", actions=None):
        from ujin.extract.links import NormalizedLink

        links = [NormalizedLink(url=f"https://x.test/{i}", text=f"headline number {i}")
                 for i in range(self.n)]
        return ScrapeResult(
            url=url, kind="links", fingerprint=self.fingerprint, fetched_at=1.0,
            cached=False, age_secs=0.0, used_renderer=False, strategy_used="http",
            links=links, next_poll_hint_secs=60.0,
        )


def test_scrape_pagination_pages_and_cursor():
    client, app = _client_with_stubs()
    app.state.service = _PagedStub(n=5)
    try:
        r1 = client.post("/scrape", json={"url": "https://x.test", "page_size": 2})
        b1 = r1.json()
        assert len(b1["links"]) == 2 and b1["total"] == 5
        assert b1["next_cursor"]
        assert [l["url"] for l in b1["links"]] == ["https://x.test/0", "https://x.test/1"]

        r2 = client.post("/scrape", json={"url": "https://x.test", "page_size": 2,
                                          "cursor": b1["next_cursor"]})
        b2 = r2.json()
        assert [l["url"] for l in b2["links"]] == ["https://x.test/2", "https://x.test/3"]

        r3 = client.post("/scrape", json={"url": "https://x.test", "page_size": 2,
                                          "cursor": b2["next_cursor"]})
        b3 = r3.json()
        assert [l["url"] for l in b3["links"]] == ["https://x.test/4"]
        assert b3["next_cursor"] is None      # last page
    finally:
        client.__exit__(None, None, None)


def test_scrape_no_page_size_unchanged():
    client, app = _client_with_stubs()
    app.state.service = _PagedStub(n=5)
    try:
        b = client.post("/scrape", json={"url": "https://x.test"}).json()
        assert len(b["links"]) == 5
        assert b["total"] is None and b["next_cursor"] is None   # opt-in only
    finally:
        client.__exit__(None, None, None)


def test_scrape_stale_cursor_409():
    client, app = _client_with_stubs()
    app.state.service = _PagedStub(n=5, fingerprint="v1")
    try:
        b1 = client.post("/scrape", json={"url": "https://x.test", "page_size": 2}).json()
        # the underlying list changes -> fingerprint drifts
        app.state.service = _PagedStub(n=5, fingerprint="v2")
        r = client.post("/scrape", json={"url": "https://x.test", "page_size": 2,
                                         "cursor": b1["next_cursor"]})
        assert r.status_code == 409
    finally:
        client.__exit__(None, None, None)


def test_scrape_browser_render_strategy():
    client, _ = _client_with_stubs()
    try:
        r = client.post("/scrape", json={
            "url": "https://x.test", "render": "browser",
            "actions": [{"action": "load_more", "button": ".m", "results": ".i"}]})
        assert r.status_code == 200
        assert r.json()["strategy_used"] == "browser"
    finally:
        client.__exit__(None, None, None)


def test_scrape_empty_url_400():
    client, _ = _client_with_stubs()
    try:
        r = client.post("/scrape", json={"url": "", "mode": "links"})
        assert r.status_code == 400
    finally:
        client.__exit__(None, None, None)


def test_batch_shape():
    client, _ = _client_with_stubs()
    try:
        r = client.post("/scrape:batch", json={"requests": [
            {"url": "https://x.test/1", "mode": "links"},
            {"url": "https://x.test/2", "mode": "links"},
        ]})
        assert r.status_code == 200
        assert len(r.json()["results"]) == 2
    finally:
        client.__exit__(None, None, None)


def test_metrics_shape():
    client, _ = _client_with_stubs()
    try:
        r = client.get("/metrics")
        assert r.status_code == 200
        body = r.json()
        assert body["total_fetches"] == 3
        assert "x.test" in body["hosts"]
    finally:
        client.__exit__(None, None, None)
