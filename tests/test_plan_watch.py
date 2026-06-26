"""Opt-in plan-file watcher for `jobs-serve` (--watch-plan / $UJIN_WATCH_PLAN).

When enabled, a background task re-runs the *identical* resolution as
``POST /plan/reload?prune=true`` whenever the plan file changes on disk, so added/
changed jobs appear and removed plan-owned jobs are pruned with no restart. These
tests drive the change-detection path directly (``poll_once``) and the loop
(``run`` with an injected sleep + ``max_ticks``) — all offline and deterministic,
no real sleeping and no network.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import yaml

from ujin.engine import PollEngine
from ujin.jobs.app import (
    DEFAULT_WATCH_INTERVAL,
    _env_truthy,
    _PlanWatcher,
    _resolve_watch,
)
from ujin.jobs.manager import JobManager
from ujin.jobs.store import JobStore


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _cmd_job(job_id, arg="x"):
    return {"id": job_id, "name": job_id, "schedule": {"mode": "once"},
            "source": {"kind": "command", "config": {"argv": ["printf", arg]}}}


def _client():
    from fastapi.testclient import TestClient

    return TestClient


# ── flag/env resolution (_resolve_watch / _env_truthy) ──────────────────────── #
def test_env_truthy_accepts_shellish_values():
    for v in ("1", "true", "TRUE", "yes", "on", " On "):
        assert _env_truthy(v) is True
    for v in (None, "", "0", "false", "no", "off", "nope"):
        assert _env_truthy(v) is False


def test_resolve_watch_off_by_default(monkeypatch):
    monkeypatch.delenv("UJIN_WATCH_PLAN", raising=False)
    monkeypatch.delenv("UJIN_WATCH_INTERVAL", raising=False)
    enabled, interval = _resolve_watch(False, None)
    assert enabled is False and interval == DEFAULT_WATCH_INTERVAL


def test_resolve_watch_flag_wins_and_interval_flag(monkeypatch):
    monkeypatch.delenv("UJIN_WATCH_PLAN", raising=False)
    enabled, interval = _resolve_watch(True, 0.25)
    assert enabled is True and interval == 0.25


def test_resolve_watch_env_enables_and_sets_interval(monkeypatch):
    monkeypatch.setenv("UJIN_WATCH_PLAN", "yes")
    monkeypatch.setenv("UJIN_WATCH_INTERVAL", "3.5")
    enabled, interval = _resolve_watch(False, None)
    assert enabled is True and interval == 3.5


def test_resolve_watch_bad_or_nonpositive_interval_falls_back(monkeypatch):
    monkeypatch.delenv("UJIN_WATCH_PLAN", raising=False)
    monkeypatch.setenv("UJIN_WATCH_INTERVAL", "not-a-number")
    assert _resolve_watch(False, None)[1] == DEFAULT_WATCH_INTERVAL
    monkeypatch.delenv("UJIN_WATCH_INTERVAL", raising=False)
    # A non-positive explicit value also falls back (no busy-loop / stall).
    assert _resolve_watch(False, 0.0)[1] == DEFAULT_WATCH_INTERVAL
    assert _resolve_watch(False, -5.0)[1] == DEFAULT_WATCH_INTERVAL


# ── end-to-end via the FastAPI app: watcher reflects file edits, no restart ─── #
def test_watch_disabled_is_inert_by_default(tmp_path, monkeypatch):
    # Default: no watcher task, and a file edit changes nothing until an explicit
    # reload. Confirms the no-config path is byte-identical to today.
    TestClient = _client()
    from ujin.jobs.app import create_jobs_app

    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    monkeypatch.delenv("UJIN_WATCH_PLAN", raising=False)
    plan = tmp_path / "plan.yaml"
    _write(plan, [_cmd_job("keep", "k")])

    with TestClient(create_jobs_app(run_engine=False, plan_path=str(plan))) as c:
        assert c.app.state.plan_watcher is None
        # Edit the file: with no watcher, the running set must not change.
        _write(plan, [_cmd_job("keep", "k"), _cmd_job("added", "a")])
        assert {j["id"] for j in c.get("/jobs").json()} == {"keep"}
        # Only an explicit reload picks it up.
        c.post("/plan/reload")
        assert {j["id"] for j in c.get("/jobs").json()} == {"keep", "added"}


def test_watch_enabled_adds_and_prunes_on_change(tmp_path, monkeypatch):
    # Acceptance: with the watcher on, editing the file adds a new job id and
    # prunes one removed from the file — visible via /jobs AND the /health plan block.
    TestClient = _client()
    from ujin.jobs.app import create_jobs_app

    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    plan = tmp_path / "plan.yaml"
    _write(plan, [_cmd_job("keep", "k"), _cmd_job("drop", "d")])

    app = create_jobs_app(run_engine=False, plan_path=str(plan), watch_plan=True)
    with TestClient(app) as c:
        w = app.state.plan_watcher
        assert w is not None
        assert {j["id"] for j in c.get("/jobs").json()} == {"keep", "drop"}

        # An unchanged file is a no-op (no reload).
        assert w.poll_once() is False

        # Add `added`, remove `drop`; one change check applies both.
        _write(plan, [_cmd_job("keep", "k"), _cmd_job("added", "a")])
        assert w.poll_once() is True
        ids = {j["id"] for j in c.get("/jobs").json()}
        assert ids == {"keep", "added"}
        assert c.get("/jobs/drop").status_code == 404
        plan_block = c.get("/health").json()["plan"]
        assert plan_block["loaded"] == ["keep", "added"]
        assert plan_block["pruned"] == ["drop"]


def test_watch_malformed_save_keeps_jobs_and_surfaces_failure(tmp_path, monkeypatch):
    # Acceptance: a malformed save must not wipe or crash the job set; the previous
    # good jobs remain and the error surfaces via the existing `failed` reporting.
    TestClient = _client()
    from ujin.jobs.app import create_jobs_app

    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    plan = tmp_path / "plan.yaml"
    _write(plan, [_cmd_job("alpha", "a")])

    app = create_jobs_app(run_engine=False, plan_path=str(plan), watch_plan=True)
    with TestClient(app) as c:
        w = app.state.plan_watcher
        # A bare scalar is neither a list nor a mapping -> a file-level error.
        plan.write_text("just a string\n", encoding="utf-8")
        assert w.poll_once() is True  # change detected and processed (non-destructive)
        # alpha survives; the failure is surfaced in the /health plan block.
        assert {j["id"] for j in c.get("/jobs").json()} == {"alpha"}
        plan_block = c.get("/health").json()["plan"]
        assert plan_block["failed"]
        assert plan_block.get("pruned") == []


def test_watch_never_prunes_api_or_preload_jobs(tmp_path, monkeypatch):
    # The provenance guard holds for the watcher too: API-created and positional
    # preload jobs are never pruned even when the plan is emptied.
    TestClient = _client()
    from ujin.jobs.app import create_jobs_app

    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    preload = tmp_path / "jobs.yaml"
    _write(preload, [_cmd_job("preloaded", "p")])
    plan = tmp_path / "plan.yaml"
    _write(plan, [_cmd_job("planjob", "x")])

    app = create_jobs_app(str(preload), run_engine=False, plan_path=str(plan),
                          watch_plan=True)
    with TestClient(app) as c:
        api_id = c.post("/jobs", json={
            "name": "apijob", "schedule": {"mode": "once"},
            "source": {"kind": "command", "config": {"argv": ["printf", "a"]}},
        }).json()["id"]
        # Empty the plan and let the watcher reload: only the plan-owned job goes.
        _write(plan, [])
        assert app.state.plan_watcher.poll_once() is True
        ids = {j["id"] for j in c.get("/jobs").json()}
        assert "planjob" not in ids
        assert "preloaded" in ids and api_id in ids


def test_watch_missing_file_is_skipped_keeping_jobs(tmp_path, monkeypatch):
    # A transiently-absent plan file is "no actionable change": poll_once returns
    # False and the running jobs are untouched (a deleted file never wipes jobs).
    TestClient = _client()
    from ujin.jobs.app import create_jobs_app

    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    plan = tmp_path / "plan.yaml"
    _write(plan, [_cmd_job("alpha", "a")])

    app = create_jobs_app(run_engine=False, plan_path=str(plan), watch_plan=True)
    with TestClient(app) as c:
        plan.unlink()
        assert app.state.plan_watcher.poll_once() is False
        assert {j["id"] for j in c.get("/jobs").json()} == {"alpha"}


def test_watch_only_no_match_keeps_last_good(tmp_path, monkeypatch):
    # If a watched reload's --only now matches nothing, poll_once reports False
    # (logged) and keeps the last good job set rather than wiping it.
    TestClient = _client()
    from ujin.jobs.app import create_jobs_app

    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    plan = tmp_path / "plan.yaml"
    _write(plan, [_cmd_job("alpha", "a")])

    app = create_jobs_app(run_engine=False, plan_path=str(plan), watch_plan=True,
                          only=["alpha"])
    with TestClient(app) as c:
        # Replace alpha with beta -> --only alpha matches nothing on reload.
        _write(plan, [_cmd_job("beta", "b")])
        assert app.state.plan_watcher.poll_once() is False
        assert {j["id"] for j in c.get("/jobs").json()} == {"alpha"}


def test_watch_enabled_but_no_plan_is_inactive(tmp_path, monkeypatch):
    # --watch-plan with no plan configured: the watcher stays inactive (the reload
    # it mirrors is plan-only), and the server otherwise behaves normally.
    TestClient = _client()
    from ujin.jobs.app import create_jobs_app

    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    monkeypatch.delenv("UJIN_INGEST_PLAN", raising=False)
    with TestClient(create_jobs_app(run_engine=False, watch_plan=True)) as c:
        assert c.app.state.plan_watcher is None
        assert c.get("/health").json()["status"] == "ok"


# ── the background run() loop (deterministic: injected sleep + max_ticks) ────── #
def _bare_manager(tmp_path):
    return JobManager(PollEngine(), JobStore(tmp_path / "jobs.db"))


class _FakeApp:
    """Minimal stand-in exposing the `.state` the watcher writes `plan` onto."""

    class _State:
        pass

    def __init__(self):
        self.state = self._State()


def test_run_loop_applies_change_then_stops(tmp_path):
    # Drive run() with a no-op sleep and max_ticks so it polls exactly once and
    # picks up a pre-applied edit, with zero real waiting.
    from ujin.jobs.app import _apply_ingest_plan

    mgr = _bare_manager(tmp_path)
    app = _FakeApp()
    plan = tmp_path / "plan.yaml"
    _write(plan, [_cmd_job("keep", "k")])
    # Seed the manager + baseline like startup would.
    app.state.plan = _apply_ingest_plan(mgr, str(plan), prune=True)

    async def _noop(_delay):  # injected sleep: never actually waits
        return None

    w = _PlanWatcher(app, mgr, str(plan), None, None, 0.01, sleep=_noop)
    # Edit the file, then run one tick.
    _write(plan, [_cmd_job("keep", "k"), _cmd_job("added", "a")])
    asyncio.run(w.run(max_ticks=1))
    assert set(mgr.jobs) == {"keep", "added"}
    assert app.state.plan["loaded"] == ["keep", "added"]


def test_run_loop_honors_stop_event(tmp_path):
    mgr = _bare_manager(tmp_path)
    app = _FakeApp()
    plan = tmp_path / "plan.yaml"
    _write(plan, [_cmd_job("keep", "k")])

    async def _noop(_delay):
        return None

    w = _PlanWatcher(app, mgr, str(plan), None, None, 0.01, sleep=_noop)

    async def _drive():
        stop = asyncio.Event()
        stop.set()
        await w.run(stop=stop)  # returns immediately without polling

    asyncio.run(_drive())
    # Nothing reloaded: app.state has no plan attribute set by the watcher.
    assert not hasattr(app.state, "plan")


def test_run_loop_survives_tick_error(tmp_path, monkeypatch):
    # An unexpected error inside a tick is swallowed so the loop never crashes.
    mgr = _bare_manager(tmp_path)
    app = _FakeApp()
    plan = tmp_path / "plan.yaml"
    _write(plan, [_cmd_job("keep", "k")])

    async def _noop(_delay):
        return None

    w = _PlanWatcher(app, mgr, str(plan), None, None, 0.01, sleep=_noop)

    def _boom():
        raise RuntimeError("kaboom")

    monkeypatch.setattr(w, "poll_once", _boom)
    # Must return cleanly despite poll_once raising every tick.
    asyncio.run(w.run(max_ticks=2))
