"""Offline deterministic tests for StrategyFeedback / StrategyOutcome."""

from __future__ import annotations

import pytest

from ujin.adapt import StrategyFeedback, StrategyOutcome
from ujin.adapt.site_store import HostRecord

HTTP = ("http", "html")
OBSCURA = ("obscura", "js")
BROWSER = ("browser", "html")


# --------------------------------------------------------------------------- #
# Basic record / outcome structure
# --------------------------------------------------------------------------- #

def test_record_returns_outcome():
    fb = StrategyFeedback()
    out = fb.record("example.com", HTTP, ok=True, latency=0.1)
    assert isinstance(out, StrategyOutcome)
    assert out.host == "example.com"
    assert out.strategy == HTTP
    assert out.attempts == 1
    assert out.successes == 1
    assert out.failures == 0
    assert out.last_latency == pytest.approx(0.1)


def test_record_failure():
    fb = StrategyFeedback()
    out = fb.record("example.com", HTTP, ok=False, latency=0.5)
    assert out.attempts == 1
    assert out.successes == 0
    assert out.failures == 1


def test_counters_accumulate():
    fb = StrategyFeedback()
    fb.record("example.com", HTTP, ok=True, latency=0.1)
    fb.record("example.com", HTTP, ok=True, latency=0.2)
    out = fb.record("example.com", HTTP, ok=False, latency=0.3)
    assert out.attempts == 3
    assert out.successes == 2
    assert out.failures == 1


def test_strategies_tracked_independently():
    fb = StrategyFeedback()
    fb.record("example.com", HTTP, ok=True, latency=0.1)
    fb.record("example.com", HTTP, ok=True, latency=0.1)
    out_obs = fb.record("example.com", OBSCURA, ok=False, latency=0.5)
    assert out_obs.attempts == 1
    assert out_obs.successes == 0
    assert out_obs.failures == 1


def test_p50_latency_median():
    fb = StrategyFeedback()
    for lat in [0.1, 0.3, 0.5, 0.7, 0.9]:
        out = fb.record("example.com", HTTP, ok=True, latency=lat)
    assert out.p50_latency == pytest.approx(0.5)


def test_last_seen_from_clock():
    t = 1000.0
    fb = StrategyFeedback(clock=lambda: t)
    out = fb.record("example.com", HTTP, ok=True, latency=0.1)
    assert out.last_seen == pytest.approx(1000.0)


def test_last_latency_overwritten():
    fb = StrategyFeedback()
    fb.record("example.com", HTTP, ok=True, latency=0.1)
    out = fb.record("example.com", HTTP, ok=True, latency=0.9)
    assert out.last_latency == pytest.approx(0.9)


# --------------------------------------------------------------------------- #
# recommend()
# --------------------------------------------------------------------------- #

def test_recommend_unseen_host_none():
    fb = StrategyFeedback()
    assert fb.recommend("unknown.host") is None


def test_recommend_single_strategy():
    fb = StrategyFeedback()
    fb.record("example.com", HTTP, ok=True, latency=0.1)
    assert fb.recommend("example.com") == HTTP


def test_recommend_highest_success_rate():
    fb = StrategyFeedback()
    # HTTP: 2/3 successes ≈ 0.667
    for _ in range(2):
        fb.record("example.com", HTTP, ok=True, latency=0.1)
    fb.record("example.com", HTTP, ok=False, latency=0.1)
    # OBSCURA: 3/3 successes = 1.0
    for _ in range(3):
        fb.record("example.com", OBSCURA, ok=True, latency=0.2)
    assert fb.recommend("example.com") == OBSCURA


def test_recommend_tie_breaking_by_attempts():
    fb = StrategyFeedback()
    # Both 100% success rate; HTTP has more attempts → preferred
    fb.record("example.com", HTTP, ok=True, latency=0.1)
    fb.record("example.com", HTTP, ok=True, latency=0.1)
    fb.record("example.com", OBSCURA, ok=True, latency=0.1)
    assert fb.recommend("example.com") == HTTP


def test_recommend_tie_breaking_lexicographic():
    fb = StrategyFeedback()
    # Equal success rate AND equal attempts → lexicographic (backend, render_mode)
    # BROWSER = ("browser", "html"), HTTP = ("http", "html")
    # "browser" < "http" → BROWSER wins
    fb.record("example.com", HTTP, ok=True, latency=0.1)
    fb.record("example.com", BROWSER, ok=True, latency=0.1)
    assert fb.recommend("example.com") == BROWSER


def test_recommend_is_deterministic():
    fb = StrategyFeedback()
    fb.record("example.com", HTTP, ok=True, latency=0.1)
    fb.record("example.com", BROWSER, ok=True, latency=0.1)
    first = fb.recommend("example.com")
    for _ in range(5):
        assert fb.recommend("example.com") == first


def test_recommend_isolated_by_host():
    fb = StrategyFeedback()
    fb.record("a.com", HTTP, ok=True, latency=0.1)
    fb.record("b.com", OBSCURA, ok=True, latency=0.2)
    assert fb.recommend("a.com") == HTTP
    assert fb.recommend("b.com") == OBSCURA


