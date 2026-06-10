"""ApiKeyMiddleware: header/bearer acceptance, rejection, open /health,
and WebSocket handshake closure."""
from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi import FastAPI, WebSocket  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from ujin.jobs.auth import ApiKeyMiddleware, _present_key  # noqa: E402

KEY = "sekrit"


def _app():
    app = FastAPI()

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/jobs")
    def jobs():
        return []

    @app.websocket("/jobs/events")
    async def ws(socket: WebSocket):
        await socket.accept()
        await socket.send_json({"hello": True})
        await socket.close()

    wrapped = FastAPI()
    wrapped.mount("", app)
    return ApiKeyMiddleware(app, KEY)


@pytest.fixture
def client():
    return TestClient(_app())


def test_health_open_without_key(client):
    assert client.get("/health").status_code == 200


def test_request_without_key_401(client):
    r = client.get("/jobs")
    assert r.status_code == 401
    assert r.json() == {"detail": "unauthorized"}


def test_x_api_key_accepted(client):
    assert client.get("/jobs", headers={"X-API-Key": KEY}).status_code == 200


def test_bearer_accepted(client):
    r = client.get("/jobs", headers={"Authorization": f"Bearer {KEY}"})
    assert r.status_code == 200


def test_wrong_key_401(client):
    assert client.get("/jobs", headers={"X-API-Key": "nope"}).status_code == 401
    assert client.get("/jobs", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_non_bearer_authorization_ignored(client):
    r = client.get("/jobs", headers={"Authorization": f"Basic {KEY}"})
    assert r.status_code == 401


def test_websocket_with_key_accepted(client):
    with client.websocket_connect("/jobs/events",
                                  headers={"X-API-Key": KEY}) as ws:
        assert ws.receive_json() == {"hello": True}


def test_websocket_without_key_closed(client):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/jobs/events"):
            pass
    assert exc.value.code == 1008


def test_present_key_parsing():
    assert _present_key([(b"x-api-key", b"k")]) == "k"
    assert _present_key([(b"authorization", b"Bearer tok")]) == "tok"
    assert _present_key([(b"authorization", b"bearer tok")]) == "tok"
    assert _present_key([(b"authorization", b"Basic tok")]) is None
    assert _present_key([]) is None
