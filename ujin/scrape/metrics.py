"""Per-host metrics collector.

Sliding-window counters and latency percentiles, keyed by the host
portion of the request URL. The collector is thread-safe (FastAPI may
serve concurrent requests on multiple workers, though uvicorn default
is single-worker async — the lock is cheap insurance).

We compute percentiles by sorting the deque copy on read. The window
is small (default 100), so this is O(N log N) per /metrics call which
is well under a millisecond.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from threading import Lock
from typing import Optional
from urllib.parse import urlsplit


_LATENCY_WINDOW = 100


@dataclass
class _HostBucket:
    fetches: int = 0
    successes: int = 0
    failures: int = 0
    renderer_used: int = 0
    cached_returns: int = 0
    fallback_used: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    latency_ms: deque = field(default_factory=lambda: deque(maxlen=_LATENCY_WINDOW))
    last_seen: float = 0.0


class HostMetrics:
    def __init__(self) -> None:
        self._buckets: dict[str, _HostBucket] = defaultdict(_HostBucket)
        self._lock = Lock()

    @staticmethod
    def host_of(url: str) -> str:
        return urlsplit(url).netloc.lower() or "(unknown)"

    def record(
        self,
        url: str,
        *,
        success: bool,
        latency_ms: float,
        used_renderer: bool = False,
        cached: bool = False,
        strategy: Optional[str] = None,
    ) -> None:
        host = self.host_of(url)
        now = time.time()
        with self._lock:
            b = self._buckets[host]
            b.fetches += 1
            if success:
                b.successes += 1
            else:
                b.failures += 1
            if used_renderer:
                b.renderer_used += 1
            if cached:
                b.cached_returns += 1
            if strategy:
                b.fallback_used[strategy] = b.fallback_used.get(strategy, 0) + 1
            if latency_ms >= 0:
                b.latency_ms.append(latency_ms)
            b.last_seen = now

    def snapshot(self) -> dict:
        with self._lock:
            out: dict = {"hosts": {}, "total_fetches": 0}
            for host, b in self._buckets.items():
                out["total_fetches"] += b.fetches
                samples = sorted(b.latency_ms)
                p50 = _percentile(samples, 50)
                p95 = _percentile(samples, 95)
                out["hosts"][host] = {
                    "fetches": b.fetches,
                    "successes": b.successes,
                    "failures": b.failures,
                    "renderer_used": b.renderer_used,
                    "cached_returns": b.cached_returns,
                    "fallback_used": dict(b.fallback_used),
                    "latency_ms_p50": p50,
                    "latency_ms_p95": p95,
                    "samples": len(samples),
                    "last_seen": b.last_seen,
                }
            return out


def _percentile(sorted_samples: list[float], p: int) -> Optional[float]:
    if not sorted_samples:
        return None
    if len(sorted_samples) == 1:
        return float(sorted_samples[0])
    # Nearest-rank.
    k = max(0, min(len(sorted_samples) - 1, int(round((p / 100) * (len(sorted_samples) - 1)))))
    return float(sorted_samples[k])
