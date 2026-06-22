"""Persistent, crash-safe state for the orchestrator.

Everything lives as JSON/JSONL under ``orchestrator/state/`` (gitignored), so a tick
can die at any point and the next tick resumes from disk. Writes are atomic
(write-temp-then-rename). This module is pure I/O — no orchestration logic.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# Per-focus lifecycle phases.
PHASE_READY = "ready"
PHASE_BUILDING = "building"
PHASE_TESTING = "testing"
PHASE_REVIEWING = "reviewing"
PHASE_NEEDS_WORK = "needs_work"
PHASE_APPROVED = "approved"
PHASE_INTEGRATED = "integrated"
PHASE_DEAD = "dead"

TERMINAL_PHASES = frozenset({PHASE_INTEGRATED, PHASE_DEAD})
ACTIVE_PHASES = frozenset(
    {PHASE_READY, PHASE_BUILDING, PHASE_TESTING, PHASE_REVIEWING, PHASE_NEEDS_WORK}
)

# Cycle-level phases.
CYCLE_PLANNING = "planning"
CYCLE_WORKING = "working"
CYCLE_INTEGRATING = "integrating"
CYCLE_RELEASING = "releasing"
CYCLE_DONE = "done"


@dataclass
class FocusState:
    focus: str
    branch: str
    cycle: str = ""
    difficulty: str = "routine"
    priority: int = 5
    phase: str = PHASE_READY
    attempts: int = 0
    tasks: list[str] = field(default_factory=list)
    acceptance: list[str] = field(default_factory=list)
    summary: str = ""
    last_diff_hash: str = ""
    test: dict[str, Any] = field(default_factory=dict)
    verdict: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    updated_at: str = field(default_factory=now_iso)


def _atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data)
    os.replace(tmp, path)


class StateStore:
    """Filesystem-backed state under a single directory."""

    def __init__(self, state_dir: Path) -> None:
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    # ---- generic json ----
    def _read_json(self, name: str, default: Any) -> Any:
        p = self.dir / name
        if not p.exists():
            return default
        return json.loads(p.read_text())

    def _write_json(self, name: str, obj: Any) -> None:
        _atomic_write(self.dir / name, json.dumps(obj, indent=2, sort_keys=True))

    # ---- kill switch ----
    @property
    def killed(self) -> bool:
        return (self.dir / "KILL").exists()

    # ---- cycle ----
    def read_cycle(self) -> dict[str, Any]:
        return self._read_json("cycle.json", {})

    def write_cycle(self, obj: dict[str, Any]) -> None:
        self._write_json("cycle.json", obj)

    # ---- backlog ----
    def read_backlog(self) -> list[dict[str, Any]]:
        return self._read_json("backlog.json", [])

    def write_backlog(self, items: list[dict[str, Any]]) -> None:
        self._write_json("backlog.json", items)

    def clear_backlog(self) -> None:
        self._write_json("backlog.json", [])

    # ---- per-focus ----
    def _focus_path(self, focus: str) -> str:
        return f"{focus}/status.json"

    def read_focus(self, focus: str) -> FocusState | None:
        raw = self._read_json(self._focus_path(focus), None)
        if raw is None:
            return None
        return FocusState(**raw)

    def write_focus(self, fs: FocusState) -> None:
        fs.updated_at = now_iso()
        self._write_json(self._focus_path(fs.focus), asdict(fs))

    def all_foci(self, cycle: str | None = None) -> list[FocusState]:
        out: list[FocusState] = []
        for child in sorted(self.dir.iterdir()):
            status = child / "status.json"
            if child.is_dir() and status.exists():
                fs = FocusState(**json.loads(status.read_text()))
                if cycle is None or fs.cycle == cycle:
                    out.append(fs)
        return out

    # ---- coverage floor (ratchet) ----
    def read_cov_floor(self, default: float) -> float:
        p = self.dir / "cov_floor.txt"
        if not p.exists():
            return default
        try:
            return float(p.read_text().strip())
        except ValueError:
            return default

    def write_cov_floor(self, value: float) -> None:
        _atomic_write(self.dir / "cov_floor.txt", f"{value:.4f}\n")

    # ---- budget ----
    def read_budget(self) -> dict[str, Any]:
        b = self._read_json("budget.json", {})
        # Roll over the daily counter at UTC midnight.
        if b.get("day") != today():
            b = {"day": today(), "spent_today": 0.0,
                 "cycle": b.get("cycle"), "spent_cycle": b.get("spent_cycle", 0.0)}
        return b

    def add_spend(self, usd: float, cycle: str) -> dict[str, Any]:
        b = self.read_budget()
        if b.get("cycle") != cycle:
            b["cycle"] = cycle
            b["spent_cycle"] = 0.0
        b["spent_today"] = round(b.get("spent_today", 0.0) + usd, 4)
        b["spent_cycle"] = round(b.get("spent_cycle", 0.0) + usd, 4)
        self._write_json("budget.json", b)
        return b

    # ---- event log (append-only jsonl) ----
    def log_event(self, cycle: str, event: str, **fields: Any) -> None:
        rec = {"ts": now_iso(), "cycle": cycle, "event": event, **fields}
        path = self.dir / f"cycle-{cycle}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")

    def read_events(self, cycle: str) -> list[dict[str, Any]]:
        path = self.dir / f"cycle-{cycle}.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