def test_recommend_updates_after_new_records():
    fb = StrategyFeedback()
    fb.record("example.com", HTTP, ok=True, latency=0.1)
    assert fb.recommend("example.com") == HTTP
    # Now OBSCURA accumulates more successes
    for _ in range(5):
        fb.record("example.com", OBSCURA, ok=True, latency=0.1)
    assert fb.recommend("example.com") == OBSCURA


# --------------------------------------------------------------------------- #
# Signal-driven penalty
# --------------------------------------------------------------------------- #

def test_is_penalized_clean_record_false():
    fb = StrategyFeedback()
    record = HostRecord(host="example.com")  # zero-valued → health == 1.0
    assert fb.is_penalized("example.com", HTTP, record) is False


def test_is_penalized_rate_limited_true():
    fb = StrategyFeedback()
    record = HostRecord(host="example.com", rate_limit_count=1)
    assert fb.is_penalized("example.com", HTTP, record) is True


def test_is_penalized_status_429_true():
    fb = StrategyFeedback()
    record = HostRecord(host="example.com", last_status=429)
    assert fb.is_penalized("example.com", HTTP, record) is True


def test_is_penalized_low_health_true():
    fb = StrategyFeedback()
    # error_count=10 → penalty = 0.5*10 = 5 → health = 1/6 ≈ 0.167 < 0.5
    record = HostRecord(host="example.com", error_count=10)
    assert fb.is_penalized("example.com", HTTP, record) is True


def test_is_penalized_moderate_errors_not_penalized():
    fb = StrategyFeedback()
    # error_count=1 → penalty = 0.5 → health = 1/1.5 ≈ 0.667 > 0.5 → not penalized
    record = HostRecord(host="example.com", error_count=1)
    assert fb.is_penalized("example.com", HTTP, record) is False


def test_is_penalized_same_for_all_strategies():
    fb = StrategyFeedback()
    record = HostRecord(host="example.com", rate_limit_count=2)
    assert fb.is_penalized("example.com", HTTP, record) is True
    assert fb.is_penalized("example.com", OBSCURA, record) is True
    assert fb.is_penalized("example.com", BROWSER, record) is True


def test_is_penalized_no_store_io(tmp_path):
    db_path = tmp_path / "strategy.db"
    fb = StrategyFeedback(store=db_path)
    fb.close()
    # After close() any DB access would raise sqlite3.ProgrammingError —
    # is_penalized must still succeed because it performs no I/O.
    record = HostRecord(host="example.com", rate_limit_count=1)
    assert fb.is_penalized("example.com", HTTP, record) is True


# --------------------------------------------------------------------------- #
# Durability: records survive close + reopen
# --------------------------------------------------------------------------- #

def test_durability_counters_across_reopen(tmp_path):
    db_path = tmp_path / "strategy.db"
    fb1 = StrategyFeedback(store=db_path)
    fb1.record("example.com", HTTP, ok=True, latency=0.1)
    fb1.record("example.com", HTTP, ok=True, latency=0.2)
    fb1.close()

    fb2 = StrategyFeedback(store=db_path)
    out = fb2.record("example.com", HTTP, ok=False, latency=0.3)
    assert out.attempts == 3
    assert out.successes == 2
    assert out.failures == 1
    fb2.close()


def test_durability_recommend_across_reopen(tmp_path):
    db_path = tmp_path / "strategy.db"
    fb1 = StrategyFeedback(store=db_path)
    for _ in range(3):
        fb1.record("example.com", OBSCURA, ok=True, latency=0.1)
    fb1.record("example.com", HTTP, ok=False, latency=0.5)
    fb1.close()

    fb2 = StrategyFeedback(store=db_path)
    assert fb2.recommend("example.com") == OBSCURA
    fb2.close()


def test_durability_multiple_strategies_across_reopen(tmp_path):
    db_path = tmp_path / "strategy.db"
    fb1 = StrategyFeedback(store=db_path)
    fb1.record("example.com", HTTP, ok=True, latency=0.1)
    fb1.record("example.com", OBSCURA, ok=False, latency=0.5)
    fb1.close()

    fb2 = StrategyFeedback(store=db_path)
    http_out = fb2.record("example.com", HTTP, ok=True, latency=0.2)
    obscura_out = fb2.record("example.com", OBSCURA, ok=True, latency=0.3)
    assert http_out.attempts == 2
    assert http_out.successes == 2
    assert obscura_out.attempts == 2
    assert obscura_out.successes == 1
    fb2.close()


# --------------------------------------------------------------------------- #
# Export surface
# --------------------------------------------------------------------------- #

def test_exports_in_all():
    import ujin.adapt as m
    assert "StrategyFeedback" in m.__all__
    assert "StrategyOutcome" in m.__all__


def test_import_from_adapt():
    from ujin.adapt import StrategyFeedback, StrategyOutcome  # noqa: F401
