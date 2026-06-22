"""The cycle state machine — ``tick()`` advances the loop one bounded step.

Designed for systemd ``Type=oneshot``: each tick does at most ``max_concurrent``
expensive operations (builder / gate / verifier / merge), persists everything to disk,
and exits, so the loop is resumable after a crash and never runs unbounded.

Cycle phases: planning -> working -> integrating -> releasing -> done -> (next cycle).
Focus phases: ready -> building -> testing -> reviewing -> {approved|needs_work|dead};
approved -> integrated during the integrate phase.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from . import gitutil, merge, worktree
from .agents import AgentBackend
from .config import Config
from .gates import contracts_touched, gate_passed, run_gates
from .state import (
    ACTIVE_PHASES,
    CYCLE_DONE,
    CYCLE_INTEGRATING,
    CYCLE_PLANNING,
    CYCLE_RELEASING,
    CYCLE_WORKING,
    PHASE_APPROVED,
    PHASE_BUILDING,
    PHASE_DEAD,
    PHASE_INTEGRATED,
    PHASE_NEEDS_WORK,
    PHASE_READY,
    PHASE_REVIEWING,
    PHASE_TESTING,
    FocusState,
    StateStore,
    now_iso,
)


def _cfg_for_state(cfg: Config, store: StateStore) -> Config:
    """Override cfg.cycle with the cycle persisted in state (cold start uses cfg)."""
    cycle = store.read_cycle()
    if cycle.get("cycle"):
        return dataclasses.replace(cfg, cycle=cycle["cycle"])
    return cfg


def next_cycle(cycle: str) -> str:
    parts = cycle.split(".")
    if parts[-1].isdigit():
        parts[-1] = str(int(parts[-1]) + 1)
        return ".".join(parts)
    return f"{cycle}.1"  # non-numeric label (e.g. a scratch cycle) -> sub-cycle


def tick(cfg: Config, backend: AgentBackend, store: StateStore | None = None) -> dict[str, Any]:
    store = store or StateStore(cfg.state_dir)

    if store.killed:
        return {"action": "killed"}

    cfg = _cfg_for_state(cfg, store)
    cycle = store.read_cycle()
    if not cycle:
        cycle = {"cycle": cfg.cycle, "phase": CYCLE_PLANNING,
                 "integration_branch": cfg.integration_branch, "started_at": now_iso()}
        store.write_cycle(cycle)
        store.log_event(cfg.cycle, "cycle_start", integration=cfg.integration_branch)

    phase = cycle["phase"]
    dispatch = {
        CYCLE_PLANNING: _do_plan,
        CYCLE_WORKING: _advance_work,
        CYCLE_INTEGRATING: _do_integrate,
        CYCLE_RELEASING: _do_release,
        CYCLE_DONE: _start_next_cycle,
    }
    handler = dispatch.get(phase)
    if handler is None:
        return {"action": "unknown_phase", "phase": phase}
    return handler(cfg, backend, store, cycle)


# --------------------------------------------------------------------------- #
def _budget_ok(cfg: Config, store: StateStore) -> bool:
    b = store.read_budget()
    return (b.get("spent_today", 0.0) < cfg.budgets.per_day
            and b.get("spent_cycle", 0.0) < cfg.budgets.per_cycle)


def _do_plan(cfg, backend, store, cycle) -> dict[str, Any]:
    if not _budget_ok(cfg, store):
        return {"action": "plan_skipped_budget"}
    # Ensure the integration branch/worktree exists before any agent branches off it.
    merge.ensure_integration_worktree(cfg)

    context = _plan_context(cfg, store)
    items, cost = backend.run_planner(cfg, context)
    store.add_spend(cost, cfg.cycle)
    if not items:
        store.log_event(cfg.cycle, "plan_empty")
        return {"action": "plan_empty"}

    floor = min(store.read_cov_floor(cfg.coverage_floor), cfg.coverage_floor_cap)
    for item in items:
        fs = FocusState(
            focus=item["focus"],
            branch=cfg.agent_branch(item["focus"]),
            cycle=cfg.cycle,
            difficulty=item.get("difficulty", "routine"),
            priority=int(item.get("priority", 5)),
            tasks=list(item.get("tasks", [])),
            acceptance=list(item.get("acceptance", [])),
            phase=PHASE_READY,
            test={"cov_floor": floor},
        )
        store.write_focus(fs)
    store.write_backlog(items)
    cycle["phase"] = CYCLE_WORKING
    store.write_cycle(cycle)
    store.log_event(cfg.cycle, "plan_done", foci=[i["focus"] for i in items], cost=cost)
    return {"action": "planned", "foci": [i["focus"] for i in items]}


def _advance_work(cfg, backend, store, cycle) -> dict[str, Any]:
    foci = store.all_foci(cfg.cycle)
    active = [f for f in foci if f.phase in ACTIVE_PHASES]
    if not active:
        cycle["phase"] = CYCLE_INTEGRATING
        store.write_cycle(cycle)
        store.log_event(cfg.cycle, "work_done")
        return {"action": "work_complete"}

    budget_ok = _budget_ok(cfg, store)
    # Start order: keep already-started foci moving first, then admit new ready ones
    # up to the concurrency cap.
    started = [f for f in active if f.phase != PHASE_READY]
    order = sorted(started, key=lambda f: f.priority)
    for f in sorted([f for f in active if f.phase == PHASE_READY], key=lambda f: f.priority):
        if len(order) >= cfg.max_concurrent:
            break
        order.append(f)

    advanced = []
    ops = 0
    for fs in order:
        if ops >= cfg.max_concurrent:
            break
        step = _advance_focus(cfg, backend, store, fs, budget_ok)
        if step:
            advanced.append({"focus": fs.focus, "step": step})
            ops += 1
    return {"action": "work", "advanced": advanced}


def _advance_focus(cfg, backend, store, fs: FocusState, budget_ok: bool) -> str | None:
    if fs.phase in (PHASE_READY, PHASE_BUILDING):
        if not budget_ok:
            return None
        return _build(cfg, backend, store, fs)
    if fs.phase == PHASE_TESTING:
        return _test(cfg, store, fs)
    if fs.phase == PHASE_REVIEWING:
        if not budget_ok:
            return None
        return _review(cfg, backend, store, fs)
    if fs.phase == PHASE_NEEDS_WORK:
        return _retry_or_kill(cfg, backend, store, fs, budget_ok)
    return None


def _build(cfg, backend, store, fs: FocusState) -> str:
    fs.phase = PHASE_BUILDING
    store.write_focus(fs)
    wt = worktree.create(cfg, fs.focus, cfg.integration_branch)
    feedback = fs.verdict.get("blocking_issues", []) if fs.verdict else []
    summary, cost = backend.run_builder(cfg, fs, wt, "\n".join(feedback))
    store.add_spend(cost, cfg.cycle)
    fs.summary = summary
    fs.cost_usd = round(fs.cost_usd + cost, 4)
    fs.phase = PHASE_TESTING
    store.write_focus(fs)
    store.log_event(cfg.cycle, "built", focus=fs.focus, attempt=fs.attempts, cost=cost)
    return "built"


def _test(cfg, store, fs: FocusState) -> str:
    wt = worktree.path_for(cfg, fs.focus)
    # Gate floor is clamped to the cap so the ratchet can't force feature work to hold
    # a near-impossible total-coverage bar set by earlier hardening units.
    floor = min(store.read_cov_floor(cfg.coverage_floor), cfg.coverage_floor_cap)
    # No-progress guard: identical diff across a retry means the builder is stuck.
    diff_hash = gitutil.diff_hash(cfg.integration_branch, cwd=wt)
    if fs.attempts > 0 and diff_hash == fs.last_diff_hash:
        return _kill(cfg, store, fs, "no progress between attempts")
    fs.last_diff_hash = diff_hash

    test = run_gates(cfg, wt, floor)
    test["cov_floor"] = floor
    fs.test = test
    if gate_passed(test):
        # Ratchet the coverage floor upward when this branch raised it — but never
        # above the cap.
        if test.get("cov_pct") and test["cov_pct"] > floor:
            store.write_cov_floor(min(test["cov_pct"], cfg.coverage_floor_cap))
        fs.phase = PHASE_REVIEWING
        store.write_focus(fs)
        store.log_event(cfg.cycle, "gate_green", focus=fs.focus, cov=test.get("cov_pct"))
        return "gate_green"
    fs.phase = PHASE_NEEDS_WORK
    store.write_focus(fs)
    store.log_event(cfg.cycle, "gate_red", focus=fs.focus,
                    failed=test.get("tests_failed"), cov=test.get("cov_pct"))
    return "gate_red"


def _review(cfg, backend, store, fs: FocusState) -> str:
    wt = worktree.path_for(cfg, fs.focus)
    # Hard block: a branch touching the frozen contract file is rejected outright.
    if contracts_touched(cfg, fs.branch):
        fs.verdict = {"verdict": "REJECT", "blocking_issues": ["touched frozen consumer contract"]}
        store.log_event(cfg.cycle, "CONTRACT_BLOCK", focus=fs.focus, alert=True)
        return _kill(cfg, store, fs, "touched frozen consumer contract")

    diff = gitutil.git("diff", cfg.integration_branch, cwd=wt, check=False).stdout
    verdict, cost = backend.run_verifier(cfg, fs, wt, diff)
    store.add_spend(cost, cfg.cycle)
    fs.verdict = verdict
    fs.cost_usd = round(fs.cost_usd + cost, 4)
    decision = verdict.get("verdict", "REJECT")
    if decision == "APPROVE":
        fs.phase = PHASE_APPROVED
        store.write_focus(fs)
        store.log_event(cfg.cycle, "approved", focus=fs.focus)
        return "approved"
    if decision == "NEEDS_WORK":
        fs.phase = PHASE_NEEDS_WORK
        store.write_focus(fs)
        store.log_event(cfg.cycle, "needs_work", focus=fs.focus,
                        issues=verdict.get("blocking_issues"))
        return "needs_work"
    return _kill(cfg, store, fs, "verifier rejected: " + "; ".join(verdict.get("blocking_issues", [])))


def _retry_or_kill(cfg, backend, store, fs: FocusState, budget_ok: bool) -> str | None:
    if fs.attempts >= cfg.max_build_retries:
        return _kill(cfg, store, fs, f"exhausted {cfg.max_build_retries} retries")
    if not budget_ok:
        return None
    fs.attempts += 1
    store.write_focus(fs)
    return _build(cfg, backend, store, fs)


def _kill(cfg, store, fs: FocusState, reason: str) -> str:
    fs.phase = PHASE_DEAD
    fs.notes.append(reason)
    store.write_focus(fs)
    dead = worktree.quarantine(cfg, fs.focus)
    store.log_event(cfg.cycle, "dead", focus=fs.focus, reason=reason, dead_branch=dead)
    return "dead"


def _do_integrate(cfg, backend, store, cycle) -> dict[str, Any]:
    foci = store.all_foci(cfg.cycle)
    approved = sorted([f for f in foci if f.phase == PHASE_APPROVED], key=lambda f: f.priority)
    if not approved:
        cycle["phase"] = CYCLE_RELEASING
        store.write_cycle(cycle)
        store.log_event(cfg.cycle, "integrate_done")
        return {"action": "integrate_complete"}

    fs = approved[0]  # one serialized merge per tick
    result = merge.merge_agent_branch(cfg, fs, backend)
    if result.get("merged"):
        fs.phase = PHASE_INTEGRATED
        store.write_focus(fs)
        # Force-delete: the commits are preserved in the (gated-green) integration
        # merge, so the bare branch ref is safe to drop even though `git branch -d`
        # run from the main checkout can't see it as merged into master.
        worktree.remove(cfg, fs.focus, delete_branch=True, force_branch=True)
        store.log_event(cfg.cycle, "integrated", focus=fs.focus)
        return {"action": "integrated", "focus": fs.focus}
    # Could not merge cleanly -> re-queue for a rebuild on fresh integration.
    if fs.attempts < cfg.max_build_retries:
        fs.attempts += 1
        fs.phase = PHASE_NEEDS_WORK
        fs.verdict = {"blocking_issues": [result.get("reason", "merge failed") +
                                          " — rebase onto latest integration"]}
        store.write_focus(fs)
        cycle["phase"] = CYCLE_WORKING
        store.write_cycle(cycle)
        store.log_event(cfg.cycle, "merge_requeue", focus=fs.focus, reason=result.get("reason"))
        return {"action": "merge_requeue", "focus": fs.focus, "reason": result.get("reason")}
    _kill(cfg, store, fs, "merge unrecoverable: " + result.get("reason", ""))
    return {"action": "merge_dead", "focus": fs.focus}


def _do_release(cfg, backend, store, cycle) -> dict[str, Any]:
    foci = store.all_foci(cfg.cycle)
    integrated = [f for f in foci if f.phase == PHASE_INTEGRATED]
    if not integrated:
        cycle["phase"] = CYCLE_DONE
        store.write_cycle(cycle)
        store.log_event(cfg.cycle, "release_skipped_empty")
        return {"action": "release_empty"}

    result = merge.release(cfg)
    cycle["phase"] = CYCLE_DONE
    cycle["release"] = result
    store.write_cycle(cycle)
    store.log_event(cfg.cycle, "released", **{k: result.get(k) for k in ("released", "version", "pushed")})
    return {"action": "released", **result}


def _start_next_cycle(cfg, backend, store, cycle) -> dict[str, Any]:
    prev = cfg.cycle
    nxt = next_cycle(prev)
    store.log_event(prev, "cycle_complete")
    # Archive this cycle's focus dirs by clearing them (events/jsonl are kept).
    new_cycle = {"cycle": nxt, "phase": CYCLE_PLANNING,
                 "integration_branch": f"{cfg.integration_prefix}{nxt}",
                 "started_at": now_iso(), "prev": prev}
    store.write_cycle(new_cycle)
    store.clear_backlog()
    store.log_event(nxt, "cycle_start", prev=prev)
    return {"action": "next_cycle", "from": prev, "to": nxt}


def _plan_context(cfg: Config, store: StateStore) -> str:
    """Assemble the planner's context from the last cycle's diff + roadmap pointer."""
    prev = store.read_cycle().get("prev")
    parts = [
        "Plan the next ujin development cycle.",
        f"Current cycle: {cfg.cycle}. Integration branch: {cfg.integration_branch}.",
        f"Max concurrent foci: {cfg.max_concurrent}. Coverage floor: "
        f"{store.read_cov_floor(cfg.coverage_floor)}%.",
        "Roadmap (see the approved plan): Track 1 Adaptive learning FIRST "
        "(site-store -> host-policy-signals -> strategy-feedback -> learned-rate-limit "
        "-> robots), then Track 2 multifunctional, Track 3 multiprocessing (measure-gated), "
        "Track 4 polish.",
    ]
    if prev:
        diff = gitutil.git("diff", "--stat", f"{cfg.base_branch}...{cfg.integration_branch}",
                           cwd=cfg.repo_root, check=False).stdout
        parts.append(f"Last integration diffstat vs {cfg.base_branch}:\n{diff[:4000]}")

    dead = _dead_foci(cfg)
    if dead:
        parts.append(
            "PREVIOUSLY-QUARANTINED (dead) foci — RE-ATTEMPT these this cycle if still "
            "on the roadmap; the blocker that killed them (e.g. the coverage floor) may "
            "now be resolved. Branch names encode focus-cycle:\n"
            + "\n".join(f"- {d}" for d in dead)
        )
    return "\n\n".join(parts)


def _dead_foci(cfg: Config) -> list[str]:
    """Live ``dead/agent/*`` branches — surfaced to the planner for retry until they
    either land or the daily GC removes them (7-day window)."""
    out = gitutil.git(
        "for-each-ref", "--format=%(refname:short)",
        f"refs/heads/{cfg.dead_prefix}{cfg.agent_prefix}",
        cwd=cfg.repo_root, check=False,
    ).stdout
    return [ln.strip() for ln in out.splitlines() if ln.strip()]
