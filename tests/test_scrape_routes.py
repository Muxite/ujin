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
    async def scrape(self, url, *, mode="links", force_refresh=False, enrich_html_top_n=0):
        from ujin.extract.links import NormalizedLink

        link = NormalizedLink(url="https://x.test/a", text="A headline that is long enough")
        return ScrapeResult(
            url=url, kind="links", fingerprint="fp", fetched_at=1.0,
            cached=False, age_secs=0.0, used_renderer=False,
            strategy_used="http", links=[link], next_poll_hint_secs=60.0,
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
