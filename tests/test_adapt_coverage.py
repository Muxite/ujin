"""Edge-case coverage for ujin/adapt/jitter.py, interval.py, and backoff.py.

Targets the previously-uncovered branches:
  jitter.py   lines 52, 54, 60  — apply("full"), apply("equal"), apply(unknown)
  interval.py lines 38, 55-56   — min>max validation, reset()
  backoff.py  line 46            — Backoff.reset()
"""
from __future__ import annotations

import random

import pytest

from ujin.adapt import jitter
from ujin.adapt.backoff import Backoff
from ujin.adapt.interval import AdaptiveInterval


# -- jitter.apply edge cases ------------------------------------------------- #

def test_apply_full_dispatches():
    rng = random.Random(7)
    v = jitter.apply(10.0, "full", rng=rng)
    assert 0.0 <= v <= 10.0


def test_apply_equal_dispatches():
    rng = random.Random(7)
    v = jitter.apply(10.0, "equal", rng=rng)
    assert 5.0 <= v <= 10.0


def test_apply_off_is_identity():
    assert jitter.apply(5.0, "off") == 5.0


def test_apply_unknown_raises():
    with pytest.raises(ValueError, match="unknown jitter strategy"):
        jitter.apply(1.0, "bogus")


# -- AdaptiveInterval validation and reset ----------------------------------- #

def test_interval_min_gt_max_raises():
    with pytest.raises(ValueError, match="min_interval"):
        AdaptiveInterval(base=10, min_interval=100, max_interval=10)


def test_interval_reset_returns_base():
    iv = AdaptiveInterval(base=10, min_interval=1, max_interval=1000)
    iv.next(changed=False)
    iv.next(changed=False)
    assert iv.current != 10
    result = iv.reset()
    assert result == 10
    assert iv.current == 10


def test_interval_reset_clamps_to_bounds():
    iv = AdaptiveInterval(base=0.5, min_interval=5.0, max_interval=1000.0)
    assert iv.reset() == 5.0


# -- Backoff.reset ----------------------------------------------------------- #

def test_backoff_reset_clears_failures():
    b = Backoff(base=1, factor=2, cap=100)
    b.on_failure()
    b.on_failure()
    assert b.failures == 2
    b.reset()
    assert b.failures == 0
    # next failure after reset restarts from base
    assert b.on_failure() == 1.0
