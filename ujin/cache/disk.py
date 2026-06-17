"""SQLite-backed durable cache.

Vendored from jennie/services/scraper-v2/app/cache/disk.py.
Modifications:
- Removed jennie config/settings dependency.
- Logger name changed to awork.scrape.cache.disk.
- Added ``get`` / ``contains`` helpers for direct-lookup patterns used
  by awork source blocks (they don't want to pre-load everything into
  memory).
"""

from __future__ import annotations

import logging
import pickle
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from .store import CachedEntry

logger = logging.getLogger("awork.scrape.cache.disk")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (
    key TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    etag TEXT,
    last_modified TEXT,
    fetched_wall REAL NOT NULL,
    payload BLOB NOT NULL
);
"""


class DiskCache:
    """SQLite cache with pickle-serialised payloads.

    Thread-safe via a single lock around all DB operations.

    Durability/throughput: the connection runs in WAL mode with
    ``synchronous=NORMAL``. WAL lets each ``put`` commit 10-50x faster than the
    default rollback journal (no full fsync of the database file per commit)
    while keeping the cache's contract intact — committed rows survive process
    death and reopen (see ``test_disk_persists_across_reopen``). Only an OS/power
    loss inside the checkpoint window can drop the most recent commits, which is
    acceptable for a cache (the memory tier is the source of truth at runtime and
    is re-flushed at shutdown). ``journal_mode`` is a no-op on in-memory DBs, so
    the PRAGMA is applied best-effort.
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._configure_pragmas()
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def _configure_pragmas(self) -> None:
        """Enable WAL + relaxed-but-durable sync for fast per-put commits.

        ``journal_mode=WAL`` cannot switch while a transaction is open, so any
        implicit transaction must be committed first; the result row is consumed
        to fully execute the statement. The mode is then verified — on a
        filesystem that can't support WAL (some network shares), SQLite silently
        keeps the rollback journal and we simply run with the safe default.
        """
        try:
            self._conn.commit()
            mode = self._conn.execute("PRAGMA journal_mode=WAL").fetchone()
            self._conn.execute("PRAGMA synchronous=NORMAL").fetchone()
            if not (mode and str(mode[0]).lower() == "wal"):  # pragma: no cover
                logger.debug("disk cache: WAL unavailable (mode=%s)", mode)
        except sqlite3.DatabaseError:  # pragma: no cover - exotic FS/driver
            logger.debug("disk cache: WAL pragmas unavailable; using defaults")

    def close(self) -> None:
        with self._lock:
            try:
                # Fold the WAL back into the main DB file so the on-disk artifact
                # is self-contained and the last commits are durably checkpointed.
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.DatabaseError:  # pragma: no cover - already closing
                pass
            self._conn.close()

    def get(self, key: str) -> Optional[CachedEntry]:
        """Look up a single key directly (avoids loading the whole table)."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT url, fingerprint, etag, last_modified, payload "
                "FROM cache WHERE key = ?",
                (key,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        url, fingerprint, etag, last_modified, payload_blob = row
        try:
            payload = pickle.loads(payload_blob)
        except Exception:  # noqa: BLE001
            logger.warning("dropping corrupt cache row for key=%s", key)
            return None
        return CachedEntry(
            url=url,
            fingerprint=fingerprint,
            payload=payload,
            fetched_at=time.monotonic(),
            etag=etag,
            last_modified=last_modified,
        )

    def contains(self, key: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM cache WHERE key = ?", (key,)
            )
            return cur.fetchone() is not None

    def load_all(self) -> Iterable[tuple[str, CachedEntry]]:
        """Yield (cache_key, entry) for every row — for warm-up."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT key, url, fingerprint, etag, last_modified, payload FROM cache"
            )
            rows = cur.fetchall()
        now = time.monotonic()
        for key, url, fingerprint, etag, last_modified, payload_blob in rows:
            try:
                payload = pickle.loads(payload_blob)
            except Exception:  # noqa: BLE001
                logger.warning("dropping corrupt cache row for key=%s", key)
                continue
            yield key, CachedEntry(
                url=url,
                fingerprint=fingerprint,
                payload=payload,
                fetched_at=now,
                etag=etag,
                last_modified=last_modified,
            )

    def put(self, key: str, entry: CachedEntry) -> None:
        try:
            blob = pickle.dumps(entry.payload, protocol=pickle.HIGHEST_PROTOCOL)
        except Exception as exc:  # noqa: BLE001
            logger.debug("disk-cache put skipped (unpicklable payload): %s", exc)
            return
        with self._lock:
            self._conn.execute(
                """INSERT INTO cache (key, url, fingerprint, etag, last_modified,
                                       fetched_wall, payload)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                       fingerprint=excluded.fingerprint,
                       etag=excluded.etag,
                       last_modified=excluded.last_modified,
                       fetched_wall=excluded.fetched_wall,
                       payload=excluded.payload""",
                (
                    key,
                    entry.url,
                    entry.fingerprint,
                    entry.etag,
                    entry.last_modified,
                    time.time(),
                    blob,
                ),
            )
            self._conn.commit()

    def flush_from(self, entries: Iterable[tuple[str, CachedEntry]]) -> None:
        """Bulk write — call at shutdown to persist in-memory cache."""
        rows: list[tuple[Any, ...]] = []
        for key, entry in entries:
            try:
                blob = pickle.dumps(entry.payload, protocol=pickle.HIGHEST_PROTOCOL)
            except Exception:
                continue
            rows.append(
                (
                    key,
                    entry.url,
                    entry.fingerprint,
                    entry.etag,
                    entry.last_modified,
                    time.time(),
                    blob,
                )
            )
        if not rows:
            return
        with self._lock:
            self._conn.executemany(
                """INSERT INTO cache (key, url, fingerprint, etag, last_modified,
                                       fetched_wall, payload)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                       fingerprint=excluded.fingerprint,
                       etag=excluded.etag,
                       last_modified=excluded.last_modified,
                       fetched_wall=excluded.fetched_wall,
                       payload=excluded.payload""",
                rows,
            )
            self._conn.commit()
