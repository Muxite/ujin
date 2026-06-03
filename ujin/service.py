"""ujin service — drive the poll engine over REST + WebSocket.

REST controls the engine (add/list/remove targets, sweep, stats); a WebSocket
streams change events live as targets change. The engine runs as a background
task so the daemon's adaptive/jittered polling happens while the API is served.

Endpoints:
  GET    /health
  GET    /stats
  GET    /targets
  POST   /targets         {kind, config, base?, min?, max?, jitter?}
  DELETE /targets/{key}
  POST   /sweep           run one pass now; returns per-target change flags
  WS     /ws              stream {"event":"change", "key":..., "fingerprint":...}

Build with :func:`create_app` to embed, or run ``ujin api`` / :func:`serve`.
Needs the ``service`` extra (fastapi, uvicorn, websockets).
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from pydantic import BaseModel

# Imported at module level so FastAPI can resolve handler annotations (the module
# uses `from __future__ import annotations`, which turns them into strings).
try:
    from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect

    _HAVE_FASTAPI = True
except ModuleNotFoundError:  # pragma: no cover
    _HAVE_FASTAPI = False

log = logging.getLogger("ujin.service")


class AddTarget(BaseModel):
    """Body for POST /targets."""

    kind: str
    config: dict[str, Any] = {}
    base: float = 60.0
    min: float = 5.0
    max: float = 3600.0
    jitter: str = "decorrelated"


class _Hub:
    """Tracks connected WebSockets and broadcasts change events to them."""

    def __init__(self) -> None:
        self._conns: set[Any] = set()

    def add(self, ws: Any) -> None:
        self._conns.add(ws)

    def remove(self, ws: Any) -> None:
        self._conns.discard(ws)

    async def broadcast(self, key: str, result: Any) -> None:
        await self.broadcast_event({
            "event": "change",
            "key": key,
            "fingerprint": result.fingerprint,
            "ts": result.ts,
        })

    async def broadcast_event(self, event: dict[str, Any]) -> None:
        """Send an arbitrary JSON event to every connected socket."""
        dead = []
        for ws in list(self._conns):
            try:
                await ws.send_json(event)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self._conns.discard(ws)


def create_app(config_path: str | None = None, *, run_engine: bool = True) -> Any:
    if not _HAVE_FASTAPI:  # pragma: no cover
        raise RuntimeError(
            "ujin service needs the 'service' extra: pip install 'ujin[service]'"
        )

    from ujin.cli import _build_pollable, _load
    from ujin.engine import PollEngine

    engine: PollEngine = _load(config_path) if config_path else PollEngine()
    hub = _Hub()

    # route every target's change through the hub (preserve any existing cb)
    def _wire(target) -> None:
        prev_cb = target.on_change

        async def _cb(key: str, result):
            if prev_cb is not None:
                r = prev_cb(key, result)
                if asyncio.iscoroutine(r):
                    await r
            await hub.broadcast(key, result)

        target.on_change = _cb

    for t in engine.targets.values():
        _wire(t)

    @asynccontextmanager
    async def lifespan(app):  # noqa: ANN001
        task = asyncio.create_task(engine.run()) if run_engine else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    app = FastAPI(title="ujin", version="0.3.0", lifespan=lifespan)

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "targets": len(engine.targets)}

    @app.get("/stats")
    def stats() -> dict[str, Any]:
        return engine.stats()

    @app.get("/targets")
    def list_targets() -> list[dict[str, Any]]:
        return [
            {
                "key": t.key,
                "interval": round(t.interval.current, 2),
                "polls": t.polls,
                "changes": t.changes,
                "circuit": t.breaker.state,
            }
            for t in engine.targets.values()
        ]

    @app.post("/targets")
    def add_target(req: AddTarget) -> dict[str, Any]:
        try:
            pollable = _build_pollable(req.kind, req.config)
        except (ValueError, KeyError) as exc:
            raise HTTPException(400, str(exc))
        target = engine.add(
            pollable, base=req.base, min_interval=req.min, max_interval=req.max,
            jitter=req.jitter,
        )
        _wire(target)
        return {"key": target.key}

    @app.delete("/targets/{key}")
    def remove_target(key: str) -> dict[str, Any]:
        if key not in engine.targets:
            raise HTTPException(404, f"no target {key!r}")
        del engine.targets[key]
        return {"removed": key}

    @app.post("/sweep")
    async def sweep() -> dict[str, Any]:
        await engine.sweep()
        return {
            "changed": [t.key for t in engine.targets.values()
                        if t.prev and t.prev.changed],
            "targets": len(engine.targets),
        }

    @app.get("/content")
    def content(key: str) -> dict[str, Any]:
        """Return the body ujin last fetched for *key*, plus change status.

        Lets a consumer (e.g. hct-chron) reuse the content ujin already
        retrieved instead of re-fetching the (anti-bot) origin itself. ``key``
        is the target key — for HTTP targets that is the URL.
        """
        t = engine.targets.get(key)
        if t is None or t.prev is None:
            raise HTTPException(404, f"no content for {key!r}")
        p = t.prev
        body = p.payload.get("body", "") if isinstance(p.payload, dict) else ""
        return {
            "key": key,
            "changed": p.changed,
            "fingerprint": p.fingerprint,
            "ts": p.ts,
            "status": p.status,
            "body": body or "",
        }

    @app.websocket("/ws")
    async def ws(socket: WebSocket) -> None:
        await socket.accept()
        hub.add(socket)
        try:
            while True:
                await socket.receive_text()  # keepalive / client pings
        except WebSocketDisconnect:
            pass
        finally:
            hub.remove(socket)

    app.state.engine = engine
    app.state.hub = hub
    return app


def serve(host: str = "0.0.0.0", port: int = 8900, config_path: str | None = None) -> None:
    import uvicorn

    uvicorn.run(create_app(config_path), host=host, port=port)
