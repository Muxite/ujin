"""Optional API-key gate for the jobs control plane.

Off by default (decision: trust-the-network). When ``UJIN_API_KEY`` is set, the
:class:`ApiKeyMiddleware` is mounted and every request — HTTP *and* WebSocket —
must present the key as ``X-API-Key: <key>`` or ``Authorization: Bearer <key>``.
``/health`` stays open so liveness probes work without a credential.

This is a pure-ASGI middleware so it can reject WebSocket handshakes too (which a
plain HTTP middleware cannot).
"""
from __future__ import annotations

import hmac
import json

_OPEN_PATHS = frozenset({"/health"})


def _present_key(headers: list[tuple[bytes, bytes]]) -> str | None:
    hdr = {k.lower(): v for k, v in headers}
    api_key = hdr.get(b"x-api-key")
    if api_key is not None:
        return api_key.decode("latin-1")
    auth = hdr.get(b"authorization")
    if auth is not None:
        val = auth.decode("latin-1")
        if val.lower().startswith("bearer "):
            return val[7:].strip()
    return None


class ApiKeyMiddleware:
    def __init__(self, app, api_key: str):
        self.app = app
        self.api_key = api_key

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        if scope.get("path") in _OPEN_PATHS:
            await self.app(scope, receive, send)
            return

        presented = _present_key(scope.get("headers", []))
        if presented is not None and hmac.compare_digest(presented, self.api_key):
            await self.app(scope, receive, send)
            return

        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = json.dumps({"detail": "unauthorized"}).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [(b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode())],
        })
        await send({"type": "http.response.body", "body": body})
