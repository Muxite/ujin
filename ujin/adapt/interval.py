"""Adaptive poll interval.

Back off targets that aren't changing; speed up targets that are. This keeps the
engine responsive to active sources while not hammering static ones — the
"adaptive" half of ujin. Pure and deterministic (no clock), so it's trivially
testable.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AdaptiveInterval:
    """Multiplicative grow/shrink interval controller.

    interval starts at ``base`` and is clamped to ``[min_interval, max_interval]``.
    - no change  -> interval *= grow   (back off)
    - change     -> interval *= shrink (poll faster)

    ``grow > 1`` and ``0 < shrink < 1``. Returns the *pre-jitter* interval in
    seconds; the engine applies jitter on top.
    """

    base: float
    min_interval: float = 1.0
    max_interval: float = 3600.0
    grow: float = 1.6
    shrink: float = 0.4
    _interval: float = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.grow <= 1:
            raise ValueError("grow must be > 1")
        if not (0 < self.shrink < 1):
            raise ValueError("shrink must be in (0, 1)")
        if self.min_interval > self.max_interval:
            raise ValueError("min_interval must be <= max_interval")
        self._interval = self._clamp(self.base)

    def _clamp(self, v: float) -> float:
        return max(self.min_interval, min(self.max_interval, v))

    @property
    def current(self) -> float:
        return self._interval

    def next(self, changed: bool) -> float:
        """Advance the interval based on whether the last poll saw a change."""
        factor = self.shrink if changed else self.grow
        self._interval = self._clamp(self._interval * factor)
        return self._interval

    def reset(self) -> float:
        self._interval = self._clamp(self.base)
        return self._interval
