"""Error backoff and a circuit breaker.

When a target errors (timeout, 5xx) or is rate-limited (429), eujin should slow
down rather than keep hammering. :class:`Backoff` produces an exponentially
growing delay that honors a provider-supplied ``retry_after``. :class:`CircuitBreaker`
trips after repeated failures so a dead target is skipped entirely until a probe.

Both are pure/stateful and take an injectable ``clock`` for deterministic tests.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable


@dataclass
class Backoff:
    """Exponential backoff with a cap; resets on success."""

    base: float = 1.0
    factor: float = 2.0
    cap: float = 3600.0
    _failures: int = 0

    @property
    def failures(self) -> int:
        return self._failures

    def on_success(self) -> None:
        self._failures = 0

    def on_failure(self, retry_after: float | None = None) -> float:
        """Record a failure; return the delay to wait before the next attempt.

        A provided ``retry_after`` (e.g. from a 429) takes precedence but is still
        capped.
        """
        self._failures += 1
        if retry_after is not None:
            return min(self.cap, max(0.0, retry_after))
        delay = self.base * (self.factor ** (self._failures - 1))
        return min(self.cap, delay)

    def reset(self) -> None:
        self._failures = 0


@dataclass
class CircuitBreaker:
    """Open after ``threshold`` consecutive failures; half-open after ``cooldown``.

    States: closed (normal) -> open (skip) -> half_open (allow one probe).
    """

    threshold: int = 5
    cooldown: float = 300.0
    clock: Callable[[], float] = time.monotonic
    _failures: int = 0
    _opened_at: float | None = None

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        if self.clock() - self._opened_at >= self.cooldown:
            return "half_open"
        return "open"

    def allow(self) -> bool:
        """Whether a poll should be attempted now."""
        return self.state != "open"

    def on_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def on_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.threshold and self._opened_at is None:
            self._opened_at = self.clock()
        elif self.state == "half_open":
            # probe failed -> re-open with a fresh cooldown window
            self._opened_at = self.clock()
