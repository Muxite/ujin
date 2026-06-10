"""ujin REST + WebSocket service. Skips if fastapi isn't installed."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from ujin.service import create_app  # noqa: E402


@pytest.fixture
def client():
    # run_engine=False: drive polling explicitly via /sweep (no background loop)
    app = create_app(run_engine=False)
    with TestClient(app) as c:
        yield c


def test_health_and_empty_targets(client):
    assert client.get("/health").json() == {
        "ok": True, "status": "ok", "service": "ujin-poller", "targets": 0,
    }
    assert client.get("/targets").json() == []


def test_add_command_target_and_sweep(client):
    r = client.post("/targets", json={"kind": "command",
                                      "config": {"argv": ["printf", "hi"]}, "base": 30})
    assert r.status_code == 200
    key = r.json()["key"]

    targets = client.get("/targets").json()
    assert any(t["key"] == key for t in targets)

    sweep = client.post("/sweep").json()
    assert key in sweep["changed"]            # first poll -> changed

    metrics = client.get("/metrics").json()   # 0.4.0: /stats renamed
    assert metrics["targets"] == 1 and metrics["polls"] >= 1


def test_add_bad_kind_400(client):
    r = client.post("/targets", json={"kind": "nope", "config": {}})
    assert r.status_code == 400


def test_delete_target(client):
    key = client.post("/targets", json={"kind": "command",
                                        "config": {"argv": ["true"]}}).json()["key"]
    assert client.delete(f"/targets/{key}").status_code == 200
    assert client.delete(f"/targets/{key}").status_code == 404


def test_websocket_receives_change_event(client):
    client.post("/targets", json={"kind": "command",
                                  "config": {"argv": ["printf", "x"]}})
    with client.websocket_connect("/ws") as ws:
        # trigger a poll while connected; the change broadcasts to the socket
        client.post("/sweep")
        event = ws.receive_json()
        assert event["event"] == "change"
        assert event["fingerprint"]
