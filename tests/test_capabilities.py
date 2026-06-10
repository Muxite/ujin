"""Backend capability matrix + the /capabilities route."""
from __future__ import annotations

import pytest

from ujin.fetch.capabilities import BACKENDS, capabilities_snapshot


def test_matrix_covers_all_four_backends():
    assert set(BACKENDS) == {"http", "obscura", "playwright", "selenium"}


def test_snapshot_is_json_safe_and_has_availability():
    import json

    snap = capabilities_snapshot()
    json.dumps(snap)  # fully serializable
    for name, cap in snap.items():
        assert isinstance(cap["available"], bool)
        assert "availability_check" not in cap
        assert cap["name"] == name
        assert cap["js_rendering"] in ("none", "full")


def test_only_browsers_support_interaction():
    assert BACKENDS["http"].interaction is False
    assert BACKENDS["obscura"].interaction is False
    assert BACKENDS["playwright"].interaction is True
    assert BACKENDS["selenium"].interaction is True


def test_only_http_supports_conditional_get():
    assert BACKENDS["http"].conditional_get is True
    assert all(not BACKENDS[b].conditional_get
               for b in ("obscura", "playwright", "selenium"))


def test_availability_probe_failure_reads_as_unavailable(monkeypatch):
    cap = BACKENDS["obscura"]
    object.__setattr__  # frozen dataclass — patch the module fn instead
    monkeypatch.setattr("ujin.fetch.obscura.obscura_available",
                        lambda: 1 / 0)
    assert cap.available() is False


def test_http_backend_reports_available_here():
    # aiohttp is installed in the dev environment
    assert BACKENDS["http"].available() is True


def test_capabilities_route():
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ujin.scrape.app import create_scrape_app
    from ujin.scrape.config import ScrapeConfig

    app = create_scrape_app(ScrapeConfig())
    with TestClient(app) as client:
        body = client.get("/capabilities").json()
    assert set(body["backends"]) == {"http", "obscura", "playwright", "selenium"}
    assert body["backends"]["http"]["available"] is True
