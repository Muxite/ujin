"""Durable per-host observed-state store.

The adaptive controllers in this package (:class:`AdaptiveInterval`,
:class:`Backoff`, :class:`CircuitBreaker`, the limiters) are *in-memory* and
forget everything on restart. :class:`SiteStore` is the persistence floor those
units build on: it remembers, per host, what we last saw — last HTTP status,
typical (p50) and last latency, accumulated error / 429 counts, any observed
``Crawl-delay``, the current adaptive interval, and when the host was last
touched — so a fresh process can resume polite, well-calibrated polling instead
of relearning every target from scratch.

It is dependency-free (stdlib ``sqlite3``) and reuses the exact durability
pattern from :mod:`ujin.cache.disk`: a single connection in WAL mode with
``synchronous=NORMAL`` (fast per-write commits that still survive process death
and reopen) guarded by one lock, and a truncating ``wal_checkpoint`` on
``close()`` so the on-disk artifact is self-contained. The clock is injectable
so ``last_seen`` timestamping is deterministic in tests.

Everything here is additive and defaults to in-process — nothing wires itself
into the engine. It is the foundation other Track-1 units consume.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import statistics
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

logger = logging.getLogger("ujin.adapt.site_store")

# Bound the recent-latency window the p50 is computed over: enough samples for a
# stable median, small enough to keep the persisted row tiny.
_P50_WINDOW = 128

_SCHEMA = """
CREATE TABLE IF NOT EXISTS site_state (
    host             TEXT PRIMARY KEY,
    last_status      INTEGER NOT NULL DEFAULT 0,
    last_latency     REAL    NOT NULL DEFAULT 0.0,
    p50_latency      REAL    NOT NULL DEFAULT 0.0,
    error_count      INTEGER NOT NULL DEFAULT 0,
    rate_limit_count INTEGER NOT NULL DEFAULT 0,
    crawl_delay      REAL    NOT NULL DEFAULT 0.0,
    interval         REAL    NOT NULL DEFAULT 0.0,
    last_seen        REAL    NOT NULL DEFAULT 0.0,
    recent_latencies TEXT    NOT NULL DEFAULT '[]'
);
"""

# Signals record() understands. "gauge" overwrites the stored value, "counter"
# adds the supplied delta, "latency" feeds last/p50 latency.
_GAUGES = ("status", "crawl_delay", "interval")
_COUNTERS = ("error", "rate_limited")
_SIGNALS = _GAUGES + _COUNTERS + ("latency",)


@dataclass(frozen=True)
class HostRecord:
    """Immutable snapshot of one host's observed state.

    Unknown hosts get a zero-valued instance (see :meth:`SiteStore.get`), so
    callers never have to special-case "never seen this host".
    """

    host: str
    last_status: int = 0
    last_latency: float = 0.0
    p50_latency: float = 0.0
    error_count: int = 0
    rate_limit_count: int = 0
    crawl_delay: float = 0.0
    interval: float = 0.0
    last_seen: float = 0.0


class SiteStore:
    """SQLite-backed per-host state with atomic upserts.

    Thread-safe via a single lock around all DB operations (the connection is
    opened with ``check_same_thread=False``). Each :meth:`record` is applied as
    one serialized read-modify-write committed transaction, so concurrent
    callers accumulate counters without losing updates.
    """

    def __init__(
        self,
        path: str | Path = ":memory:",
        clock: Callable[[], float] = time.time,
    ):
        self._path = str(path)
        self._clock = clock
        self._lock = threading.Lock()
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._configure_pragmas()
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def _configure_pragmas(self) -> None:
        """Enable WAL + relaxed-but-durable sync for fast per-write commits.

        Mirrors :class:`ujin.cache.disk.DiskCache`: ``journal_mode=WAL`` can't
        switch with a transaction open, so commit first and consume the result
        row. WAL is a no-op on in-memory DBs and unavailable on some network
        filesystems, so it is applied best-effort and we fall back to the safe
        rollback-journal default.
        """
        try:
            self._conn.commit()
            mode = self._conn.execute("PRAGMA journal_mode=WAL").fetchone()
            self._conn.execute("PRAGMA synchronous=NORMAL").fetchone()
            if not (mode and str(mode[0]).lower() == "wal"):  # pragma: no cover
                logger.debug("site store: WAL unavailable (mode=%s)", mode)
        except sqlite3.DatabaseError:  # pragma: no cover - exotic FS/driver
            logger.debug("site store: WAL pragmas unavailable; using defaults")

    def hosts(self) -> list[str]:
        """Return every host persisted in the store, sorted for stable output.

        A read-only enumeration so callers can iterate learned state (e.g. the
        ``ujin learned`` CLI) without knowing host names in advance. Returns an
        empty list for a never-written store.
        """
        with self._lock:
            cur = self._conn.execute("SELECT host FROM site_state ORDER BY host")
            return [row[0] for row in cur.fetchall()]

    def get(self, host: str) -> HostRecord:
        """Return the stored record for ``host``, or a zero-valued default."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT last_status, last_latency, p50_latency, error_count, "
                "rate_limit_count, crawl_delay, interval, last_seen "
                "FROM site_state WHERE host = ?",
                (host,),
            )
            row = cur.fetchone()
        if row is None:
            return HostRecord(host=host)
        return HostRecord(host, *row)

    def record(self, host: str, **signals: float) -> HostRecord:
        """Atomically upsert observed ``signals`` for ``host``; return the row.

        Recognized signals (any subset):

        - ``status`` (int)        — sets ``last_status``
        - ``latency`` (float)     — sets ``last_latency`` and updates ``p50_latency``
          over a bounded recent-sample window
        - ``crawl_delay`` (float) — sets observed ``Crawl-delay`` seconds
        - ``interval`` (float)    — sets the adaptive-interval state
        - ``error`` (int/bool)    — adds to ``error_count``
        - ``rate_limited`` (int/bool) — adds to ``rate_limit_count`` (a 429)

        ``last_seen`` is always stamped from the injected clock. Unknown signals
        raise ``ValueError`` to surface typos early.
        """
        unknown = set(signals) - set(_SIGNALS)
        if unknown:
            raise ValueError(
                f"unknown signal(s) {sorted(unknown)}; allowed: {sorted(_SIGNALS)}"
            )
        now = float(self._clock())
        with self._lock:
            cur = self._conn.execute(
                "SELECT last_status, last_latency, p50_latency, error_count, "
                "rate_limit_count, crawl_delay, interval, last_seen, recent_latencies "
                "FROM site_state WHERE host = ?",
                (host,),
            )
            row = cur.fetchone()
            if row is None:
                (status, last_lat, p50, errs, r429, delay, interval, _seen) = (
                    0, 0.0, 0.0, 0, 0, 0.0, 0.0, 0.0,
                )
                window: list[float] = []
            else:
                status, last_lat, p50, errs, r429, delay, interval, _seen = row[:8]
                window = _decode_window(row[8])

            if "status" in signals:
                status = int(signals["status"])
            if "crawl_delay" in signals:
                delay = float(signals["crawl_delay"])
            if "interval" in signals:
                interval = float(signals["interval"])
            if "latency" in signals:
                last_lat = float(signals["latency"])
                window.append(last_lat)
                del window[:-_P50_WINDOW]  # keep only the most recent samples
                p50 = float(statistics.median(window))
            errs += int(signals.get("error", 0))
            r429 += int(signals.get("rate_limited", 0))

            self._conn.execute(
                """INSERT INTO site_state (
                       host, last_status, last_latency, p50_latency, error_count,
                       rate_limit_count, crawl_delay, interval, last_seen,
                       recent_latencies)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(host) DO UPDATE SET
                       last_status=excluded.last_status,
                       last_latency=excluded.last_latency,
                       p50_latency=excluded.p50_latency,
                       error_count=excluded.error_count,
                       rate_limit_count=excluded.rate_limit_count,
                       crawl_delay=excluded.crawl_delay,
                       interval=excluded.interval,
                       last_seen=excluded.last_seen,
                       recent_latencies=excluded.recent_latencies""",
                (
                    host, status, last_lat, p50, errs, r429, delay, interval, now,
                    json.dumps(window),
                ),
            )
            self._conn.commit()
        return HostRecord(
            host, status, last_lat, p50, errs, r429, delay, interval, now
        )

    def close(self) -> None:
        """Fold the WAL back into the main DB file and close the connection."""
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.DatabaseError:  # pragma: no cover - already closing
                pass
            self._conn.close()


def _decode_window(blob: str) -> list[float]:
    """Parse the persisted recent-latency JSON list, tolerating corruption."""
    try:
        data = json.loads(blob)
        return [float(x) for x in data]
    except (ValueError, TypeError):  # pragma: no cover - defensive
        return []
