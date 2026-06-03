"""A tiny 5-field cron parser (minute resolution) — no third-party dependency.

Supports ``*``, ``*/n``, ``a``, ``a,b``, ``a-b``, ``a-b/n`` in each of the five
fields: minute hour day-of-month month day-of-week (0/7 = Sunday). Enough for the
common ``*/5 * * * *`` / ``0 9 * * 1`` schedules. If ``croniter`` is importable it
is preferred for full fidelity.

``CronExpr.next_after(ts)`` returns the next epoch-seconds fire time strictly
after ``ts`` (computed in local time, matching operator expectations).
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

_RANGES = {
    "minute": (0, 59),
    "hour": (0, 23),
    "dom": (1, 31),
    "month": (1, 12),
    "dow": (0, 6),
}


def _parse_field(spec: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s)
        if part in ("*", ""):
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(part)
        for v in range(start, end + 1, step):
            out.add(v)
    return out


class CronExpr:
    def __init__(self, expr: str):
        fields = expr.split()
        if len(fields) != 5:
            raise ValueError(f"cron expr must have 5 fields, got {len(fields)}: {expr!r}")
        self.minute = _parse_field(fields[0], *_RANGES["minute"])
        self.hour = _parse_field(fields[1], *_RANGES["hour"])
        self.dom = _parse_field(fields[2], *_RANGES["dom"])
        self.month = _parse_field(fields[3], *_RANGES["month"])
        # normalize 7 -> 0 (Sunday) in day-of-week
        self.dow = {d % 7 for d in _parse_field(fields[4], 0, 7)}
        self._dom_restricted = fields[2] != "*"
        self._dow_restricted = fields[4] != "*"

    def _day_ok(self, dt: datetime) -> bool:
        # cron semantics: if both DOM and DOW are restricted, match either.
        py_dow = (dt.weekday() + 1) % 7  # python Mon=0 -> cron Sun=0
        dom_match = dt.day in self.dom
        dow_match = py_dow in self.dow
        if self._dom_restricted and self._dow_restricted:
            return dom_match or dow_match
        if self._dom_restricted:
            return dom_match
        if self._dow_restricted:
            return dow_match
        return True

    def next_after(self, ts: float) -> float:
        # start at the next whole minute strictly after ts
        dt = datetime.fromtimestamp(ts).replace(second=0, microsecond=0)
        dt += timedelta(minutes=1)
        # bounded search (4 years of minutes) to avoid an infinite loop on a
        # pathological expression
        for _ in range(366 * 4 * 24 * 60):
            if (
                dt.month in self.month
                and self._day_ok(dt)
                and dt.hour in self.hour
                and dt.minute in self.minute
            ):
                return dt.timestamp()
            dt += timedelta(minutes=1)
        raise ValueError(f"no cron match within horizon for expr")


def next_fire(expr: str, *, now: float | None = None) -> float:
    """Next fire time for ``expr`` strictly after ``now`` (default: time.time())."""
    try:
        from croniter import croniter  # type: ignore

        base = now if now is not None else time.time()
        return float(croniter(expr, base).get_next(float))
    except ImportError:
        return CronExpr(expr).next_after(now if now is not None else time.time())
