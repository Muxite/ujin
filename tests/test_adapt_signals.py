"""Policy-signal derivation: pure HostRecord -> PolicySignals, plus the
read-only SignalAdvisor bridge over a SiteStore.

All offline and deterministic — signals do no I/O and hand-built ``HostRecord``s
are enough to exercise every rule.
"""
from __future__ import annotations

import inspect

import pytest

from ujin.adapt import (
    HostRecord,
    PolicySignals,
    SignalAdvisor,
    SiteStore,
    derive_signals,
)
from ujin.cache.hostpolicy import HostPolicy


# -- exports stay additive --------------------------------------------------- #

def test_all_exports_present_and_additive():
    import ujin.adapt as adapt

    # the new names are exported...
    for name in ("PolicySignals", "derive_signals", "SignalAdvisor"):
        assert name in adapt.__all__
        assert hasattr(adapt, name)
    # ...without dropping or renaming any pre-existing one.
    for name in (
        "AdaptiveInterval", "Backoff", "CircuitBreaker", "TokenBucket",
        "AIMDLimiter", "jitter", "SiteStore", "HostRecord",
    ):
        assert name in adapt.__all__
        assert hasattr(adapt, name)


# -- clean record defaults --------------------------------------------------- #

def test_clean_record_is_pristine():
    sig = derive_signals(HostRecord(host="example.com"))
    assert isinstance(sig, PolicySignals)
    assert sig.health == 1.0
    assert sig.should_cooldown is False
    assert sig.cooldown_secs == 0.0
    assert sig.rate_limited is False
    assert sig.concurrency_factor == 1.0
    assert sig.recommended_interval == 0.0


def test_clean_record_recommends_base_interval():
    sig = derive_signals(HostRecord(host="h"), base_interval=12.5)
    assert sig.recommended_interval == 12.5
    assert sig.health == 1.0
    assert sig.concurrency_factor == 1.0


def test_policy_signals_is_frozen():
    sig = derive_signals(HostRecord(host="h"))
    with pytest.raises(Exception):
        sig.health = 0.0  # type: ignore[misc]


# -- rate limiting ----------------------------------------------------------- #

def test_rate_limit_count_sets_flag_and_raises_interval():
    clean = derive_signals(HostRecord(host="h"), base_interval=2.0)
    limited = derive_signals(
        HostRecord(host="h", rate_limit_count=1), base_interval=2.0
    )
    assert limited.rate_limited is True
    # acceptance: strictly greater interval than the same call on a clean record
    assert limited.recommended_interval > clean.recommended_interval
    # and concurrency is throttled below the clean 1.0
    assert limited.concurrency_factor < 1.0
    assert limited.should_cooldown is True
    assert limited.health < clean.health


def test_last_status_429_sets_rate_limited_even_without_counter():
    sig = derive_signals(HostRecord(host="h", last_status=429), base_interval=0.0)
    assert sig.rate_limited is True
    # zero base still backs off to the absolute floor
    assert sig.recommended_interval >= 1.0
    assert sig.concurrency_factor < 1.0
    assert sig.health < 1.0


def test_rate_limit_interval_multiplier_caps():
    # many 429s saturate the growth multiplier (cap 4x) rather than exploding
    sig = derive_signals(
        HostRecord(host="h", rate_limit_count=50), base_interval=10.0
    )
    assert sig.recommended_interval == pytest.approx(40.0)  # 10 * cap(4.0)


def test_rate_limit_concurrency_clamps_to_floor():
    sig = derive_signals(HostRecord(host="h", rate_limit_count=50))
    assert sig.concurrency_factor == pytest.approx(0.25)


def test_more_rate_limits_throttle_concurrency_further():
    one = derive_signals(HostRecord(host="h", rate_limit_count=1))
    two = derive_signals(HostRecord(host="h", rate_limit_count=2))
    assert two.concurrency_factor < one.concurrency_factor


# -- crawl-delay flooring ---------------------------------------------------- #

def test_observed_crawl_delay_floors_interval():
    sig = derive_signals(HostRecord(host="h", crawl_delay=7.0), base_interval=1.0)
    # acceptance: crawl_delay>0 yields recommended_interval >= crawl_delay
    assert sig.recommended_interval >= 7.0
    assert sig.recommended_interval == pytest.approx(7.0)


def test_robots_crawl_delay_floors_interval():
    sig = derive_signals(
        HostRecord(host="h"), base_interval=0.5, robots_crawl_delay=4.0
    )
    assert sig.recommended_interval == pytest.approx(4.0)


def test_crawl_floor_takes_the_larger_of_the_two():
    sig = derive_signals(
        HostRecord(host="h", crawl_delay=3.0),
        base_interval=0.0,
        robots_crawl_delay=9.0,
    )
    assert sig.recommended_interval == pytest.approx(9.0)


