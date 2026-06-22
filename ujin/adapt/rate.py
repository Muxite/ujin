"""Per-host *learned* rate governor.

The pieces already in this package each solve one slice of polite polling:
:class:`~ujin.adapt.site_store.SiteStore` *remembers* per-host observations,
:func:`~ujin.adapt.signals.derive_signals` *interprets* one record into
recommendations, and :class:`~ujin.adapt.interval.AdaptiveInterval` /
:class:`~ujin.adapt.concurrency.AIMDLimiter` / :class:`~ujin.adapt.concurrency.TokenBucket`
are the *control primitives*. :class:`LearnedRateLimiter` is the thin composition
that wires them into a single, self-calibrating gate:

* it reads persisted state through the :class:`~ujin.adapt.signals.SignalAdvisor`
  bridge and composes ``derive_signals`` output (``recommended_interval``,
  ``concurrency_factor``, ``rate_limited``, ``cooldown_secs``) with an optional
  ``robots`` ``Crawl-delay`` to *seed* and *floor* the live controllers;
* :meth:`interval_for` / :meth:`concurrency_for` report the current effective
  cadence and concurrency, the effective interval **never** dropping below
  ``max(observed Crawl-delay, robots.crawl_delay(host))``;
* :meth:`acquire` is an async gate (also usable as ``async with``) that paces a
  per-host :class:`TokenBucket` to the effective interval and caps in-flight
  requests at the effective concurrency;
* :meth:`observe` feeds each response back — into the durable ``SiteStore`` *and*
  the in-process primitives — so a 429 raises the interval and throttles
  concurrency while a run of clean responses relaxes both toward baseline.

Everything is additive and **opt-in**. Importing or constructing this class does
not touch the scrape/poll path; a no-config deploy behaves exactly as before. The
clock and the async ``sleep`` are injectable, so the gate is fully deterministic
and never touches the wall clock in tests.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

from ujin.adapt.concurrency import AIMDLimiter, TokenBucket
from ujin.adapt.interval import AdaptiveInterval
from ujin.adapt.signals import SignalAdvisor
from ujin.adapt.site_store import SiteStore

# Smallest non-zero rate-limit interval, so backing off works even from a zero
# ``base_interval`` (mirrors ``signals._MIN_RATE_LIMIT_INTERVAL``).
_MIN_RL_STEP = 1.0
# Cap the learned per-host interval so a pathological host can't grow it forever.
_DEFAULT_MAX_INTERVAL = 3600.0
# Default "full" per-host concurrency a healthy host is allowed.
_DEFAULT_MAX_CONCURRENCY = 8
# Float slack when deciding the learned penalty has fully relaxed.
_EPS = 1e-9


class _Robots(Protocol):
    """Anything exposing a per-host ``Crawl-delay`` lookup (e.g. a small adapter
    over :class:`ujin.robots.RobotsPolicy`)."""

    def crawl_delay(self, host: str) -> Optional[float]:  # pragma: no cover - proto
        ...


@dataclass
class _HostGov:
    """Live in-process controllers for one host.

    ``penalty`` (an :class:`AdaptiveInterval`) carries the *learned* interval —
    grown on a 429, shrunk back toward its floor on clean responses. ``aimd`` is
    the concurrency target. ``bucket`` paces the async gate. ``rl_active`` tracks
    whether the host is currently backed off (so a never-throttled host reports
    exactly ``base_interval``). ``inflight`` + ``cond`` implement the concurrency
    slot.
    """

    aimd: AIMDLimiter
    penalty: AdaptiveInterval
    bucket: TokenBucket
    rl_active: bool = False
    inflight: int = 0
    cond: asyncio.Condition = field(default_factory=asyncio.Condition)


class LearnedRateLimiter:
    """Self-calibrating, per-host rate + concurrency governor.

    Parameters
    ----------
    store:
        A :class:`~ujin.adapt.site_store.SiteStore` (or anything with the same
        ``get(host)`` / ``record(host, **signals)`` surface). Persisted state both
        seeds the live controllers on first use (warm restart) and is updated by
        :meth:`observe`.
    robots:
        Optional object exposing ``crawl_delay(host) -> float | None`` whose value
        floors the effective interval.
    base_interval:
        Cadence to recommend for a healthy host (seconds). Defaults to ``0.0``
        (no artificial pacing) so the default behavior is unchanged.
    clock:
        Monotonic-style ``() -> float`` used by the pacing bucket. Injectable for
        deterministic tests.
    sleep:
        ``async (secs) -> None`` used to wait out the interval. Injectable so tests
        can advance a fake clock instead of sleeping for real.
    max_concurrency:
        The "full" per-host concurrency a healthy host is allowed.
    """

    def __init__(
        self,
        store: SiteStore,
        robots: Optional[_Robots] = None,
        *,
        base_interval: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], "object"] = asyncio.sleep,
        max_concurrency: int = _DEFAULT_MAX_CONCURRENCY,
        max_interval: float = _DEFAULT_MAX_INTERVAL,
    ):
        self._store = store
        self._robots = robots
        self._base_interval = max(0.0, base_interval)
        self._clock = clock
        self._sleep = sleep
        self._max_concurrency = max(1, int(max_concurrency))
        self._max_interval = max_interval
        self._rl_floor = max(self._base_interval, _MIN_RL_STEP)
        self._advisor = SignalAdvisor(store, base_interval=self._base_interval)
        self._govs: dict[str, _HostGov] = {}

    # ------------------------------------------------------------------ helpers

    def _robots_delay(self, host: str) -> Optional[float]:
        if self._robots is None:
            return None
        try:
            return self._robots.crawl_delay(host)
        except Exception:  # pragma: no cover - defensive; robots is user-supplied
            return None

    def _crawl_floor(self, host: str) -> float:
        """Hard interval floor: the larger of observed and robots ``Crawl-delay``."""
        observed = self._store.get(host).crawl_delay
        robots = self._robots_delay(host) or 0.0
        return max(observed, robots)

    def _gov(self, host: str) -> _HostGov:
        """Return (lazily creating + seeding) the live controllers for ``host``."""
        gov = self._govs.get(host)
        if gov is None:
            sig = self._advisor.for_host(
                host, robots_crawl_delay=self._robots_delay(host)
            )
            seed_interval = max(self._rl_floor, sig.recommended_interval)
            penalty = AdaptiveInterval(
                base=seed_interval,
                min_interval=self._rl_floor,
                max_interval=self._max_interval,
            )
            limit = max(1, round(self._max_concurrency * sig.concurrency_factor))
            aimd = AIMDLimiter(
                limit=limit, min_limit=1, max_limit=self._max_concurrency
            )
            bucket = TokenBucket(rate=1.0, burst=1.0, clock=self._clock)
            gov = _HostGov(
                aimd=aimd,
                penalty=penalty,
                bucket=bucket,
                rl_active=sig.rate_limited,
            )
            self._govs[host] = gov
        return gov

    # ------------------------------------------------------------------ queries

    def interval_for(self, host: str) -> float:
        """Effective seconds-between-requests for ``host``.

        Always at least ``max(base_interval, observed Crawl-delay, robots
        Crawl-delay)``; raised further by the learned penalty while the host is
        backed off.
        """
        gov = self._gov(host)
        floor = max(self._base_interval, self._crawl_floor(host))
        if gov.rl_active:
            return max(gov.penalty.current, floor)
        return floor

    def concurrency_for(self, host: str) -> int:
        """Effective max in-flight requests for ``host`` (>= 1)."""
        return self._gov(host).aimd.value

    def cooldown_for(self, host: str) -> float:
        """Suggested cooldown (seconds) for ``host`` from the persisted signals."""
        return self._advisor.for_host(
            host, robots_crawl_delay=self._robots_delay(host)
        ).cooldown_secs

    # ------------------------------------------------------------------ feedback

    def observe(
        self,
        host: str,
        *,
        status: Optional[int] = None,
        latency: Optional[float] = None,
        error: bool = False,
        crawl_delay: Optional[float] = None,
    ):
        """Record one observed response and self-calibrate.

        Persists the observation to the store *and* nudges the in-process
        controllers: a 429 raises the learned interval and halves concurrency; a
        clean (2xx/3xx, no error) response relaxes the interval toward baseline and
        additively grows concurrency back; a non-429 error throttles concurrency
        without speeding up. Returns the updated :class:`HostRecord`.
        """
        gov = self._gov(host)
        is_429 = status == 429
        is_error = error or (status is not None and status >= 500)
        is_clean = (not error) and (status is None or 200 <= status < 400)

        # -- durable store update (keeps the record warm for restart) ---------- #
        signals: dict[str, float] = {}
        if status is not None:
            signals["status"] = status
        if latency is not None:
            signals["latency"] = latency
        if crawl_delay is not None:
            signals["crawl_delay"] = crawl_delay
        if is_429:
            signals["rate_limited"] = 1
        elif is_error:
            signals["error"] = 1
        record = self._store.record(host, **signals)

        # -- live controllers -------------------------------------------------- #
        if is_429:
            gov.aimd.on_failure()
            gov.rl_active = True
            gov.penalty.next(changed=False)  # grow == back off
        elif is_error:
            gov.aimd.on_failure()
        elif is_clean:
            gov.aimd.on_success()
            if gov.rl_active:
                gov.penalty.next(changed=True)  # shrink == relax toward base
                if gov.penalty.current <= self._rl_floor + _EPS:
                    gov.rl_active = False
        return record

    # ------------------------------------------------------------------ gate

    def acquire(self, host: str) -> "_Gate":
        """Return an async gate for ``host``.

        Use as ``async with limiter.acquire(host): ...`` (recommended) — entry
        paces the effective interval and reserves a concurrency slot, exit releases
        the slot. ``await limiter.acquire(host)`` does the same entry and returns
        the gate; call ``await gate.release()`` when done.
        """
        return _Gate(self, host)

    async def _gate_enter(self, host: str) -> None:
        gov = self._gov(host)
        # Pace to the effective interval via the per-host token bucket.
        interval = self.interval_for(host)
        if interval > 0:
            gov.bucket.rate = 1.0 / interval
            await gov.bucket.acquire(sleep=self._sleep)
        # Reserve a concurrency slot, waiting if the host is at its cap.
        async with gov.cond:
            while gov.inflight >= self.concurrency_for(host):
                await gov.cond.wait()
            gov.inflight += 1

    async def _gate_release(self, host: str) -> None:
        gov = self._gov(host)
        async with gov.cond:
            if gov.inflight > 0:
                gov.inflight -= 1
            gov.cond.notify()


class _Gate:
    """Async gate returned by :meth:`LearnedRateLimiter.acquire`.

    Supports both ``async with`` and bare ``await`` usage.
    """

    def __init__(self, limiter: LearnedRateLimiter, host: str):
        self._limiter = limiter
        self._host = host
        self._held = False

    async def _enter(self) -> "_Gate":
        await self._limiter._gate_enter(self._host)
        self._held = True
        return self

    async def __aenter__(self) -> "_Gate":
        return await self._enter()

    async def __aexit__(self, *exc) -> bool:
        await self.release()
        return False

    def __await__(self):
        return self._enter().__await__()

    async def release(self) -> None:
        if self._held:
            self._held = False
            await self._limiter._gate_release(self._host)
