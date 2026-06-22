"""Offline tests for PollEngine(adaptive=True, respect_robots=True).

All scenarios use injected fetchers and the shared fake_clock — no network.

1. robots Crawl-delay floors the effective per-host interval.
2. A disallowed URL is skipped (pollable.poll() never called); result is ok=True.
3. An allowed URL on the same site is fetched normally.
4. With respect_robots=False (default) the _robots_adaptor is None and the flag
   has zero effect on the poll path.
"""
from __future__ import annotations

import random

import pytest

from ujin.adapt.concurrency import TokenBucket
from ujin.engine import PollEngine
from ujin.poll.base import PollResult


class _CountingPollable:
    """Records how many times poll() is called; returns a canned result."""

    def __init__(self, key: str, url: str = "", *, status: int = 200) -> None:
        self.key = key
        self.url = url
        self._status = status
        self.call_count = 0

    async def poll(self, prev: PollResult | None) -> PollResult:
        self.call_count += 1
        return PollResult(
            ok=True,
            changed=True,
            fingerprint=f"fp-{self.call_count}",
            payload="x",
            status=self._status,
            latency_ms=5,
        )


def _make_engine(
    fake_clock,
    robots_txt: str,
    *,
    base_interval: float = 0.0,
) -> tuple[PollEngine, dict]:
    """Return (engine, fetch_counts) with an injected robots fetcher."""
    fetch_counts: dict[str, int] = {}

    async def fake_fetcher(url: str) -> str:
        fetch_counts[url] = fetch_counts.get(url, 0) + 1
        return robots_txt

    eng = PollEngine(
        token_bucket=TokenBucket(rate=1e9, burst=1e9, clock=fake_clock),
        clock=fake_clock,
        sleep=fake_clock.sleep,
        rng=random.Random(1),
        adaptive=True,
        adaptive_base_interval=base_interval,
        respect_robots=True,
        robots_fetcher=fake_fetcher,
    )
    return eng, fetch_counts


# --------------------------------------------------------------------------- #
# 1. robots Crawl-delay floors the effective per-host interval
# --------------------------------------------------------------------------- #
async def test_crawl_delay_floors_interval(fake_clock):
    """A Crawl-delay: 5 in robots.txt must floor interval_for() and last_delay."""
    robots_txt = "User-agent: *\nCrawl-delay: 5\n"
    eng, _ = _make_engine(fake_clock, robots_txt, base_interval=1.0)

    p = _CountingPollable("example.com", url="https://example.com/feed")
    target = eng.add(p, base=1.0, jitter="none")

    await eng.poll_once(target)

    assert p.call_count == 1, "allowed URL should be fetched"
    assert eng.limiter.interval_for("example.com") >= 5.0
    assert target.last_delay >= 5.0


# --------------------------------------------------------------------------- #
# 2. disallowed URL → pollable.poll() never called; result is a clean skip
# --------------------------------------------------------------------------- #
async def test_disallowed_url_not_fetched(fake_clock):
    """A Disallow: /private robots rule must prevent poll() from being called."""
    robots_txt = "User-agent: *\nDisallow: /private\n"
    eng, _ = _make_engine(fake_clock, robots_txt)

    p = _CountingPollable("example.com", url="https://example.com/private/page")
    target = eng.add(p, base=10.0, jitter="none")

    result = await eng.poll_once(target)

    assert result.ok is True
    assert result.changed is False
    assert p.call_count == 0, "poll() must not be called for a disallowed URL"
    assert target.polls == 1


# --------------------------------------------------------------------------- #
# 3. allowed URL on the same site is fetched normally
# --------------------------------------------------------------------------- #
async def test_allowed_url_is_fetched(fake_clock):
    """A URL outside the disallowed prefix must still be fetched."""
    robots_txt = "User-agent: *\nDisallow: /private\n"
    eng, _ = _make_engine(fake_clock, robots_txt)

    p = _CountingPollable("example.com", url="https://example.com/public/page")
    target = eng.add(p, base=10.0, jitter="none")

    result = await eng.poll_once(target)

    assert result.ok is True
    assert p.call_count == 1, "allowed URL must be fetched"


# --------------------------------------------------------------------------- #
# 4. respect_robots=False (default) — adaptor is None, path is byte-identical
# --------------------------------------------------------------------------- #
async def test_respect_robots_off_is_inert(fake_clock):
    """Default path: _robots_adaptor is None; disallowed path is fetched anyway."""
    eng = PollEngine(
        token_bucket=TokenBucket(rate=1e9, burst=1e9, clock=fake_clock),
        clock=fake_clock,
        sleep=fake_clock.sleep,
        rng=random.Random(1),
        adaptive=True,
    )
    assert eng._robots_adaptor is None

    p = _CountingPollable("example.com", url="https://example.com/private/page")
    target = eng.add(p, base=10.0, jitter="none")
    assert target.robots_origin == "", "origin not set when flag is off"

    result = await eng.poll_once(target)
    assert result.ok is True
    assert p.call_count == 1, "no skip when respect_robots is off"