def test_base_interval_wins_when_above_crawl_floor():
    sig = derive_signals(
        HostRecord(host="h", crawl_delay=2.0), base_interval=20.0
    )
    assert sig.recommended_interval == pytest.approx(20.0)


def test_rate_limit_and_crawl_delay_floor_compose():
    # interval is pushed up by rate limiting, then floored by crawl delay if higher
    sig = derive_signals(
        HostRecord(host="h", rate_limit_count=1, crawl_delay=100.0),
        base_interval=2.0,
    )
    assert sig.recommended_interval == pytest.approx(100.0)


# -- error pressure ---------------------------------------------------------- #

def test_error_count_lowers_health_and_raises_cooldown():
    low = derive_signals(HostRecord(host="h", error_count=1))
    high = derive_signals(HostRecord(host="h", error_count=5))
    assert high.health < low.health < 1.0
    assert high.cooldown_secs > low.cooldown_secs > 0.0
    assert low.should_cooldown is True
    # errors alone do not throttle concurrency (cooldown absorbs that pressure)
    assert low.concurrency_factor == 1.0
    assert low.rate_limited is False


def test_cooldown_caps():
    sig = derive_signals(HostRecord(host="h", error_count=1000))
    assert sig.cooldown_secs == pytest.approx(300.0)


def test_health_stays_in_unit_interval():
    sig = derive_signals(
        HostRecord(host="h", error_count=10_000, rate_limit_count=10_000)
    )
    assert 0.0 < sig.health < 1.0


# -- purity ------------------------------------------------------------------ #

def test_derive_signals_is_pure():
    rec = HostRecord(host="h", error_count=3, rate_limit_count=2, crawl_delay=1.5)
    a = derive_signals(rec, base_interval=4.0, robots_crawl_delay=2.0)
    b = derive_signals(rec, base_interval=4.0, robots_crawl_delay=2.0)
    assert a == b
    # record is frozen and untouched
    assert rec == HostRecord(
        host="h", error_count=3, rate_limit_count=2, crawl_delay=1.5
    )


# -- SignalAdvisor over an in-memory SiteStore ------------------------------- #

def test_advisor_default_host_is_pristine():
    store = SiteStore()
    try:
        advisor = SignalAdvisor(store)
        sig = advisor.for_host("never-seen.example")
        assert sig.health == 1.0
        assert sig.should_cooldown is False
        assert sig.recommended_interval == 0.0
    finally:
        store.close()


def test_advisor_reads_populated_host_without_mutating_store():
    store = SiteStore()
    try:
        store.record("busy.example", rate_limited=1, crawl_delay=5.0, error=2)
        before = store.get("busy.example")

        advisor = SignalAdvisor(store, base_interval=1.0)
        sig = advisor.for_host("busy.example")

        assert sig.rate_limited is True
        assert sig.recommended_interval >= 5.0
        assert sig.should_cooldown is True
        assert sig.health < 1.0

        # the advisor is read-only: the stored record is byte-identical after.
        assert store.get("busy.example") == before
    finally:
        store.close()


def test_advisor_per_call_overrides():
    store = SiteStore()
    try:
        advisor = SignalAdvisor(store, base_interval=1.0, robots_crawl_delay=2.0)
        # construction defaults
        assert advisor.for_host("h").recommended_interval == pytest.approx(2.0)
        # per-call override beats the construction default
        sig = advisor.for_host("h", base_interval=50.0, robots_crawl_delay=0.0)
        assert sig.recommended_interval == pytest.approx(50.0)
    finally:
        store.close()


# -- regression: default path is untouched, neighbours unchanged ------------- #

def test_default_scrape_and_poll_path_does_not_use_signals():
    """The signals layer is opt-in: the engine and scrape service must not
    reference it, so a no-config deploy behaves exactly as before."""
    import ujin.engine as engine
    import ujin.scrape.service as service

    for mod in (engine, service):
        src = inspect.getsource(mod)
        assert "signals" not in src
        assert "SignalAdvisor" not in src
        assert "derive_signals" not in src


def test_hostpolicy_behavior_unchanged():
    hp = HostPolicy(cooldown_secs=60)
    url = "https://h.example/x"
    assert hp.cooldown_remaining(url) == 0.0
    hp.record_failure(url, status=429)
    assert hp.cooldown_remaining(url) > 0.0
    hp.record_success(url)
    assert hp.cooldown_remaining(url) == 0.0


def test_site_store_behavior_unchanged():
    store = SiteStore()
    try:
        # unknown host -> zero-valued record
        assert store.get("u") == HostRecord(host="u")
        # counters accumulate, gauges overwrite
        store.record("u", error=1)
        store.record("u", error=2, status=500)
        rec = store.get("u")
        assert rec.error_count == 3
        assert rec.last_status == 500
    finally:
        store.close()
