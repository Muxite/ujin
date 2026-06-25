"""Global smoothing: a token bucket and an AIMD adaptive limiter.

The token bucket caps the *aggregate* request rate so bursts are flattened into a
steady stream (the main "stable not spiky" lever, complementing per-target jitter).
The AIMD limiter tunes how many polls run concurrently: additive increase on
success, multiplicative decrease on error/latency — the same control law TCP uses
to find a stable operating point.

Both take an injectable ``clock`` and (for the async wait) work with asyncio.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class TokenBucket:
    """Classic token bucket. ``rate`` tokens/sec, up to ``burst`` in reserve."""

    rate: float
    burst: float
    clock: Callable[[], float] = time.monotonic
    _tokens: float = field(default=None)  # type: ignore[assignment]
    _last: float = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.rate <= 0:
            raise ValueError("rate must be > 0")
        self._tokens = self.burst
        self._last = self.clock()

    def _refill(self) -> None:
        now = self.clock()
        elapsed = now - self._last
        self._last = now
        self._tokens = min(self.burst, self._tokens + elapsed * self.rate)

    def take(self, n: float = 1.0) -> bool:
        """Try to take ``n`` tokens now; True if granted."""
        self._refill()
        if self._tokens >= n:
            self._tokens -= n
            return True
        return False

    def time_until(self, n: float = 1.0) -> float:
        """Seconds until ``n`` tokens are available (0 if available now)."""
        self._refill()
        if self._tokens >= n:
            return 0.0
        return (n - self._tokens) / self.rate

    async def acquire(self, n: float = 1.0, *, sleep=asyncio.sleep) -> None:
        """Block until ``n`` tokens are available, then take them."""
        while True:
            wait = self.time_until(n)
            if wait <= 0:
                if self.take(n):
                    return
            else:
                await sleep(wait)


@dataclass
class AIMDLimiter:
    """Additive-increase / multiplicative-decrease concurrency target.

    Not a hard gate (the engine owns the semaphore); this just recommends a
    concurrency level that rises slowly while healthy and drops fast on trouble.
    """

    limit: float = 4.0
    min_limit: float = 1.0
    max_limit: float = 64.0
    increase: float = 1.0
    decrease: float = 0.5

    @property
    def value(self) -> int:
        return max(1, int(self.limit))

    def on_success(self) -> int:
        self.limit = min(self.max_limit, self.limit + self.increase)
        return self.value

    def on_failure(self) -> int:
        self.limit = max(self.min_limit, self.limit * self.decrease)
        return self.value
