"""0.4.0 API normalization: uniform health shape, /metrics everywhere,
UJIN_API_KEY gating all three services, stable import surfaces."""
from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from ujin.jobs.app import create_jobs_app  # noqa: E402
from ujin.scrape.app import create_scrape_app  # noqa: E402
from ujin.scrape.config import ScrapeConfig  # noqa: E402
from ujin.service import create_app as create_poller_app  # noqa: E402


@pytest.fixture
def jobs_env(tmp_path, monkeypatch):
    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    monkeypatch.delenv("UJIN_WORKFLOWS_DIR", raising=False)


def _apps(jobs=True):
    out = [
        ("ujin-poller", create_poller_app(run_engine=False)),
        ("ujin-scrape", create_scrape_app(ScrapeConfig())),
    ]
    if jobs:
        out.append(("ujin-jobs", create_jobs_app(run_engine=False)))
    return out


def test_health_shape_uniform_across_services(jobs_env):
    for service, app in _apps():
        with TestClient(app) as client:
            body = client.get("/health").json()
        assert body["ok"] is True, service
        assert body["status"] == "ok", service
        assert body["service"] == service


def test_metrics_route_on_all_services(jobs_env):
    for service, app in _apps():
        with TestClient(app) as client:
            assert client.get("/metrics").status_code == 200, service


def test_poller_stats_route_removed():
    app = create_poller_app(run_engine=False)
    with TestClient(app) as client:
        assert client.get("/stats").status_code == 404  # renamed to /metrics


def test_api_key_gates_all_three_services(jobs_env, monkeypatch):
    monkeypatch.setenv("UJIN_API_KEY", "k3y")
    for service, app in _apps():
        with TestClient(app) as client:
            # health stays open for probes
            assert client.get("/health").status_code == 200, service
            # everything else requires the key
            denied = client.get("/metrics")
            assert denied.status_code == 401, service
            allowed = client.get("/metrics", headers={"X-API-Key": "k3y"})
            assert allowed.status_code == 200, service


def test_auth_compat_shim():
    from ujin.auth import ApiKeyMiddleware as canonical
    from ujin.jobs.auth import ApiKeyMiddleware as shimmed

    assert canonical is shimmed


def test_stable_import_surfaces():
    from ujin.jobs import JobManager  # noqa: F401
    from ujin.scrape import (  # noqa: F401
        ScrapeConfig,
        ScrapeResult,
        ScrapeService,
        build_scrape_service,
    )

    import ujin.scrape

    with pytest.raises(AttributeError):
        ujin.scrape.NotAThing
