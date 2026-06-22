"""Per-host learned rate governor (:class:`ujin.adapt.LearnedRateLimiter`).

All offline and deterministic: an in-memory :class:`SiteStore`, a stub robots
policy, and the conftest ``FakeClock`` (manual time, instant async sleep) prove
that a 429 raises the interval and throttles concurrency, that ``Crawl-delay``
floors the interval, that a clean host sits at baseline with full concurrency,
that the limiter recovers after errors clear, and that the async gate never
touches the wall clock.
"""
from __future__ import annotations

import asyncio
import inspect
import time

import pytest

from ujin.adapt import LearnedRateLimiter, SiteStore


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
class StubRobots:
    """Minimal ``crawl_delay(host) -> float | None`` lookup."""

    def __init__(self, delays: dict[str, float] | None = None):
        self._delays = delays or {}

    def crawl_delay(self, host: str):
        return self._delays.get(host)


class RecordingSleep:
    """Async sleep that records each duration and advances a FakeClock."""

    def __init__(self, clock):
        self._clock = clock
        self.calls: list[float] = []

    async def __call__(self, secs: float) -> None:
        self.calls.append(secs)
        await self._clock.sleep(secs)


@pytest.fixture
def store():
    s = SiteStore()
    try:
        yield s
    finally:
        s.close()


# --------------------------------------------------------------------------- #
# Exports stay additive
# --------------------------------------------------------------------------- #
def test_export_is_additive():
    import ujin.adapt as adapt

    assert "LearnedRateLimiter" in adapt.__all__
    assert hasattr(adapt, "LearnedRateLimiter")
    # neighbours untouched
    for name in (
        "AdaptiveInterval", "AIMDLimiter", "TokenBucket", "SiteStore",
        "derive_signals", "SignalAdvisor",
    ):
        assert name in adapt.__all__


def test_import_path():
    from ujin.adapt import LearnedRateLimiter as L  # noqa: F401

    assert L is LearnedRateLimiter


# --------------------------------------------------------------------------- #
# Clean host: baseline interval + full concurrency
# --------------------------------------------------------------------------- #
def test_clean_host_is_baseline(store):
    gov = LearnedRateLimiter(store, base_interval=2.0, max_concurrency=8)
    assert gov.interval_for("fresh.example") == pytest.approx(2.0)
    assert gov.concurrency_for("fresh.example") == 8
    assert gov.cooldown_for("fresh.example") == 0.0


def test_clean_host_zero_base_has_no_pacing(store):
    gov = LearnedRateLimiter(store, base_interval=0.0, max_concurrency=4)
    assert gov.interval_for("fresh.example") == 0.0
    assert gov.concurrency_for("fresh.example") == 4


def test_clean_responses_keep_baseline(store):
    gov = LearnedRateLimiter(store, base_interval=1.0, max_concurrency=8)
    for _ in range(5):
        gov.observe("h", status=200, latency=0.1)
    assert gov.interval_for("h") == pytest.approx(1.0)
    assert gov.concurrency_for("h") == 8


# --------------------------------------------------------------------------- #
# 429 -> interval up & concurrency throttled
# --------------------------------------------------------------------------- #
def test_429_raises_interval_and_throttles_concurrency(store):
    gov = LearnedRateLimiter(store, base_interval=1.0, max_concurrency=8)
    before_interval = gov.interval_for("h")
    before_conc = gov.concurrency_for("h")

    gov.observe("h", status=429)

    assert gov.interval_for("h") > before_interval
    assert gov.concurrency_for("h") < before_conc
    # the 429 is persisted durably
    assert store.get("h").rate_limit_count == 1


def test_repeated_429_compounds(store):
    gov = LearnedRateLimiter(store, base_interval=1.0, max_concurrency=8)
    gov.observe("h", status=429)
    one_interval = gov.interval_for("h")
    one_conc = gov.concurrency_for("h")
    gov.observe("h", status=429)
    assert gov.interval_for("h") > one_interval
    assert gov.concurrency_for("h") <= one_conc


def test_server_error_throttles_concurrency_only(store):
    gov = LearnedRateLimiter(store, base_interval=2.0, max_concurrency=8)
    gov.observe("h", status=503, error=True)
    # a non-429 error does not speed the interval up, but caps concurrency
    assert gov.interval_for("h") == pytest.approx(2.0)
    assert gov.concurrency_for("h") < 8
    assert store.get("h").error_count == 1


# --------------------------------------------------------------------------- #
# Crawl-delay floors the interval
# --------------------------------------------------------------------------- #
def test_robots_crawl_delay_floors_interval(store):
    robots = StubRobots({"slow.example": 5.0})
    gov = LearnedRateLimiter(store, robots=robots, base_interval=0.0)
    eff = gov.interval_for("slow.example")
    assert eff >= 5.0
    assert eff == pytest.approx(5.0)


def test_observed_crawl_delay_floors_interval(store):
    gov = LearnedRateLimiter(store, base_interval=0.0)
    gov.observe("h", status=200, crawl_delay=7.0)
    assert gov.interval_for("h") >= 7.0


def test_crawl_floor_holds_even_under_rate_limit(store):
    robots = StubRobots({"h": 10.0})
    gov = LearnedRateLimiter(store, robots=robots, base_interval=1.0)
    gov.observe("h", status=429)
    # interval is pushed up by the 429 but never drops below the robots floor
    assert gov.interval_for("h") >= 10.0


