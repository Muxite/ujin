"""M12: /metrics aggregation and the optional API-key gate (HTTP + WS)."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from ujin.jobs.app import create_jobs_app  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))


def _job(out) -> dict:
    return {
        "name": "p",
        "source": {"kind": "command", "config": {"argv": ["printf", "hi"]}},
        "sinks": [{"kind": "jsonl", "config": {"path": str(out)}}],
        "schedule": {"mode": "once"},
    }


def test_metrics_aggregates(db, tmp_path):
    app = create_jobs_app(run_engine=False)
    with TestClient(app) as c:
        jid = c.post("/jobs", json=_job(tmp_path / "o.jsonl")).json()["id"]
        c.post(f"/jobs/{jid}/run")
        m = c.get("/metrics").json()
        assert m["totals"]["jobs"] == 1
        assert m["totals"]["polls"] >= 1
        assert m["totals"]["changes"] >= 1
        assert "engine" in m and "plugins" in m
        assert any(j["id"] == jid for j in m["jobs"])


def test_auth_disabled_by_default(db):
    app = create_jobs_app(run_engine=False)
    with TestClient(app) as c:
        assert c.get("/jobs").status_code == 200  # open when no key set


def test_auth_enabled_requires_key(db, monkeypatch, tmp_path):
    monkeypatch.setenv("UJIN_API_KEY", "s3cret")
    app = create_jobs_app(run_engine=False)
    with TestClient(app) as c:
        # health stays open for liveness probes
        assert c.get("/health").status_code == 200
        # protected routes require the key
        assert c.get("/jobs").status_code == 401
        assert c.get("/jobs", headers={"X-API-Key": "wrong"}).status_code == 401
        assert c.get("/jobs", headers={"X-API-Key": "s3cret"}).status_code == 200
        assert c.get(
            "/jobs", headers={"Authorization": "Bearer s3cret"}
        ).status_code == 200


def test_auth_guards_websocket(db, monkeypatch):
    monkeypatch.setenv("UJIN_API_KEY", "s3cret")
    app = create_jobs_app(run_engine=False)
    with TestClient(app) as c:
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/jobs/events"):
                pass
        # with the key the handshake succeeds
        with c.websocket_connect(
            "/jobs/events", headers={"X-API-Key": "s3cret"}
        ) as ws:
            assert ws is not None
