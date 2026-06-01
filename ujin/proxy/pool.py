"""Health-aware round-robin proxy pool.

Each proxy is a URL string (``http://user:pass@host:port`` or ``socks5://...``)
as accepted by aiohttp's ``proxy=`` argument. On failure a proxy is benched for
a cooldown that grows with consecutive failures; ``acquire`` skips benched
proxies and rotates through the healthy ones.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from threading import Lock
from typing import Optional


@dataclass
class _ProxyState:
    url: str
    consecutive_failures: int = 0
    cooldown_until: float = 0.0


class ProxyPool:
    def __init__(self, proxies: list[str], *, cooldown_secs: float = 60.0):
        self._states = [_ProxyState(url=p) for p in proxies]
        self._cooldown = cooldown_secs
        self._next = 0
        self._lock = Lock()

    def __bool__(self) -> bool:
        return bool(self._states)

    def acquire(self) -> Optional[str]:
        """Return the next healthy proxy URL, or None if all are benched/empty."""
        now = time.monotonic()
        with self._lock:
            n = len(self._states)
            if n == 0:
                return None
            for _ in range(n):
                state = self._states[self._next % n]
                self._next = (self._next + 1) % n
                if state.cooldown_until <= now:
                    return state.url
        return None

    def record_success(self, url: str) -> None:
        with self._lock:
            for state in self._states:
                if state.url == url:
                    state.consecutive_failures = 0
                    state.cooldown_until = 0.0
                    return

    def record_failure(self, url: str) -> None:
        """Bench the proxy with an exponentially-growing cooldown (cap 8x)."""
        now = time.monotonic()
        with self._lock:
            for state in self._states:
                if state.url == url:
                    state.consecutive_failures += 1
                    exp = min(state.consecutive_failures - 1, 3)
                    state.cooldown_until = now + self._cooldown * (2 ** exp)
                    return

    def healthy(self) -> list[str]:
        now = time.monotonic()
        with self._lock:
            return [s.url for s in self._states if s.cooldown_until <= now]
