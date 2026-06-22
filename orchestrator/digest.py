"""Human-facing summaries — a daily digest (Telegram-ready) and a status view.

Pure formatting over ``StateStore`` data; no side effects. The systemd timer or a
``--digest`` invocation feeds this text to the Telegram MCP / journald.
"""

from __future__ import annotations

from collections import Counter

from .config import Config
from .state import StateStore


def status_text(cfg: Config, store: StateStore) -> str:
    cycle = store.read_cycle()
    if not cycle:
        return "orchestrator: no cycle started yet."
    cur = cycle.get("cycle", cfg.cycle)
    foci = store.all_foci(cur)
    budget = store.read_budget()
    lines = [
        f"cycle {cur} — phase: {cycle.get('phase')}  (integration: {cycle.get('integration_branch')})",
        f"budget: ${budget.get('spent_cycle', 0):.2f}/cycle  ${budget.get('spent_today', 0):.2f}/day",
        f"cov floor: {store.read_cov_floor(cfg.coverage_floor)}%",
        "foci:",
    ]
    if not foci:
        lines.append("  (none)")
    for f in sorted(foci, key=lambda x: x.priority):
        cov = f.test.get("cov_pct")
        cov_s = f"{cov}%" if cov is not None else "–"
        lines.append(f"  [{f.phase:<10}] {f.focus:<24} attempts={f.attempts} "
                     f"cov={cov_s} ${f.cost_usd:.2f}")
    if store.killed:
        lines.append("** KILL switch present — ticks are no-ops **")
    return "\n".join(lines)


def digest_text(cfg: Config, store: StateStore, cycle: str | None = None) -> str:
    cyc = cycle or store.read_cycle().get("cycle", cfg.cycle)
    events = store.read_events(cyc)
    counts = Counter(e["event"] for e in events)
    foci = store.all_foci(cyc)
    by_phase = Counter(f.phase for f in foci)
    budget = store.read_budget()
    alerts = [e for e in events if e.get("alert") or e["event"] == "CONTRACT_BLOCK"]

    lines = [
        f"🤖 ujin orchestrator — cycle {cyc} digest",
        f"phase: {store.read_cycle().get('phase')}",
        f"foci: " + ", ".join(f"{p}={n}" for p, n in sorted(by_phase.items())) or "foci: none",
        f"integrated: {counts.get('integrated', 0)}  dead: {counts.get('dead', 0)}  "
        f"requeues: {counts.get('merge_requeue', 0)}",
        f"spend: ${budget.get('spent_cycle', 0):.2f} this cycle / ${budget.get('spent_today', 0):.2f} today",
    ]
    rel = store.read_cycle().get("release")
    if rel:
        lines.append(f"release: {rel}")
    if alerts:
        lines.append("⚠️ ALERTS:")
        for a in alerts:
            lines.append(f"  - {a['event']}: {a.get('focus', '')} {a.get('reason', '')}")
    return "\n".join(lines)
