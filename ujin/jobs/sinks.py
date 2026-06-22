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
import random
import time
from typing import Any, Optional

log = logging.getLogger("ujin.jobs.sinks")


def _dumps(event: dict) -> str:
    return json.dumps(event, default=str, sort_keys=True)


class _Delivered(Exception):
    """Raised internally once a POST succeeds, to break the retry loop."""


class WebhookSink:
    """POST the event as JSON, with retry/backoff and a disk spool for outages.

    The webhook is the *only* path scraped items take to the backend, so a transient
    backend outage must never silently drop a whole sweep (it did once, taking the DB
    offline for days). Delivery is therefore hardened three ways:

    * **Retry with exponential backoff + jitter** on *retriable* failures — connection
      errors, timeouts, and ``429``/``5xx`` responses. ``4xx`` (except ``429``) are
      permanent client errors, so they are logged and not retried.
    * **Disk spool** (``spool_dir``, default ``$UJIN_SPOOL_DIR`` or ``/data/spool``): when
      every retry is exhausted the event is written to a file instead of being dropped.
    * **Replay on recovery**: every successful ``emit`` first drains any spooled events
      (oldest first), so once the backend is back the backlog flushes automatically.

    config: url (required), method (POST), headers ({}), timeout_secs (10),
            hmac_secret (optional -> X-Ujin-Signature: sha256=<hex>),
            retries (4), backoff_secs (1.0), backoff_max_secs (30.0),
            spool_dir (str|None — empty string disables spooling),
            spool_max_files (500).
    """

    # 429 (rate limited) + 5xx are transient; retry. Other 4xx are permanent.
    _RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

    def __init__(self, cfg: dict):
        self.url = cfg["url"]
        self.method = cfg.get("method", "POST").upper()
        self.headers = dict(cfg.get("headers", {}))
        self.timeout = cfg.get("timeout_secs", 10)
        self.secret = cfg.get("hmac_secret")
        self.retries = max(0, int(cfg.get("retries", 4)))
        self.backoff = max(0.0, float(cfg.get("backoff_secs", 1.0)))
        self.backoff_max = max(self.backoff, float(cfg.get("backoff_max_secs", 30.0)))
        self.spool_max_files = max(0, int(cfg.get("spool_max_files", 500)))
        # `spool_dir: ""` disables spooling; omitted -> env / default. The dir is per-URL
        # (its hash) so two webhooks never replay each other's events.
        spool = cfg.get("spool_dir", os.environ.get("UJIN_SPOOL_DIR", "/data/spool"))
        if spool:
            tag = hashlib.sha1(self.url.encode("utf-8")).hexdigest()[:12]
            self.spool_dir: str | None = os.path.join(str(spool), f"webhook-{tag}")
        else:
            self.spool_dir = None

    def _sign(self, body: bytes) -> dict[str, str]:
        headers = dict(self.headers)
        headers.setdefault("Content-Type", "application/json")
        if self.secret:
            sig = hmac.new(self.secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
            headers["X-Ujin-Signature"] = f"sha256={sig}"
        return headers

    async def _post_once(self, session, body: bytes) -> bool:
        """One POST attempt. Returns True on success, False if the failure is retriable.

        Raises (non-retriable) on a permanent 4xx so the caller stops retrying.
        """
        import aiohttp

        try:
            async with session.request(
                self.method, self.url, data=body, headers=self._sign(body)
            ) as resp:
                if resp.status < 400:
                    return True
                if resp.status in self._RETRY_STATUSES:
                    log.warning("webhook %s -> HTTP %s (retriable)", self.url, resp.status)
                    return False
                log.warning("webhook %s -> HTTP %s (permanent; not retried)",
                            self.url, resp.status)
                raise _Delivered  # permanent: give up without spooling a doomed retry
        except aiohttp.ClientError as exc:
            log.warning("webhook %s connect error (retriable): %s", self.url, exc)
            return False
        except asyncio.TimeoutError:
            log.warning("webhook %s timed out (retriable)", self.url)
            return False

    # _deliver outcomes.
    _OK = "ok"            # delivered (2xx/3xx)
    _RETRIABLE = "retry"  # transient failure, retries exhausted -> spool
    _PERMANENT = "perm"   # 4xx (not 429) -> drop, retrying can't help

    async def _deliver(self, body: bytes) -> str:
        """POST with bounded exponential backoff. Returns one of _OK/_RETRIABLE/_PERMANENT."""
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for attempt in range(self.retries + 1):
                try:
                    if await self._post_once(session, body):
                        return self._OK
                except _Delivered:
                    return self._PERMANENT  # 4xx — spooling/retrying can't help
                if attempt < self.retries:
                    delay = min(self.backoff * (2 ** attempt), self.backoff_max)
                    delay += random.uniform(0, delay * 0.25)  # jitter to avoid thundering herd
                    await asyncio.sleep(delay)
        return self._RETRIABLE

    async def emit(self, event: dict) -> None:
        # On any successful contact with the backend, first flush whatever the last
        # outage spooled — oldest first — so a recovered backend drains the backlog.
        await self._drain_spool()
        body = _dumps(event).encode("utf-8")
        outcome = await self._deliver(body)
        if outcome == self._RETRIABLE:
            self._spool(body)  # transient outage — keep it for replay

    # ── disk spool ────────────────────────────────────────────────────────────
    def _spool(self, body: bytes) -> None:
        """Persist an undelivered event so a backend outage never loses a sweep."""
        if not self.spool_dir:
            log.error("webhook %s: delivery failed and spooling disabled — DROPPED %d bytes",
                      self.url, len(body))
            return
        try:
            os.makedirs(self.spool_dir, exist_ok=True)
            existing = self._spool_files()
            if self.spool_max_files and len(existing) >= self.spool_max_files:
                # Bounded: drop the oldest so a long outage can't exhaust the disk.
                for stale in existing[: len(existing) - self.spool_max_files + 1]:
                    self._remove(stale)
            # Monotonic ns + a random suffix keeps names ordered and collision-free.
            name = f"{time.time_ns():020d}-{random.randint(0, 1 << 20):06d}.json"
            tmp = os.path.join(self.spool_dir, name + ".tmp")
            final = os.path.join(self.spool_dir, name)
            with open(tmp, "wb") as fh:
                fh.write(body)
            os.replace(tmp, final)  # atomic
            log.warning("webhook %s: spooled undelivered event -> %s", self.url, final)
        except OSError as exc:  # noqa: BLE001
            log.error("webhook %s: spool write failed (%s) — DROPPED %d bytes",
                      self.url, exc, len(body))

    def _spool_files(self) -> list[str]:
        if not self.spool_dir or not os.path.isdir(self.spool_dir):
            return []
        return sorted(
            os.path.join(self.spool_dir, n)
            for n in os.listdir(self.spool_dir)
            if n.endswith(".json")
        )

    async def _drain_spool(self) -> None:
        """Replay spooled events oldest-first. Stops at the first one that won't deliver."""
        files = self._spool_files()
        if not files:
            return
        log.info("webhook %s: replaying %d spooled event(s)", self.url, len(files))
        for path in files:
            try:
                with open(path, "rb") as fh:
                    body = fh.read()
            except OSError:
                continue
            outcome = await self._deliver(body)
            if outcome == self._OK:
                self._remove(path)
            elif outcome == self._PERMANENT:
                # The backend rejected it (e.g. bad/expired secret) — retrying forever
                # would wedge the queue, so drop this one and keep draining the rest.
                log.error("webhook %s: spooled event permanently rejected — dropping %s",
                          self.url, path)
                self._remove(path)
            else:
                # Backend still down — leave the rest spooled for the next attempt.
                log.warning("webhook %s: replay paused, %d event(s) still spooled",
                            self.url, len(self._spool_files()))
                return

    @staticmethod
    def _remove(path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            pass


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
