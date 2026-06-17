"""Built-in sinks: webhook, ws, jsonl, stdout, sqlite, forward, csv.

Each exposes ``async emit(event) -> None`` (the
:class:`ujin.jobs.pipeline.Sink` protocol). ``build_sink(kind, cfg, *, hub,
store)`` maps a kind string to an instance; the plugin registry (M11) extends
this with ``plugin:*`` kinds.

Some sinks need ambient context: ``ws`` needs the app's broadcast hub, ``sqlite``
needs the :class:`ujin.jobs.store.JobStore`. These are injected at build time and
the sink no-ops (with a warning) if its dependency is absent.
"""
from __future__ import annotations

import asyncio
import csv as _csv
import hashlib
import hmac
import io
import json
import logging
import os
from typing import Any, Optional

log = logging.getLogger("ujin.jobs.sinks")


def _dumps(event: dict) -> str:
    return json.dumps(event, default=str, sort_keys=True)


class WebhookSink:
    """POST the event as JSON. Optional HMAC-SHA256 signature header.

    config: url (required), method (POST), headers ({}), timeout_secs (10),
            hmac_secret (optional -> X-Ujin-Signature: sha256=<hex>).
    """

    def __init__(self, cfg: dict):
        self.url = cfg["url"]
        self.method = cfg.get("method", "POST").upper()
        self.headers = dict(cfg.get("headers", {}))
        self.timeout = cfg.get("timeout_secs", 10)
        self.secret = cfg.get("hmac_secret")

    async def emit(self, event: dict) -> None:
        import aiohttp

        body = _dumps(event).encode("utf-8")
        headers = dict(self.headers)
        headers.setdefault("Content-Type", "application/json")
        if self.secret:
            sig = hmac.new(self.secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
            headers["X-Ujin-Signature"] = f"sha256={sig}"
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(
                self.method, self.url, data=body, headers=headers
            ) as resp:
                if resp.status >= 400:
                    log.warning("webhook %s -> HTTP %s", self.url, resp.status)


class ForwardSink(WebhookSink):
    """Alias of WebhookSink for the 'forward to another HTTP service' intent."""


class WsSink:
    """Broadcast the event to all connected WebSocket clients via the app hub."""

    def __init__(self, cfg: dict, *, hub: Any = None):
        self._hub = hub

    async def emit(self, event: dict) -> None:
        if self._hub is None:
            log.warning("ws sink: no hub available; dropping event")
            return
        await self._hub.broadcast_event(event)


class JsonlSink:
    """Append one JSON line per event to a file (serialized with a lock)."""

    _locks: dict[str, asyncio.Lock] = {}

    def __init__(self, cfg: dict):
        self.path = cfg["path"]

    async def emit(self, event: dict) -> None:
        lock = self._locks.setdefault(self.path, asyncio.Lock())
        line = _dumps(event) + "\n"
        async with lock:
            await asyncio.to_thread(self._append, line)

    def _append(self, line: str) -> None:
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line)


class StdoutSink:
    """Print the event as JSON. The dependency-free default sink."""

    def __init__(self, cfg: dict):
        self.prefix = cfg.get("prefix", "")

    async def emit(self, event: dict) -> None:
        print(f"{self.prefix}{_dumps(event)}", flush=True)


class SqliteSink:
    """Persist the event into the JobStore's job_events table."""

    def __init__(self, cfg: dict, *, store: Any = None):
        self._store = store

    async def emit(self, event: dict) -> None:
        if self._store is None:
            log.warning("sqlite sink: no store available; dropping event")
            return
        job_id = event.get("job_id", "")
        await asyncio.to_thread(self._store.record_event, job_id, event)


class CsvSink:
    """Append event rows to a CSV file (pure stdlib, dependency-free).

    Resolves a list/dict from the event (``path``, default ``payload``) and
    writes one CSV row per dict. The column set is fixed at construction:

      * ``columns`` given  -> those columns, in order (missing keys -> empty).
      * ``columns`` absent -> inferred from the keys of the first dict row seen,
        captured once and reused so the file's columns stay stable.

    A header row is written automatically the first time the file is created
    (suppress with ``header: false``). Writes are serialized per-path with an
    asyncio lock and run off the event loop. Non-dict items are skipped.

    config:
      path:    output file path (required)
      columns: explicit column order (optional)
      path_in_event: dotted path to the rows within the event (default "payload")
      header:  write a header row on file creation (default true)
      delimiter: field delimiter (default ",")
    """

    _locks: dict[str, asyncio.Lock] = {}

    def __init__(self, cfg: dict):
        self.path = cfg["path"]
        cols = cfg.get("columns")
        self.columns: list[str] | None = list(cols) if cols else None
        self.event_path = cfg.get("path_in_event", "payload")
        self.header = bool(cfg.get("header", True))
        self.delimiter = cfg.get("delimiter", ",")

    @staticmethod
    def _rows(target: Any) -> list[dict]:
        if isinstance(target, dict):
            return [target]
        if isinstance(target, list):
            return [it for it in target if isinstance(it, dict)]
        return []

    def _render(self, rows: list[dict], write_header: bool) -> str:
        cols = self.columns
        if cols is None:
            cols = list(rows[0].keys())
            self.columns = cols  # lock the column set for subsequent appends
        buf = io.StringIO()
        writer = _csv.DictWriter(
            buf, fieldnames=cols, extrasaction="ignore",
            delimiter=self.delimiter, lineterminator="\n",
        )
        if write_header and self.header:
            writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in cols})
        return buf.getvalue()

    async def emit(self, event: dict) -> None:
        from ujin.jobs.transforms import dotted_get

        rows = self._rows(dotted_get(event, self.event_path))
        if not rows:
            return
        lock = self._locks.setdefault(self.path, asyncio.Lock())
        async with lock:
            await asyncio.to_thread(self._append, rows)

    def _append(self, rows: list[dict]) -> None:
        write_header = not os.path.exists(self.path) or os.path.getsize(self.path) == 0
        text = self._render(rows, write_header)
        with open(self.path, "a", encoding="utf-8", newline="") as fh:
            fh.write(text)


_NEEDS_HUB = {"ws"}
_NEEDS_STORE = {"sqlite"}

BUILTIN_SINKS = {
    "webhook": WebhookSink,
    "forward": ForwardSink,
    "ws": WsSink,
    "jsonl": JsonlSink,
    "file": JsonlSink,
    "stdout": StdoutSink,
    "sqlite": SqliteSink,
    "csv": CsvSink,
}


def build_sink(kind: str, cfg: dict, *, hub: Any = None, store: Any = None):
    try:
        factory = BUILTIN_SINKS[kind]
    except KeyError:
        raise ValueError(f"unknown sink kind: {kind!r}") from None
    cfg = cfg or {}
    if kind in _NEEDS_HUB:
        return factory(cfg, hub=hub)
    if kind in _NEEDS_STORE:
        return factory(cfg, store=store)
    return factory(cfg)
