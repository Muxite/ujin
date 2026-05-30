"""Jitter strategies — the "stable, not spiky" half of eujin.

Without jitter, many targets on the same cadence drift into phase and fire
together, producing periodic load spikes. Randomizing each delay spreads work
evenly. Strategies follow AWS's "Exponential Backoff and Jitter":

- ``full``:        uniform(0, d)            — maximum spread
- ``equal``:       d/2 + uniform(0, d/2)    — spread but keeps a floor
- ``decorrelated`` base..prev*3 capped      — good for retry/backoff sequences

``phase`` gives a randomized initial offset so targets start out-of-phase.

All functions take an injectable ``rng`` (``random.Random``) for deterministic
tests; default is the module-global RNG.
"""
from __future__ import annotations

import random
from typing import Callable

_RNG = random.Random()

Strategy = Callable[..., float]


def full(d: float, *, rng: random.Random = _RNG) -> float:
    """uniform(0, d)."""
    return rng.uniform(0.0, max(0.0, d))


def equal(d: float, *, rng: random.Random = _RNG) -> float:
    """d/2 + uniform(0, d/2): never less than half the base delay."""
    half = max(0.0, d) / 2.0
    return half + rng.uniform(0.0, half)


def decorrelated(base: float, prev: float, cap: float, *, rng: random.Random = _RNG) -> float:
    """min(cap, uniform(base, prev*3)). Stateful sequences pass the last value as ``prev``."""
    lo, hi = base, max(base, prev * 3.0)
    return min(cap, rng.uniform(lo, hi))


def phase(d: float, *, rng: random.Random = _RNG) -> float:
    """Randomized initial offset in [0, d) so targets don't start aligned."""
    return rng.uniform(0.0, max(0.0, d))


def apply(d: float, strategy: str = "equal", *, rng: random.Random = _RNG,
          prev: float | None = None, cap: float | None = None) -> float:
    """Dispatch by name. ``decorrelated`` uses ``prev`` (defaults to ``d``) and ``cap``."""
    if strategy == "full":
        return full(d, rng=rng)
    if strategy == "equal":
        return equal(d, rng=rng)
    if strategy == "decorrelated":
        return decorrelated(d, prev if prev is not None else d,
                            cap if cap is not None else d * 4.0, rng=rng)
    if strategy in ("none", "off"):
        return d
    raise ValueError(f"unknown jitter strategy: {strategy!r}")
