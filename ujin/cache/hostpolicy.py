"""Per-host failure tracking + cooldown.

When a host returns 429 or 5xx, every subsequent request to that host
is short-circuited for `cooldown_secs` instead of going on the wire.
This protects us from compounding outages (and from getting IP-banned)
without making each watcher know about its siblings.

The cooldown default is a plain constructor argument (the scrape service
injects it from :class:`ujin.scrape.config.ScrapeConfig`).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from threading import Lock
from urllib.parse import urlsplit


@dataclass
class _HostState:
    cooldown_until: float = 0.0
    consecutive_failures: int = 0


class HostPolicy:
    def __init__(self, cooldown_secs: int = 60):
        self._cooldown = cooldown_secs
        self._hosts: dict[str, _HostState] = {}
        self._lock = Lock()

    def _host(self, url: str) -> str:
        return urlsplit(url).netloc.lower()

    def cooldown_remaining(self, url: str) -> float:
        host = self._host(url)
        with self._lock:
            state = self._hosts.get(host)
            if state is None:
                return 0.0
            remaining = state.cooldown_until - time.monotonic()
            return max(0.0, remaining)

    def record_success(self, url: str) -> None:
        host = self._host(url)
        with self._lock:
            state = self._hosts.get(host)
            if state is not None:
                state.consecutive_failures = 0
                state.cooldown_until = 0.0

    def record_failure(self, url: str, status: int | None = None) -> None:
        """Bump failure count and set exponential cooldown.

        Cooldown grows: base, 2x, 4x, 8x (capped at 8x base). Resets on
        first success."""
        host = self._host(url)
        with self._lock:
            state = self._hosts.get(host)
            if state is None:
                state = _HostState()
                self._hosts[host] = state
            state.consecutive_failures += 1
            exp = min(state.consecutive_failures - 1, 3)
            penalty = self._cooldown * (2 ** exp)
            state.cooldown_until = time.monotonic() + penalty
