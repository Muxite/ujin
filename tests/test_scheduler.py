"""JobManager scheduling: adaptive registration, run-now, pause/resume, and the
cron loop driven by a fake clock (no real waiting). Plus the cron parser."""
from __future__ import annotations

import random
import time

from ujin.adapt.concurrency import TokenBucket
from ujin.engine import PollEngine
from ujin.jobs.cron import CronExpr, next_fire
from ujin.jobs.manager import JobManager, UnknownKind
from ujin.jobs.model import JobSpec, ScheduleSpec, SinkSpec, SourceSpec
from ujin.jobs.store import JobStore
from ujin.poll.base import PollResult


class FakeTime:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    async def sleep(self, d: float) -> None:
        self.t += max(0.0, d)


class CountingPollable:
    """A plugin-free in-process source: returns an incrementing value."""

    def __init__(self, key: str):
        self.key = key
        self._n = 0

    async def poll(self, prev):
        self._n += 1
        return PollResult(ok=True, changed=True, fingerprint=str(self._n),
                          payload={"n": self._n})


def _engine(ft: FakeTime) -> PollEngine:
    return PollEngine(token_bucket=TokenBucket(rate=1e9, burst=1e9, clock=ft.now),
                      clock=ft.now, sleep=ft.sleep, rng=random.Random(1))


def _mgr(tmp_path, ft):
    return JobManager(_engine(ft), JobStore(tmp_path / "jobs.db"))


def _spec(mode="adaptive", **sch) -> JobSpec:
    return JobSpec(
        name="t",
        source=SourceSpec(kind="api", config={"url": "https://x"}),
        sinks=[SinkSpec(kind="stdout", config={})],
        schedule=ScheduleSpec(mode=mode, **sch),
    )


# -- cron parser ------------------------------------------------------------ #
def test_cron_every_5_min():
    base = time.mktime(time.strptime("2026-06-03 10:02:00", "%Y-%m-%d %H:%M:%S"))
    nf = next_fire("*/5 * * * *", now=base)
    assert time.strftime("%H:%M", time.localtime(nf)) == "10:05"


def test_cron_specific_time_and_dow():
    expr = CronExpr("0 9 * * 1")  # 09:00 on Mondays
    base = time.mktime(time.strptime("2026-06-03 12:00:00", "%Y-%m-%d %H:%M:%S"))  # Wed
    nf = expr.next_after(base)
    lt = time.localtime(nf)
    assert lt.tm_hour == 9 and lt.tm_min == 0 and lt.tm_wday == 0  # Monday


def test_cron_bad_expr_raises():
    try:
        CronExpr("1 2 3")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


# -- adaptive --------------------------------------------------------------- #
def test_adaptive_job_registers_with_engine(tmp_path):
    ft = FakeTime()
    m = _mgr(tmp_path, ft)
    m.scrape_service = None
    h = m.register(_spec("adaptive", base=10))
    assert h.adaptive is True
    assert h.spec.id in m.engine.targets  # the engine loop will drive it


def test_unknown_source_kind_rejected(tmp_path):
    ft = FakeTime()
    m = _mgr(tmp_path, ft)
    spec = JobSpec(name="t", source=SourceSpec(kind="nope", config={}))
    try:
        m.register(spec)
    except UnknownKind:
        pass
    else:
        raise AssertionError("expected UnknownKind")


# -- run-now / once --------------------------------------------------------- #
async def test_run_now_drives_poll_and_records(tmp_path):
    ft = FakeTime()
    m = _mgr(tmp_path, ft)
    spec = _spec("once")
    h = m.register(spec)
    # swap in a deterministic in-process source
    h.target.pollable = CountingPollable(spec.id)
    assert spec.id not in m.engine.targets  # once jobs are standalone

    r = await m.run_now(spec.id)
    assert r.ok and r.changed and r.fingerprint == "1"
    runs = m.store.runs(spec.id)
    assert len(runs) == 1 and runs[0]["changed"] is True


# -- cron loop -------------------------------------------------------------- #
async def test_cron_loop_fires_due_jobs(tmp_path):
    ft = FakeTime()
    m = _mgr(tmp_path, ft)
    spec = _spec("cron", cron="* * * * *")  # every minute
    h = m.register(spec)
    h.target.pollable = CountingPollable(spec.id)
    # first fire time was computed at register() off t=0
    assert h.next_fire >= 60.0

    await m.cron_loop(max_ticks=3)
    # clock advanced and the job polled at least once
    assert h.target.polls >= 1
    assert ft.t >= 60.0


# -- pause / resume --------------------------------------------------------- #
def test_pause_removes_from_engine_resume_rearms(tmp_path):
    ft = FakeTime()
    m = _mgr(tmp_path, ft)
    spec = _spec("adaptive", base=10)
    m.create(spec)
    assert spec.id in m.engine.targets

    assert m.pause(spec.id) is True
    assert spec.id not in m.engine.targets
    assert m.store.get(spec.id).enabled is False

    assert m.resume(spec.id) is True
    assert spec.id in m.engine.targets
    assert m.store.get(spec.id).enabled is True
