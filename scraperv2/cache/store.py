"""LRU+TTL cache for fetched scrape results.

Vendored from jennie/services/scraper-v2/app/cache/store.py.
Modifications:
- Removed jennie config/settings dependency; uses sensible defaults.
- Logger name changed to awork.scrape.cache.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Optional

_CACHE_MAX_ENTRIES = 512
_CACHE_TTL_SECS = 3600  # 1 hour — reasonable for content pipelines


@dataclass
class CachedEntry:
    url: str
    fingerprint: str
    payload: Any  # serialized response, opaque to the cache
    fetched_at: float
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    hits: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def age_secs(self) -> float:
        return time.monotonic() - self.fetched_at


class ScrapeCache:
    """Thread-safe LRU with TTL eviction."""

    def __init__(
        self,
        max_entries: int = _CACHE_MAX_ENTRIES,
        ttl_secs: int = _CACHE_TTL_SECS,
    ):
        self._max = max_entries
        self._ttl = ttl_secs
        self._store: "OrderedDict[str, CachedEntry]" = OrderedDict()
        self._lock = Lock()

    def get(self, key: str) -> Optional[CachedEntry]:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if entry.age_secs > self._ttl:
                self._store.pop(key, None)
                return None
            self._store.move_to_end(key)
            entry.hits += 1
            return entry

    def put(self, key: str, entry: CachedEntry) -> None:
        with self._lock:
            self._store[key] = entry
            self._store.move_to_end(key)
            while len(self._store) > self._max:
                self._store.popitem(last=False)

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def items(self):
        """Snapshot of (key, entry) pairs — for flushing to disk."""
        with self._lock:
            return list(self._store.items())

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "entries": len(self._store),
                "max": self._max,
                "ttl_secs": self._ttl,
            }
