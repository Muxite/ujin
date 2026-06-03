"""PollEngine — schedule many pollables with adaptive cadence and jitter.

The engine is the product: register targets, then either ``run()`` it as a
long-lived daemon or call ``sweep()`` for a single pass (cron-friendly). Each
target keeps its own :class:`AdaptiveInterval`, :class:`Backoff`, and
:class:`CircuitBreaker`; the engine applies jitter to every next-due time and
gates dispatch through a global :class:`TokenBucket` plus a concurrency cap so
aggregate load is smooth, not spiky.

Determinism: ``clock`` (time source), ``rng`` (jitter), and ``sleep`` are
injectable, so the scheduler can be driven by a fake clock in tests with no real
waiting.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ujin.adapt import jitter as _jitter
from ujin.adapt.backoff import Backoff, CircuitBreaker
from ujin.adapt.concurrency import TokenBucket
from ujin.adapt.interval import AdaptiveInterval
from ujin.poll.base import Pollable, PollResult

log = logging.getLogger("ujin.engine")

OnChange = Callable[[str, PollResult], Awaitable[None] | None]


@dataclass
class Target:
    """A registered pollable plus its adaptive state."""

    pollable: Pollable
    interval: AdaptiveInterval
    jitter: str = "decorrelated"
    on_change: OnChange | None = None
    backoff: Backoff = field(default_factory=Backoff)
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)
    prev: PollResult | None = None
    next_due: float = 0.0
    polls: int = 0
    changes: int = 0
    last_delay: float = 0.0

    @property
    def key(self) -> str:
        return self.pollable.key


class PollEngine:
    def __init__(
        self,
        *,
        token_bucket: TokenBucket | None = None,
        max_concurrency: int = 8,
        rng: random.Random | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.targets: dict[str, Target] = {}
        self.bucket = token_bucket or TokenBucket(rate=10.0, burst=10.0, clock=clock)
        self.sem = asyncio.Semaphore(max_concurrency)
        self.rng = rng or random.Random()
        self.clock = clock
        self.sleep = sleep
        self._started_at = clock()

    # -- registration ------------------------------------------------------ #
    def add(
        self,
        pollable: Pollable,
        *,
        base: float = 60.0,
        min_interval: float = 1.0,
        max_interval: float = 3600.0,
        grow: float = 1.6,
        shrink: float = 0.4,
        jitter: str = "decorrelated",
        on_change: OnChange | None = None,
    ) -> Target:
        interval = AdaptiveInterval(
            base=base, min_interval=min_interval, max_interval=max_interval,
            grow=grow, shrink=shrink,
        )
        target = Target(pollable=pollable, interval=interval, jitter=jitter,
                        on_change=on_change)
        # phase jitter: start out-of-phase so targets never fire together
        target.next_due = self.clock() + _jitter.phase(base, rng=self.rng)
        self.targets[pollable.key] = target
        return target

    # -- one poll ---------------------------------------------------------- #
    async def poll_once(self, target: Target) -> PollResult:
        """Run one poll, update adaptive state + schedule next_due. Returns result.

        Public entry point used by the daemon loop, ``sweep()``, and by the jobs
        scheduler to drive ``once``/``cron``/run-now jobs through the same global
        :class:`TokenBucket` + concurrency gate.
        """
        async with self.sem:
            await self.bucket.acquire(sleep=self.sleep)
            try:
                result = await target.pollable.poll(target.prev)
            except Exception as exc:  # noqa: BLE001
                result = PollResult.failure(f"{type(exc).__name__}: {exc}")

        target.polls += 1
        now = self.clock()

        if not result.ok:
            target.breaker.on_failure()
            delay = target.backoff.on_failure(result.retry_after)
            log.debug("poll %s failed: %s (backoff %.1fs)", target.key, result.error, delay)
        else:
            target.backoff.on_success()
            target.breaker.on_success()
            base = target.interval.next(result.changed)
            delay = _jitter.apply(base, target.jitter, rng=self.rng,
                                  prev=target.last_delay or base,
                                  cap=target.interval.max_interval)
            if result.changed:
                target.changes += 1
                await self._fire(target, result)
            target.prev = result

        target.last_delay = delay
        target.next_due = now + delay
        return result

    # back-compat alias (pre-M9 callers referenced the private name)
    _poll_target = poll_once

    async def _fire(self, target: Target, result: PollResult) -> None:
        if target.on_change is None:
            return
        try:
            ret = target.on_change(target.key, result)
            if asyncio.iscoroutine(ret):
                await ret
        except Exception:  # noqa: BLE001
            log.exception("on_change for %s raised", target.key)

    # -- one pass over all targets ----------------------------------------- #
    async def sweep(self) -> list[PollResult]:
        """Poll every target once (concurrently, smoothed). Cron-friendly."""
        due = [t for t in self.targets.values() if t.breaker.allow()]
        return await asyncio.gather(*(self.poll_once(t) for t in due))

    # -- daemon loop ------------------------------------------------------- #
    async def run(self, stop: asyncio.Event | None = None, *, max_ticks: int | None = None) -> None:
        """Run until ``stop`` is set (or ``max_ticks`` reached, for tests)."""
        ticks = 0
        while stop is None or not stop.is_set():
            now = self.clock()
            ready = [
                t for t in self.targets.values()
                if t.next_due <= now and t.breaker.allow()
            ]
            if ready:
                await asyncio.gather(*(self.poll_once(t) for t in ready))
            else:
                nxt = min((t.next_due for t in self.targets.values()), default=now + 1.0)
                await self.sleep(max(0.0, nxt - now))

            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                return

    # -- observability ----------------------------------------------------- #
    def stats(self) -> dict[str, Any]:
        return {
            "targets": len(self.targets),
            "polls": sum(t.polls for t in self.targets.values()),
            "changes": sum(t.changes for t in self.targets.values()),
            "open_circuits": sum(
                1 for t in self.targets.values() if t.breaker.state == "open"
            ),
            "per_target": {
                t.key: {
                    "polls": t.polls,
                    "changes": t.changes,
                    "interval": round(t.interval.current, 2),
                    "circuit": t.breaker.state,
                }
                for t in self.targets.values()
            },
        }
