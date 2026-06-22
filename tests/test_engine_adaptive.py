"""Opt-in adaptive wiring in :class:`ujin.engine.PollEngine`.

Proves three things, all offline + deterministic with the shared ``fake_clock``:

1. Default path (``adaptive`` unset/false) constructs **no** SiteStore and **no**
   LearnedRateLimiter and makes no extra calls — behaviour unchanged from before.
2. With ``adaptive=True`` the engine records per-host state in its SiteStore and
   paces each target's next interval via ``LearnedRateLimiter.interval_for``.
3. A host returning HTTP 429 earns a strictly longer next interval than a clean
   host.
"""
from __future__ import annotations

import random

import pytest

import ujin.engine as engine_mod
from ujin.adapt.concurrency import TokenBucket
from ujin.adapt.site_store import HostRecord
from ujin.engine import PollEngine
from ujin.poll.base import PollResult


class FakePollable:
    """Returns a canned :class:`PollResult` so we control status/latency.

    ``key`` doubles as a host label; the engine's ``_host_for`` parses it.
    """

    def __init__(self, key: str, *, status: int = 200) -> None:
        self.key = key
        self._status = status

    async def poll(self, prev: PollResult | None) -> PollResult:
        if self._status == 429:
            return PollResult(ok=False, status=429, error="http 429", latency_ms=5)
        return PollResult(
            ok=True, changed=True, fingerprint=f"fp-{self._status}",
            payload="x", status=self._status, latency_ms=5,
        )


def _engine(fake_clock, **kw) -> PollEngine:
    return PollEngine(
        token_bucket=TokenBucket(rate=1e9, burst=1e9, clock=fake_clock),
        clock=fake_clock,
        sleep=fake_clock.sleep,
        rng=random.Random(1),
        **kw,
    )


# --------------------------------------------------------------------------- #
# 1. default path is inert — nothing adaptive is constructed
# --------------------------------------------------------------------------- #
async def test_default_path_constructs_no_adaptive(fake_clock, monkeypatch):
    """No flag → no SiteStore, no LearnedRateLimiter, no extra construction."""

    def _boom(*a, **k):  # pragma: no cover - must never be called
        raise AssertionError("adaptive machinery constructed on the default path")

    monkeypatch.setattr(engine_mod, "SiteStore", _boom)
    monkeypatch.setattr(engine_mod, "LearnedRateLimiter", _boom)

    eng = _engine(fake_clock)  # adaptive defaults to False
    assert eng.adaptive is False
    assert eng.site_store is None
    assert eng.limiter is None

    target = eng.add(FakePollable("feed.example"), base=10, jitter="none")
    assert target.host == ""  # host identity not computed when inert
    res = await eng.poll_once(target)  # runs without touching the booby-trapped ctors
    assert res.ok


# --------------------------------------------------------------------------- #
# 2. adaptive path records per-host state and paces via interval_for
# --------------------------------------------------------------------------- #
async def test_adaptive_records_host_state_and_paces(fake_clock):
    eng = _engine(fake_clock, adaptive=True, adaptive_base_interval=2.0)
    assert eng.adaptive is True
    assert eng.site_store is not None and eng.limiter is not None

    target = eng.add(FakePollable("feed.example"), base=10, jitter="none")
    assert target.host == "feed.example"

    await eng.poll_once(target)

    # SiteStore now holds a non-default record for the host.
    rec = eng.site_store.get("feed.example")
    assert rec != HostRecord(host="feed.example")
    assert rec.last_status == 200
    assert rec.last_latency == pytest.approx(0.005)

    # The engine paced the next interval by the learned cadence (>= base 2.0).
    assert eng.limiter.interval_for("feed.example") == pytest.approx(2.0)
    assert target.last_delay >= 2.0
    assert target.next_due >= 2.0


# --------------------------------------------------------------------------- #
# 3. a 429 host gets a strictly longer next interval than a clean host
# --------------------------------------------------------------------------- #
async def test_429_host_paces_slower_than_clean_host(fake_clock):
    eng = _engine(fake_clock, adaptive=True)  # base_interval 0.0 (default)
    bad = eng.add(FakePollable("rate.example", status=429), base=5, jitter="none")
    good = eng.add(FakePollable("calm.example", status=200), base=5, jitter="none")

    await eng.poll_once(bad)
    await eng.poll_once(good)

    bad_iv = eng.limiter.interval_for("rate.example")
    good_iv = eng.limiter.interval_for("calm.example")
    assert bad_iv > good_iv  # 429 backs off above a clean host's floor of 0
    assert good_iv == pytest.approx(0.0)

    # The 429 is durably remembered, and the engine floored its next delay by the
    # learned interval.
    assert eng.site_store.get("rate.example").rate_limit_count == 1
    assert bad.last_delay >= bad_iv - 1e-9


# --------------------------------------------------------------------------- #
# host derivation: URL-bearing pollables collapse onto their origin
# --------------------------------------------------------------------------- #
def test_host_for_prefers_url_then_key():
    class _UrlPollable:
        url = "https://news.example.com/feed?page=2"
        key = "news"

    assert PollEngine._host_for(_UrlPollable()) == "news.example.com"
    assert PollEngine._host_for(FakePollable("plain.host")) == "plain.host"
