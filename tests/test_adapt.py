"""Adaptive interval, jitter, backoff, and concurrency smoothing."""
from __future__ import annotations

import random

import pytest

from ujin.adapt import jitter
from ujin.adapt.backoff import Backoff, CircuitBreaker
from ujin.adapt.concurrency import AIMDLimiter, TokenBucket
from ujin.adapt.interval import AdaptiveInterval


# -- AdaptiveInterval -------------------------------------------------------- #

def test_interval_grows_when_unchanged():
    iv = AdaptiveInterval(base=10, min_interval=1, max_interval=1000, grow=2.0)
    assert iv.next(changed=False) == 20
    assert iv.next(changed=False) == 40


def test_interval_shrinks_when_changed():
    iv = AdaptiveInterval(base=10, min_interval=1, max_interval=1000, shrink=0.5)
    assert iv.next(changed=True) == 5
    assert iv.next(changed=True) == 2.5


def test_interval_clamps():
    iv = AdaptiveInterval(base=10, min_interval=4, max_interval=40, grow=10, shrink=0.01)
    assert iv.next(changed=False) == 40  # clamped to max
    assert iv.next(changed=True) == 4    # clamped to min


def test_interval_validates():
    with pytest.raises(ValueError):
        AdaptiveInterval(base=1, grow=0.9)
    with pytest.raises(ValueError):
        AdaptiveInterval(base=1, shrink=1.5)


# -- jitter ------------------------------------------------------------------ #

def test_full_jitter_bounds():
    rng = random.Random(0)
    for _ in range(100):
        assert 0 <= jitter.full(10, rng=rng) <= 10


def test_equal_jitter_keeps_floor():
    rng = random.Random(0)
    for _ in range(100):
        v = jitter.equal(10, rng=rng)
        assert 5 <= v <= 10


def test_decorrelated_respects_cap():
    rng = random.Random(0)
    for _ in range(100):
        assert jitter.decorrelated(1, 100, cap=20, rng=rng) <= 20


def test_jitter_deterministic_with_seed():
    a = [jitter.full(10, rng=random.Random(42)) for _ in range(5)]
    b = [jitter.full(10, rng=random.Random(42)) for _ in range(5)]
    assert a == b


def test_apply_none_is_identity():
    assert jitter.apply(7.0, "none") == 7.0


# -- Backoff ----------------------------------------------------------------- #

def test_backoff_exponential():
    b = Backoff(base=1, factor=2, cap=100)
    assert b.on_failure() == 1
    assert b.on_failure() == 2
    assert b.on_failure() == 4
    b.on_success()
    assert b.failures == 0
    assert b.on_failure() == 1


def test_backoff_honors_retry_after_capped():
    b = Backoff(cap=30)
    assert b.on_failure(retry_after=10) == 10
    assert b.on_failure(retry_after=999) == 30


# -- CircuitBreaker ---------------------------------------------------------- #

def test_circuit_opens_and_half_opens():
    t = [0.0]
    cb = CircuitBreaker(threshold=3, cooldown=100, clock=lambda: t[0])
    assert cb.allow() and cb.state == "closed"
    cb.on_failure(); cb.on_failure(); cb.on_failure()
    assert cb.state == "open" and not cb.allow()
    t[0] = 101
    assert cb.state == "half_open" and cb.allow()
    cb.on_success()
    assert cb.state == "closed"


def test_circuit_reopens_on_probe_failure():
    t = [0.0]
    cb = CircuitBreaker(threshold=1, cooldown=10, clock=lambda: t[0])
    cb.on_failure()
    assert cb.state == "open"
    t[0] = 11
    assert cb.state == "half_open"
    cb.on_failure()  # probe fails
    assert cb.state == "open"


# -- TokenBucket / AIMD ------------------------------------------------------ #

def test_token_bucket_take_and_refill():
    t = [0.0]
    tb = TokenBucket(rate=2, burst=2, clock=lambda: t[0])
    assert tb.take() and tb.take()      # burst of 2
    assert not tb.take()                # empty
    t[0] = 0.5                          # 0.5s * 2/s = 1 token
    assert tb.take()
    assert tb.time_until() == pytest.approx(0.5, abs=1e-6)


async def test_token_bucket_acquire_waits():
    t = [0.0]
    slept = []

    async def fake_sleep(d):
        slept.append(d)
        t[0] += d

    tb = TokenBucket(rate=1, burst=1, clock=lambda: t[0])
    await tb.acquire(sleep=fake_sleep)  # first is free
    await tb.acquire(sleep=fake_sleep)  # must wait ~1s
    assert sum(slept) == pytest.approx(1.0, abs=1e-6)


def test_aimd():
    a = AIMDLimiter(limit=4, min_limit=1, max_limit=10, increase=1, decrease=0.5)
    assert a.on_success() == 5
    assert a.on_failure() == 2   # 5 * 0.5 -> 2 (int)
    for _ in range(20):
        a.on_failure()
    assert a.value == 1          # floored at min
