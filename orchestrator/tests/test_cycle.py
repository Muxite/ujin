"""End-to-end cycle tests against a hermetic temp git repo with the fake backend.

These exercise the real plumbing — worktree create, gate run (with the editable-install
PYTHONPATH fix + import self-check), verify, serialized merge, and release-to-master —
plus the full-auto safety rails (KILL, contract block, coverage-drop death).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from orchestrator import gitutil, worktree
from orchestrator.agents import FakeAgentBackend
from orchestrator.cycle import _dead_foci, _plan_context, next_cycle, tick
from orchestrator.orchestrator import serve
from orchestrator.state import StateStore as _StateStore
from orchestrator.state import (
    CYCLE_DONE,
    PHASE_DEAD,
    PHASE_INTEGRATED,
    StateStore,
)
from .conftest import COV_LOW, make_cfg

DEMO = [{"focus": "demo", "difficulty": "routine", "priority": 1,
         "tasks": ["add marker"], "acceptance": ["marker present", "gate green"]}]


def _git_out(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True,
                          text=True).stdout


def run_until_done(cfg, backend, store, max_ticks=80):
    for _ in range(max_ticks):
        tick(cfg, backend, store)
        cyc = store.read_cycle()
        if cyc.get("phase") == CYCLE_DONE and cyc.get("cycle") == cfg.cycle:
            return
    raise AssertionError("cycle did not reach done")


# --------------------------------------------------------------------------- #
def test_dead_foci_surfaced_to_planner(temp_repo: Path):
    cfg = make_cfg(temp_repo)
    from orchestrator.state import StateStore
    # Simulate a focus quarantined in a prior cycle.
    gitutil.git("branch", "dead/agent/learned-rate-limit-0.8", "master", cwd=temp_repo)
    assert "dead/agent/learned-rate-limit-0.8" in _dead_foci(cfg)
    ctx = _plan_context(cfg, StateStore(cfg.state_dir))
    assert "RE-ATTEMPT" in ctx and "learned-rate-limit" in ctx


def test_next_cycle():
    assert next_cycle("0.6") == "0.7"
    assert next_cycle("0.9") == "0.10"
    assert next_cycle("3") == "4"
    assert next_cycle("smoke") == "smoke.1"  # non-numeric scratch label


def test_worktree_isolation(temp_repo: Path):
    cfg = make_cfg(temp_repo)
    from orchestrator import merge
    merge.ensure_integration_worktree(cfg)
    wt = worktree.create(cfg, "demo", cfg.integration_branch)
    assert wt.exists()
    # The import self-check must resolve ujin inside the worktree, not elsewhere.
    worktree.verify_import(wt)
    env = worktree.env_for(wt)
    assert env["PYTHONPATH"].startswith(str(wt.resolve()))


def test_full_cycle_releases_to_master(temp_repo: Path):
    cfg = make_cfg(temp_repo)
    store = StateStore(cfg.state_dir)
    run_until_done(cfg, FakeAgentBackend(backlog=DEMO), store)

    foci = store.all_foci("0.6")
    assert len(foci) == 1 and foci[0].phase == PHASE_INTEGRATED

    # Master advanced: version bumped to 0.6.0 and the merge landed.
    master_init = _git_out(temp_repo, "show", "master:ujin/__init__.py")
    assert '0.6.0' in master_init
    master_log = _git_out(temp_repo, "log", "--oneline", "master")
    assert "Cycle 0.6" in master_log

    # The agent branch was cleaned up; the release did not push (default).
    assert not gitutil.branch_exists("agent/demo", temp_repo)

    events = {e["event"] for e in store.read_events("0.6")}
    assert {"plan_done", "built", "gate_green", "approved", "integrated", "released"} <= events


def test_kill_switch_noops(temp_repo: Path):
    cfg = make_cfg(temp_repo)
    store = StateStore(cfg.state_dir)
    (cfg.state_dir / "KILL").parent.mkdir(parents=True, exist_ok=True)
    (cfg.state_dir / "KILL").write_text("stop")
    assert tick(cfg, FakeAgentBackend(backlog=DEMO), store) == {"action": "killed"}
    assert store.read_cycle() == {}  # nothing was created


def test_contract_touch_is_hard_blocked(temp_repo: Path):
    class ContractBreaker(FakeAgentBackend):
        def run_builder(self, cfg, fs, wt, feedback):
            (wt / "tests" / "test_consumer_contracts.py").write_text(
                "def test_contract():\n    assert True  # tampered\n")
            return super().run_builder(cfg, fs, wt, feedback)

    cfg = make_cfg(temp_repo)
    store = StateStore(cfg.state_dir)
    run_until_done(cfg, ContractBreaker(backlog=DEMO), store)

    assert store.all_foci("0.6")[0].phase == PHASE_DEAD
    events = store.read_events("0.6")
    assert any(e["event"] == "CONTRACT_BLOCK" and e.get("alert") for e in events)
    # Never merged: master untouched, branch quarantined to dead/*.
    assert "Cycle 0.6" not in _git_out(temp_repo, "log", "--oneline", "master")
    assert gitutil.branch_exists("dead/agent/demo-0.6", temp_repo)


def test_serve_runs_continuously_until_release(temp_repo: Path):
    cfg = make_cfg(temp_repo)
    loader = lambda: (cfg, _StateStore(cfg.state_dir))
    serve(loader, FakeAgentBackend(backlog=DEMO), idle_sleep=0, busy_sleep=0,
          max_iterations=40, sleep_fn=lambda s: None)
    events = {e["event"] for e in _StateStore(cfg.state_dir).read_events("0.6")}
    assert "released" in events  # the daemon drove a full cycle without manual ticks


def test_coverage_floor_ratchet_is_capped(temp_repo: Path):
    cfg = make_cfg(temp_repo)  # COV_OK reports 95%, cap defaults to 90
    store = StateStore(cfg.state_dir)
    run_until_done(cfg, FakeAgentBackend(backlog=DEMO), store)
    # The ratchet saw 95% but must not raise the floor above the 90 cap.
    assert store.read_cov_floor(cfg.coverage_floor) == 90.0


def test_drain_finishes_cycle_then_halts(temp_repo: Path):
    import dataclasses
    from orchestrator.state import CYCLE_HALTED
    cfg = dataclasses.replace(make_cfg(temp_repo), drain=True)
    store = StateStore(cfg.state_dir)
    run_until_done(cfg, FakeAgentBackend(backlog=DEMO), store)
    # The current cycle still released (in-flight work merged)...
    assert "released" in {e["event"] for e in store.read_events("0.6")}
    # ...but draining halts instead of starting cycle 0.7.
    res = tick(cfg, FakeAgentBackend(backlog=DEMO), store)
    assert res == {"action": "halted"}
    assert store.read_cycle()["phase"] == CYCLE_HALTED
    assert store.read_cycle()["cycle"] == "0.6"  # never advanced


def test_serve_respects_kill(temp_repo: Path):
    cfg = make_cfg(temp_repo)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    (cfg.state_dir / "KILL").write_text("stop")
    loader = lambda: (cfg, _StateStore(cfg.state_dir))
    serve(loader, FakeAgentBackend(backlog=DEMO), idle_sleep=0, busy_sleep=0,
          max_iterations=5, sleep_fn=lambda s: None)
    assert _StateStore(cfg.state_dir).read_cycle() == {}  # never started any work


def test_coverage_drop_dies_after_retries(temp_repo: Path):
    cfg = make_cfg(temp_repo, cov_cmd=COV_LOW)  # 50% < 85% floor
    store = StateStore(cfg.state_dir)
    run_until_done(cfg, FakeAgentBackend(backlog=DEMO), store)

    fs = store.all_foci("0.6")[0]
    assert fs.phase == PHASE_DEAD
    assert fs.attempts == cfg.max_build_retries
    assert any("retr" in n for n in fs.notes)
    assert "Cycle 0.6" not in _git_out(temp_repo, "log", "--oneline", "master")
