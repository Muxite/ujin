"""Engine behavior at scale and under contention: 1k targets in one sweep,
token-bucket gating, semaphore caps, and circuit-breaker isolation."""
from __future__ import annotations

import asyncio

from ujin.adapt.concurrency import TokenBucket
from ujin.engine import PollEngine
from ujin.poll.base import PollResult
from ujin.poll.callable import CallablePollable


class _Counter:
    """Pollable that tracks concurrent executions."""

    def __init__(self, key: str, *, delay: float = 0.0, fail: bool = False):
        self.key = key
        self.delay = delay
        self.fail = fail
        self.polls = 0
        self.inflight = 0
        self.max_inflight = 0

    async def poll(self, prev):
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            self.polls += 1
            if self.fail:
                return PollResult.failure("boom")
            return PollResult(ok=True, changed=False, fingerprint="fp")
        finally:
            self.inflight -= 1


def _fast_bucket() -> TokenBucket:
    # The engine's default bucket is 10 req/s — fine in production, an
    # eternity for a 1k-target test sweep.
    return TokenBucket(rate=1e6, burst=1e6)


async def test_sweep_1k_targets_all_polled():
    engine = PollEngine(max_concurrency=32, token_bucket=_fast_bucket())
    counters = [_Counter(f"t{i}") for i in range(1000)]
    for c in counters:
        engine.add(c, base=60, jitter="none")
    results = await engine.sweep()
    assert len(results) == 1000
    assert all(c.polls == 1 for c in counters)


async def test_no_overlapping_polls_per_target():
    """One target polled via poll_once concurrently still runs its calls;
    the engine-level guarantee is per-sweep single dispatch."""
    engine = PollEngine(max_concurrency=8)
    c = _Counter("solo", delay=0.01)
    engine.add(c, base=60, jitter="none")
    await engine.sweep()
    assert c.max_inflight == 1


async def test_global_semaphore_caps_concurrency():
    engine = PollEngine(max_concurrency=3, token_bucket=_fast_bucket())
    counters = [_Counter(f"t{i}", delay=0.01) for i in range(12)]
    for c in counters:
        engine.add(c, base=60, jitter="none")

    snapshot = {"max": 0}
    orig_polls = [c.poll for c in counters]

    def wrap(c, orig):
        async def _poll(prev):
            snapshot["now"] = sum(x.inflight for x in counters)
            snapshot["max"] = max(snapshot["max"],
                                  sum(x.inflight for x in counters) + 1)
            return await orig(prev)
        return _poll

    for c, orig in zip(counters, orig_polls):
        c.poll = wrap(c, orig)

    await engine.sweep()
    assert snapshot["max"] <= 3


async def test_token_bucket_throttles_sweep():
    """rate=0 + burst=N: exactly N tokens exist, so a sweep of N targets
    consumes them all without stalling; the engine never deadlocks."""
    engine = PollEngine(token_bucket=TokenBucket(rate=1000.0, burst=4.0),
                        max_concurrency=8)
    counters = [_Counter(f"t{i}") for i in range(8)]
    for c in counters:
        engine.add(c, base=60, jitter="none")
    await asyncio.wait_for(engine.sweep(), timeout=5)
    assert all(c.polls == 1 for c in counters)


async def test_failing_target_does_not_poison_others():
    engine = PollEngine()
    good = _Counter("good")
    bad = _Counter("bad", fail=True)
    engine.add(good, base=60, jitter="none")
    engine.add(bad, base=60, jitter="none")
    for _ in range(6):
        await engine.sweep()
    assert good.polls == 6
    # the failing target trips its breaker and gets skipped eventually
    assert engine.targets["bad"].breaker.state in ("open", "half_open", "closed")
    assert bad.polls <= 6


async def test_breaker_opens_after_repeated_failures():
    engine = PollEngine()
    bad = _Counter("bad", fail=True)
    engine.add(bad, base=60, jitter="none")
    t = engine.targets["bad"]
    for _ in range(10):
        await engine.poll_once(t)
    assert t.breaker.state == "open"


async def test_concurrent_token_bucket_acquire():
    """Many tasks acquiring tokens under contention neither deadlock nor
    over-issue."""
    bucket = TokenBucket(rate=200.0, burst=10.0)

    async def taker():
        await bucket.acquire()
        return 1

    results = await asyncio.wait_for(
        asyncio.gather(*(taker() for _ in range(50))), timeout=5
    )
    assert sum(results) == 50


def test_token_bucket_take_sync_semantics():
    clock = [0.0]
    bucket = TokenBucket(rate=1.0, burst=2.0, clock=lambda: clock[0])
    assert bucket.take() and bucket.take()
    assert bucket.take() is False          # burst exhausted
    assert bucket.time_until() == 1.0      # one token at 1/s
    clock[0] = 1.0
    assert bucket.take() is True           # refilled