def test_interval_never_below_max_crawl_delay(store):
    """Acceptance: for every host with either Crawl-delay set, the effective
    interval is >= max(observed, robots) delay."""
    robots = StubRobots({"a": 4.0, "b": 9.0})
    gov = LearnedRateLimiter(store, robots=robots, base_interval=0.5)
    gov.observe("a", status=200, crawl_delay=2.0)  # observed 2, robots 4 -> 4
    gov.observe("b", status=429, crawl_delay=12.0)  # observed 12, robots 9 -> 12
    for host, floor in (("a", 4.0), ("b", 12.0)):
        assert gov.interval_for(host) >= floor


# --------------------------------------------------------------------------- #
# Recovery back to baseline after errors clear
# --------------------------------------------------------------------------- #
def test_recovery_after_429_clears(store):
    gov = LearnedRateLimiter(store, base_interval=2.0, max_concurrency=8)
    gov.observe("h", status=429)
    gov.observe("h", status=429)
    assert gov.interval_for("h") > 2.0
    assert gov.concurrency_for("h") < 8

    # a run of clean responses relaxes both back to baseline
    for _ in range(12):
        gov.observe("h", status=200, latency=0.05)

    assert gov.interval_for("h") == pytest.approx(2.0)
    assert gov.concurrency_for("h") == 8


def test_partial_recovery_is_monotonic(store):
    gov = LearnedRateLimiter(store, base_interval=2.0, max_concurrency=8)
    gov.observe("h", status=429)
    gov.observe("h", status=429)
    gov.observe("h", status=429)
    peak = gov.interval_for("h")
    gov.observe("h", status=200)
    mid = gov.interval_for("h")
    assert mid < peak
    assert mid >= 2.0


# --------------------------------------------------------------------------- #
# Warm start: persisted history seeds the live controllers
# --------------------------------------------------------------------------- #
def test_warm_start_from_persisted_state(store):
    # a previous process saw a 429 and a Crawl-delay for this host
    store.record("warm", status=429, rate_limited=1, crawl_delay=3.0)

    gov = LearnedRateLimiter(store, base_interval=0.0, max_concurrency=8)
    # seeded as rate-limited: interval honours the crawl floor, concurrency < full
    assert gov.interval_for("warm") >= 3.0
    assert gov.concurrency_for("warm") < 8


# --------------------------------------------------------------------------- #
# Async gate: paces the interval with no wall-clock sleep
# --------------------------------------------------------------------------- #
async def test_gate_paces_interval_without_real_sleep(store, fake_clock):
    sleep = RecordingSleep(fake_clock)
    robots = StubRobots({"slow.example": 5.0})
    gov = LearnedRateLimiter(
        store, robots=robots, base_interval=0.0, clock=fake_clock, sleep=sleep
    )

    wall_start = time.monotonic()
    async with gov.acquire("slow.example"):
        pass  # first acquire is free (token burst)
    assert sleep.calls == []  # nothing slept yet
    assert fake_clock.t == 0.0

    async with gov.acquire("slow.example"):
        pass  # must wait one interval
    wall_elapsed = time.monotonic() - wall_start

    assert sleep.calls and sleep.calls[-1] == pytest.approx(5.0, abs=1e-6)
    assert fake_clock.t == pytest.approx(5.0)  # only the fake clock advanced
    assert wall_elapsed < 0.5  # no real time was slept


async def test_gate_zero_interval_never_sleeps(store, fake_clock):
    sleep = RecordingSleep(fake_clock)
    gov = LearnedRateLimiter(
        store, base_interval=0.0, clock=fake_clock, sleep=sleep
    )
    for _ in range(3):
        async with gov.acquire("fast.example"):
            pass
    assert sleep.calls == []
    assert fake_clock.t == 0.0


async def test_gate_caps_concurrency(store, fake_clock):
    sleep = RecordingSleep(fake_clock)
    gov = LearnedRateLimiter(
        store, base_interval=0.0, clock=fake_clock, sleep=sleep, max_concurrency=2
    )

    g1 = await gov.acquire("h")
    g2 = await gov.acquire("h")  # at cap (2)

    third_entered = False

    async def third():
        nonlocal third_entered
        async with gov.acquire("h"):
            third_entered = True

    task = asyncio.create_task(third())
    for _ in range(3):
        await asyncio.sleep(0)  # let the task run as far as it can
    assert not third_entered  # blocked: host is at its concurrency cap

    await g1.release()  # free a slot
    await task
    assert third_entered

    await g2.release()
    assert sleep.calls == []  # no interval pacing at base 0


async def test_gate_await_then_release(store, fake_clock):
    sleep = RecordingSleep(fake_clock)
    gov = LearnedRateLimiter(
        store, base_interval=0.0, clock=fake_clock, sleep=sleep, max_concurrency=1
    )
    gate = await gov.acquire("h")  # bare-await entry holds a slot
    await gate.release()
    # releasing twice is a no-op (idempotent)
    await gate.release()
    # the slot is free again, so a fresh acquire succeeds immediately
    async with gov.acquire("h"):
        pass


# --------------------------------------------------------------------------- #
# Opt-in: the default scrape/poll path does not import the limiter
# --------------------------------------------------------------------------- #
def test_default_path_does_not_import_limiter():
    import ujin.engine as engine
    import ujin.scrape.service as service
    import ujin.poll as poll

    for mod in (engine, service, poll):
        src = inspect.getsource(mod)
        assert "LearnedRateLimiter" not in src
