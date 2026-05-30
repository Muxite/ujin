"""PollEngine: sweep, adaptive scheduling, change events, backoff, and the
'stable not spiky' scheduling property — all with an injected fake clock."""
from __future__ import annotations

import random

from eujin.adapt.concurrency import TokenBucket
from eujin.engine import PollEngine
from eujin.poll.callable import CallablePollable


class FakeTime:
    """Manual clock; async sleep advances it (no real waiting)."""

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    async def sleep(self, d: float) -> None:
        self.t += max(0.0, d)


def _engine(ft: FakeTime, **kw) -> PollEngine:
    # huge bucket so scheduling tests aren't gated by rate
    return PollEngine(
        token_bucket=TokenBucket(rate=1e9, burst=1e9, clock=ft.now),
        clock=ft.now,
        sleep=ft.sleep,
        rng=random.Random(1),
        **kw,
    )


async def test_sweep_polls_all_and_flags_changed():
    ft = FakeTime()
    eng = _engine(ft)
    eng.add(CallablePollable(lambda: "static", key="s"), base=10, jitter="none")
    counter = iter(range(100))
    eng.add(CallablePollable(lambda: next(counter), key="c"), base=10, jitter="none")

    r1 = await eng.sweep()
    assert all(r.ok for r in r1)
    # the static target keeps returning the same value; poll it a few more times
    for _ in range(3):
        await eng.sweep()
    by_key = {t.key: t for t in eng.targets.values()}
    # static target: unchanged after the first poll -> interval backs off above base
    assert by_key["s"].prev.changed is False
    assert by_key["s"].interval.current > 10
    # counter target keeps changing -> always polls faster than the static one
    assert by_key["c"].prev.changed is True
    assert by_key["c"].interval.current < by_key["s"].interval.current


async def test_on_change_fires_only_on_change():
    ft = FakeTime()
    eng = _engine(ft)
    fired = []
    seq = iter([1, 1, 2, 2])
    eng.add(
        CallablePollable(lambda: next(seq), key="c"),
        base=10, jitter="none",
        on_change=lambda key, res: fired.append((key, res.payload)),
    )
    for _ in range(4):
        await eng.sweep()
    # changes at value 1 (first) and 2 -> two events
    assert fired == [("c", 1), ("c", 2)]


async def test_run_dispatches_due_targets_with_fake_clock():
    ft = FakeTime()
    eng = _engine(ft)
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        return calls["n"]  # always changes

    t = eng.add(CallablePollable(fn, key="c"), base=5, min_interval=1, jitter="none")
    t.next_due = 0.0  # start immediately
    # run loop alternates poll-ticks and sleep-ticks, so ~2 ticks per poll
    await eng.run(max_ticks=10)
    assert calls["n"] >= 4
    # always-changing target shrinks toward the min interval
    assert t.interval.current == 1


async def test_failing_target_backs_off_and_trips_circuit():
    ft = FakeTime()
    eng = _engine(ft)

    def boom():
        raise RuntimeError("down")

    t = eng.add(CallablePollable(boom, key="bad"), base=5, jitter="none")
    t.breaker.threshold = 3
    t.next_due = 0.0
    await eng.run(max_ticks=10)
    assert t.backoff.failures >= 3
    assert t.breaker.state in ("open", "half_open")
    assert eng.stats()["per_target"]["bad"]["circuit"] in ("open", "half_open")


async def test_phase_jitter_spreads_targets_no_spike():
    """Many targets must NOT all become due at the same instant."""
    ft = FakeTime()
    eng = PollEngine(
        token_bucket=TokenBucket(rate=1e9, burst=1e9, clock=ft.now),
        clock=ft.now, sleep=ft.sleep, rng=random.Random(7),
    )
    for i in range(100):
        eng.add(CallablePollable(lambda: 0, key=f"t{i}"), base=100, jitter="equal")
    due = sorted(t.next_due for t in eng.targets.values())
    # spread across most of the [0, 100) window, and not clustered at one time
    assert due[-1] - due[0] > 50
    assert len(set(round(x, 3) for x in due)) > 90
