"""Durable per-host, per-strategy outcome store for adaptive backend selection.

:class:`StrategyFeedback` records the outcome (ok/fail, latency) of each
``(backend, render_mode)`` pair tried for a host and exposes a
:meth:`~StrategyFeedback.recommend` query that returns the highest-success-rate
known strategy.  It is the *input layer* for future learned strategy-selection
and rate-limit avoidance logic.

Durability mirrors :mod:`ujin.adapt.site_store`: one stdlib-``sqlite3``
connection in WAL mode with ``synchronous=NORMAL``, a single lock for
serialized atomic upserts, and a truncating ``wal_checkpoint`` on
:meth:`~StrategyFeedback.close`.

Everything is additive and opt-in.  Nothing in this module wires itself into
the default scrape or poll path.
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

from ujin.adapt.signals import derive_signals
from ujin.adapt.site_store import HostRecord

logger = logging.getLogger("ujin.adapt.strategy")

_P50_WINDOW = 128

# Strategies whose host has health below this are penalized by is_penalized().
_LOW_HEALTH_THRESHOLD = 0.5

_SCHEMA = """
CREATE TABLE IF NOT EXISTS strategy_outcomes (
    host             TEXT    NOT NULL,
    backend          TEXT    NOT NULL,
    render_mode      TEXT    NOT NULL,
    attempts         INTEGER NOT NULL DEFAULT 0,
    successes        INTEGER NOT NULL DEFAULT 0,
    failures         INTEGER NOT NULL DEFAULT 0,
    last_latency     REAL    NOT NULL DEFAULT 0.0,
    p50_latency      REAL    NOT NULL DEFAULT 0.0,
    last_seen        REAL    NOT NULL DEFAULT 0.0,
    recent_latencies TEXT    NOT NULL DEFAULT '[]',
    PRIMARY KEY (host, backend, render_mode)
);
"""


@dataclass(frozen=True)
class StrategyOutcome:
    """Immutable snapshot of accumulated outcomes for one (host, strategy) pair.

    ``strategy`` is ``(backend, render_mode)``.  Unknown (host, strategy) pairs
    yield a zero-valued instance — callers never need to special-case "never seen".
    """

    host: str
    strategy: tuple[str, str]
    attempts: int = 0
    successes: int = 0
    failures: int = 0
    p50_latency: float = 0.0
    last_latency: float = 0.0
    last_seen: float = 0.0


class StrategyFeedback:
    """SQLite-backed per-host, per-strategy outcome store.

    ``strategy`` is a ``(backend, render_mode)`` pair, e.g.
    ``("http", "html")`` or ``("obscura", "js")``.

    Thread-safe via a single lock around all DB operations.  Each
    :meth:`record` is an atomic read-modify-write committed transaction so
    concurrent callers accumulate counters without losing updates.

    Args:
        store: SQLite path (``":memory:"`` for ephemeral in-process store).
        clock: time source for ``last_seen``; injectable for deterministic tests.
    """

    def __init__(
        self,
        store: str | Path = ":memory:",
        clock: Callable[[], float] = time.time,
    ):
        self._path = str(store)
        self._clock = clock
        self._lock = threading.Lock()
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._configure_pragmas()
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def _configure_pragmas(self) -> None:
        try:
            self._conn.commit()
            mode = self._conn.execute("PRAGMA journal_mode=WAL").fetchone()
            self._conn.execute("PRAGMA synchronous=NORMAL").fetchone()
            if not (mode and str(mode[0]).lower() == "wal"):  # pragma: no cover
                logger.debug("strategy store: WAL unavailable (mode=%s)", mode)
        except sqlite3.DatabaseError:  # pragma: no cover
            logger.debug("strategy store: WAL pragmas unavailable; using defaults")

    def record(
        self,
        host: str,
        strategy: tuple[str, str],
        *,
        ok: bool,
        latency: float,
    ) -> StrategyOutcome:
        """Atomically upsert one outcome for ``(host, strategy)``; return the row.

        Counters (``attempts``, ``successes``, ``failures``) accumulate; latency
        gauges (``last_latency``, ``p50_latency``) overwrite / roll; ``last_seen``
        is stamped from the injected clock.
        """
        backend, render_mode = strategy
        now = float(self._clock())
        with self._lock:
            cur = self._conn.execute(
                "SELECT attempts, successes, failures, last_latency, p50_latency, "
                "last_seen, recent_latencies "
                "FROM strategy_outcomes "
                "WHERE host = ? AND backend = ? AND render_mode = ?",
                (host, backend, render_mode),
            )
            row = cur.fetchone()
            if row is None:
                attempts = successes = failures = 0
                window: list[float] = []
            else:
                attempts, successes, failures = row[0], row[1], row[2]
                window = _decode_window(row[6])

            attempts += 1
            if ok:
                successes += 1
            else:
                failures += 1
            last_latency = float(latency)
            window.append(last_latency)
            del window[:-_P50_WINDOW]
            p50_latency = float(statistics.median(window))

            self._conn.execute(
                """INSERT INTO strategy_outcomes (
                       host, backend, render_mode, attempts, successes, failures,
                       last_latency, p50_latency, last_seen, recent_latencies)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(host, backend, render_mode) DO UPDATE SET
                       attempts=excluded.attempts,
                       successes=excluded.successes,
                       failures=excluded.failures,
                       last_latency=excluded.last_latency,
                       p50_latency=excluded.p50_latency,
                       last_seen=excluded.last_seen,
                       recent_latencies=excluded.recent_latencies""",
                (
                    host, backend, render_mode,
                    attempts, successes, failures,
                    last_latency, p50_latency, now,
                    json.dumps(window),
                ),
            )
            self._conn.commit()
        return StrategyOutcome(
            host=host,
            strategy=strategy,
            attempts=attempts,
            successes=successes,
            failures=failures,
            p50_latency=p50_latency,
            last_latency=last_latency,
            last_seen=now,
        )

    def recommend(self, host: str) -> tuple[str, str] | None:
        """Return the highest-success-rate known strategy for ``host``.

        Success rate is ``successes / attempts``.  Ties are broken
        deterministically: most attempts first, then lexicographic
        ``(backend, render_mode)``.  Returns ``None`` for an unseen host.
        """
        with self._lock:
            cur = self._conn.execute(
                "SELECT backend, render_mode, successes, attempts "
                "FROM strategy_outcomes WHERE host = ?",
                (host,),
            )
            rows = cur.fetchall()
        if not rows:
            return None

        def _key(r: tuple) -> tuple:
            backend, render_mode, successes, attempts = r
            rate = successes / attempts if attempts else 0.0
            return (-rate, -attempts, backend, render_mode)

        rows.sort(key=_key)
        return (rows[0][0], rows[0][1])

    def is_penalized(
        self,
        host: str,
        strategy: tuple[str, str],
        record: HostRecord,
    ) -> bool:
        """Return ``True`` if host signals suggest this strategy should be avoided.

        Pure — performs no I/O.  Penalizes when
        :func:`~ujin.adapt.signals.derive_signals` reports the host is
        ``rate_limited`` or ``health`` has fallen below the low-health
        threshold.  The caller supplies the ``HostRecord`` (e.g. via
        :meth:`~ujin.adapt.site_store.SiteStore.get`) so this method stays I/O-free.
        """
        signals = derive_signals(record)
        return signals.rate_limited or signals.health < _LOW_HEALTH_THRESHOLD

    def close(self) -> None:
        """Fold the WAL back into the main DB file and close the connection."""
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.DatabaseError:  # pragma: no cover
                pass
            self._conn.close()


def _decode_window(blob: str) -> list[float]:
    try:
        data = json.loads(blob)
        return [float(x) for x in data]
    except (ValueError, TypeError):  # pragma: no cover
        return []
