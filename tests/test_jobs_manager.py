"""JobManager lifecycle: register/pause/resume/delete, run_now, the cron loop
with a fake clock, and load_from_store fault isolation."""
from __future__ import annotations

import asyncio

import pytest

from conftest import FakeClock
from ujin.engine import PollEngine
from ujin.jobs import JobSpec, JobStore, ScheduleSpec, SourceSpec, TransformSpec
from ujin.jobs.manager import JobManager, UnknownKind
from ujin.poll.callable import CallablePollable


def _manager(tmp_path, clock=None):
    eng_kw = {}
    if clock is not None:
        async def _sleep(d):
            await clock.sleep(d)
        eng_kw = {"clock": clock, "sleep": _sleep}
    engine = PollEngine(**eng_kw)
    store = JobStore(tmp_path / "jobs.db")
    return JobManager(engine, store)


def _cmd_spec(name="echo", *, mode="adaptive", cron=None, enabled=True, **kw):
    return JobSpec(
        name=name,
        source=SourceSpec(kind="command", config={"argv": ["echo", name]}),
        schedule=ScheduleSpec(mode=mode, cron=cron, base=60),
        enabled=enabled,
        **kw,
    )


# ── register / CRUD ─────────────────────────────────────────────────────────

def test_register_adaptive_lands_in_engine(tmp_path):
    m = _manager(tmp_path)
    h = m.register(_cmd_spec())
    assert h.adaptive is True
    assert h.spec.id in m.engine.targets


def test_register_cron_stays_out_of_engine(tmp_path):
    m = _manager(tmp_path)
    h = m.register(_cmd_spec(mode="cron", cron="*/5 * * * *"))
    assert h.adaptive is False
    assert h.spec.id not in m.engine.targets
    assert h.next_fire > 0


def test_register_unknown_source_kind_raises(tmp_path):
    m = _manager(tmp_path)
    spec = JobSpec(name="x", source=SourceSpec(kind="warp", config={}))
    with pytest.raises(UnknownKind):
        m.register(spec)


def test_register_unknown_transform_kind_raises(tmp_path):
    m = _manager(tmp_path)
    spec = _cmd_spec(transforms=[TransformSpec(kind="nope", config={})])
    with pytest.raises(UnknownKind):
        m.register(spec)


def test_create_persists_only_valid_specs(tmp_path):
    m = _manager(tmp_path)
    m.create(_cmd_spec())
    assert len(m.store.list()) == 1
    with pytest.raises(UnknownKind):
        m.create(JobSpec(name="bad", source=SourceSpec(kind="warp", config={})))
    assert len(m.store.list()) == 1  # invalid spec never persisted


def test_delete_removes_everywhere(tmp_path):
    m = _manager(tmp_path)
    h = m.create(_cmd_spec())
    assert m.delete(h.spec.id) is True
    assert h.spec.id not in m.engine.targets
    assert m.store.list() == []
    assert m.delete(h.spec.id) is False  # second delete is a no-op


def test_pause_and_resume_adaptive(tmp_path):
    m = _manager(tmp_path)
    h = m.create(_cmd_spec())
    jid = h.spec.id

    assert m.pause(jid) is True
    assert jid not in m.engine.targets
    assert m.get(jid).summary()["state"] == "paused"
    # store reflects disabled
    assert m.store.list()[0].enabled is False

    assert m.resume(jid) is True
    assert jid in m.engine.targets
    assert m.engine.targets[jid] is h.target  # counters survive
    assert m.store.list()[0].enabled is True


def test_pause_resume_unknown_id(tmp_path):
    m = _manager(tmp_path)
    assert m.pause("ghost") is False
    assert m.resume("ghost") is False


def test_resume_cron_recomputes_next_fire(tmp_path):
    m = _manager(tmp_path)
    h = m.create(_cmd_spec(mode="cron", cron="*/5 * * * *"))
    m.pause(h.spec.id)
    h.next_fire = 0.0
    m.resume(h.spec.id)
    assert h.next_fire > 0


# ── run_now ─────────────────────────────────────────────────────────────────

async def test_run_now_executes_and_records(tmp_path):
    m = _manager(tmp_path)
    h = m.create(_cmd_spec())
    result = await m.run_now(h.spec.id)
    assert result.ok is True
    assert h.state == "idle"
    assert h.target.polls == 1


async def test_run_now_unknown_id_returns_none(tmp_path):
    m = _manager(tmp_path)
    assert await m.run_now("ghost") is None


async def test_run_now_failure_sets_error_state(tmp_path):
    m = _manager(tmp_path)
    spec = JobSpec(
        name="boom",
        source=SourceSpec(kind="command",
                          config={"argv": ["false"]}),  # exits 1
    )
    h = m.create(spec)
    result = await m.run_now(h.spec.id)
    assert result.ok is False
    assert h.state == "error"
    assert h.last_error


# ── cron loop with fake clock ────────────────────────────────────────────────

async def test_cron_loop_fires_due_jobs(tmp_path):
    clk = FakeClock(start=0.0)
    m = _manager(tmp_path, clock=clk)

    fired = []

    class _Probe:
        key = "probe"

        async def poll(self, prev):
            from ujin.poll.base import PollResult

            fired.append(clk.now())
            return PollResult(ok=True, changed=True, fingerprint=str(len(fired)))

    spec = _cmd_spec(mode="cron", cron="* * * * *")
    h = m.register(spec)
    h.target.pollable = _Probe()
    h.next_fire = 0.0  # due immediately

    await m.cron_loop(max_ticks=2)
    assert len(fired) >= 1
    assert h.next_fire > 0  # rescheduled after firing


async def test_cron_loop_skips_paused_jobs(tmp_path):
    clk = FakeClock(start=0.0)
    m = _manager(tmp_path, clock=clk)
    h = m.register(_cmd_spec(mode="cron", cron="* * * * *"))
    h.next_fire = 0.0
    m.pause(h.spec.id)
    await m.cron_loop(max_ticks=2)
    assert h.target.polls == 0


async def test_cron_loop_stop_event(tmp_path):
    clk = FakeClock(start=0.0)
    m = _manager(tmp_path, clock=clk)
    stop = asyncio.Event()
    stop.set()
    await m.cron_loop(stop=stop)  # returns immediately


# ── startup reload ───────────────────────────────────────────────────────────

def test_load_from_store_rebuilds_and_isolates_bad_specs(tmp_path):
    m = _manager(tmp_path)
    good = _cmd_spec(name="good")
    m.create(good)

    # persist a spec whose kind no longer resolves (simulates a removed plugin)
    bad = JobSpec(name="bad", source=SourceSpec(kind="vanished_plugin", config={}))
    m.store.upsert(bad)

    m2 = JobManager(PollEngine(), m.store)
    m2.load_from_store()
    assert good.id in m2.jobs
    assert bad.id not in m2.jobs  # logged + skipped, not fatal


def test_load_from_store_once_jobs_marked_done(tmp_path):
    m = _manager(tmp_path)
    spec = _cmd_spec(name="oneshot", mode="once")
    m.create(spec)
    m2 = JobManager(PollEngine(), m.store)
    m2.load_from_store()
    assert m2.jobs[spec.id].state == "done"
