"""Orchestrator configuration — loaded from ``orchestrator/config.toml``.

All knobs the human controls live here: autonomy posture, model tiers, concurrency
and budget caps, paths, and the gate command. Defaults are conservative; the TOML
file overrides them. Parsing uses stdlib ``tomllib`` (Python 3.11+).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

# Repo root is the parent of this package directory.
PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parent
DEFAULT_CONFIG_PATH = PACKAGE_DIR / "config.toml"


@dataclass(frozen=True)
class Models:
    """Model tier per role. ``builder_*`` is chosen by a backlog item's difficulty."""

    planner: str = "opus"
    builder_hard: str = "opus"
    builder_routine: str = "sonnet"
    verifier: str = "haiku"
    triage: str = "haiku"

    def builder_for(self, difficulty: str) -> str:
        return self.builder_hard if difficulty == "hard" else self.builder_routine


@dataclass(frozen=True)
class Budgets:
    """Cost caps in USD. ``per_agent`` is enforced by the CLI's ``--max-budget-usd``."""

    per_agent: float = 5.0
    per_cycle: float = 40.0
    per_day: float = 100.0


@dataclass(frozen=True)
class Config:
    # --- paths (all absolute, derived from repo_root) ---
    repo_root: Path = REPO_ROOT
    state_dirname: str = "orchestrator/state"
    worktrees_dirname: str = ".claude/worktrees"

    # --- git topology ---
    base_branch: str = "master"
    integration_prefix: str = "integration/cycle-"
    cycle: str = "0.6"  # the cycle currently being assembled
    agent_prefix: str = "agent/"
    dead_prefix: str = "dead/"

    # --- autonomy / safety ---
    # "full_auto" merges all the way to master; "supervised" stops before master.
    autonomy: str = "full_auto"
    # Whether a successful release pushes the base branch to origin. Default off so
    # enabling origin writes is an explicit, separate decision.
    push_on_release: bool = False
    max_concurrent: int = 3
    max_build_retries: int = 2
    coverage_floor: float = 85.0
    # The ratchet never raises the floor above this — keeps feature work from being
    # forced to maintain a near-impossible total-coverage bar set by hardening units.
    coverage_floor_cap: float = 90.0
    bench_tolerance: float = 4.0
    # Paths (relative to repo root) whose modification hard-blocks a merge.
    contract_paths: tuple[str, ...] = ("tests/test_consumer_contracts.py",)

    # --- gate command (reused identically by humans via `make gate`) ---
    gate_cmd: tuple[str, ...] = ("make", "gate")
    test_cmd: tuple[str, ...] = ("make", "test")
    cov_cmd: tuple[str, ...] = ("make", "cov")
    bench_cmd: tuple[str, ...] = ("make", "bench")

    # --- agent runtime ---
    claude_bin: str = "claude"
    # Use regular Claude Code subscription auth (drop API-key env vars from agent
    # subprocesses) so the loop consumes plan usage, never metered per-token billing.
    use_subscription_auth: bool = True
    # Per-agent wall-clock ceilings (seconds). 2.1.185 has no --max-turns.
    builder_timeout_s: int = 5400
    verifier_timeout_s: int = 900
    planner_timeout_s: int = 1800
    permission_flag: str = "--dangerously-skip-permissions"

    # --- nested ---
    models: Models = field(default_factory=Models)
    budgets: Budgets = field(default_factory=Budgets)

    # ---- derived paths ----
    @property
    def state_dir(self) -> Path:
        return self.repo_root / self.state_dirname

    @property
    def worktrees_dir(self) -> Path:
        return self.repo_root / self.worktrees_dirname

    @property
    def kill_file(self) -> Path:
        return self.state_dir / "KILL"

    @property
    def integration_branch(self) -> str:
        return f"{self.integration_prefix}{self.cycle}"

    def agent_branch(self, focus: str) -> str:
        return f"{self.agent_prefix}{focus}"

    def dead_branch(self, focus: str) -> str:
        return f"{self.dead_prefix}{self.agent_prefix}{focus}-{self.cycle}"


def _coerce(raw: dict, dc_type):
    """Build a dataclass from a dict, recursing into nested dataclass fields."""
    kwargs = {}
    type_by_name = {f.name: f.type for f in fields(dc_type)}
    for key, value in raw.items():
        if key not in type_by_name:
            continue  # ignore unknown keys (forward-compat)
        if key == "models":
            kwargs[key] = _coerce(value, Models)
        elif key == "budgets":
            kwargs[key] = _coerce(value, Budgets)
        elif key in ("contract_paths", "gate_cmd", "test_cmd", "cov_cmd", "bench_cmd"):
            kwargs[key] = tuple(value)
        elif key == "repo_root":
            kwargs[key] = Path(value)
        else:
            kwargs[key] = value
    return dc_type(**kwargs)


def load(path: Path | str | None = None) -> Config:
    """Load config from TOML, falling back to dataclass defaults for absent keys."""
    p = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if not p.exists():
        return Config()
    with p.open("rb") as fh:
        raw = tomllib.load(fh)
    return _coerce(raw, Config)
