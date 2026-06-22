"""Serialized integration + release.

All merges happen in dedicated worktrees so the orchestrator never disturbs whatever
branch the main checkout is on, and the editable-install PYTHONPATH discipline is kept.

INTEGRATE: merge each approved ``agent/<focus>`` into the integration branch one at a
time, re-running the full gate after each merge so the integration branch is always
green-by-construction. A merge that turns it red is reverted; the focus is re-queued.

RELEASE: finalize version + CHANGELOG (deterministic, templated) and merge the
integration branch into the base branch. Pushing is gated behind ``push_on_release``
(default off) so enabling origin writes is an explicit, separate decision.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import gitutil, worktree
from .agents import AgentBackend
from .config import Config
from .gates import gate_passed, run_gates
from .state import FocusState

INTEGRATION_WT = "__integration__"
RELEASE_WT = "__release__"


def _ensure_worktree(cfg: Config, name: str, branch: str, *, create_from: str | None = None) -> Path:
    wt = cfg.worktrees_dir / name
    root = cfg.repo_root
    if wt.exists():
        if gitutil.branch_exists(branch, root):
            gitutil.git("checkout", branch, cwd=wt, check=False)
        elif create_from is not None:
            # New cycle: the reused worktree must switch to a not-yet-created branch.
            gitutil.git("checkout", "-b", branch, create_from, cwd=wt, check=False)
        else:
            gitutil.git("checkout", branch, cwd=wt, check=False)
        return wt
    wt.parent.mkdir(parents=True, exist_ok=True)
    if gitutil.branch_exists(branch, root):
        gitutil.git("worktree", "add", "--force", str(wt), branch, cwd=root)
    elif create_from is not None:
        gitutil.git("worktree", "add", "--force", "-b", branch, str(wt), create_from, cwd=root)
    else:
        raise gitutil.GitError(["worktree", "add", branch], type("R", (), {
            "returncode": 1, "stdout": "", "stderr": f"branch {branch} missing"})())
    return wt


def ensure_integration_worktree(cfg: Config) -> Path:
    return _ensure_worktree(cfg, INTEGRATION_WT, cfg.integration_branch,
                            create_from=cfg.base_branch)


def merge_agent_branch(cfg: Config, fs: FocusState, backend: AgentBackend) -> dict[str, Any]:
    """Merge one approved agent branch into integration, with conflict triage + re-gate."""
    wt = ensure_integration_worktree(cfg)
    branch = fs.branch
    head_before = gitutil.git("rev-parse", "HEAD", cwd=wt).stdout.strip()

    res = gitutil.git("merge", "--no-ff", "-m", f"Merge {branch}", branch, cwd=wt, check=False)
    if not res.ok:
        conflicts = _conflicted_files(wt)
        triaged = _triage_conflicts(cfg, backend, wt, conflicts)
        if not triaged:
            gitutil.git("merge", "--abort", cwd=wt, check=False)
            return {"merged": False, "reason": "unresolvable conflict", "conflicts": conflicts}
        gitutil.git("add", "-A", cwd=wt, check=False)
        cont = gitutil.git("commit", "--no-edit", cwd=wt, check=False)
        if not cont.ok:
            gitutil.git("merge", "--abort", cwd=wt, check=False)
            return {"merged": False, "reason": "conflict commit failed", "conflicts": conflicts}

    # Re-gate the post-merge integration HEAD: never leave integration red.
    floor = cfg.coverage_floor
    test = run_gates(cfg, wt, floor)
    if not gate_passed(test):
        gitutil.git("reset", "--hard", head_before, cwd=wt, check=False)
        return {"merged": False, "reason": "integration gate red after merge", "test": test}

    return {"merged": True, "test": test}


def _conflicted_files(wt: Path) -> list[str]:
    r = gitutil.git("diff", "--name-only", "--diff-filter=U", cwd=wt, check=False)
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def _triage_conflicts(cfg: Config, backend: AgentBackend, wt: Path, conflicts: list[str]) -> bool:
    """Ask the cheap triage model to resolve trivial conflicts. Conservative: abstain on doubt."""
    if not conflicts or not hasattr(backend, "run_triage"):
        return False
    return bool(backend.run_triage(cfg, wt, conflicts))  # type: ignore[attr-defined]


def cycle_to_version(cycle: str) -> str:
    """'0.6' -> '0.6.0'; '1.2.3' stays. Best-effort semantic mapping."""
    parts = cycle.split(".")
    while len(parts) < 3:
        parts.append("0")
    return ".".join(parts[:3])


def finalize_release_files(cfg: Config, wt: Path, version: str) -> list[str]:
    """Deterministically bump __version__ and stamp the CHANGELOG [Unreleased] section."""
    touched: list[str] = []
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    init = wt / "ujin" / "__init__.py"
    if init.exists():
        text = init.read_text()
        new = re.sub(r'__version__\s*=\s*"[^"]*"', f'__version__ = "{version}"', text, count=1)
        if new != text:
            init.write_text(new)
            touched.append("ujin/__init__.py")

    changelog = wt / "CHANGELOG.md"
    if changelog.exists():
        text = changelog.read_text()
        if "## [Unreleased]" in text:
            new = text.replace("## [Unreleased]", f"## {version} — {date}", 1)
            changelog.write_text(new)
            touched.append("CHANGELOG.md")
    return touched


def release(cfg: Config) -> dict[str, Any]:
    """Finalize + merge integration -> base branch. Returns a result dict."""
    version = cycle_to_version(cfg.cycle)
    int_wt = ensure_integration_worktree(cfg)

    touched = finalize_release_files(cfg, int_wt, version)
    if touched:
        gitutil.git("add", *touched, cwd=int_wt, check=False)
        gitutil.git("commit", "-m", f"chore(release): {version}", cwd=int_wt, check=False)
        # The release commit changed code; re-gate once more.
        test = run_gates(cfg, int_wt, cfg.coverage_floor)
        if not gate_passed(test):
            gitutil.git("reset", "--hard", "HEAD~1", cwd=int_wt, check=False)
            return {"released": False, "reason": "release-finalize gate red", "test": test}

    if cfg.autonomy != "full_auto":
        return {"released": False, "reason": "supervised: human approval required for master",
                "version": version}

    rel_wt = _ensure_worktree(cfg, RELEASE_WT, cfg.base_branch)
    gitutil.git("merge", "--no-ff", "-m", f"Cycle {cfg.cycle}: {version}",
                cfg.integration_branch, cwd=rel_wt, check=False)
    head = gitutil.git("rev-parse", "HEAD", cwd=rel_wt).stdout.strip()

    pushed = False
    if getattr(cfg, "push_on_release", False):
        pr = gitutil.git("push", "origin", cfg.base_branch, cwd=rel_wt, check=False)
        pushed = pr.ok
    return {"released": True, "version": version, "master_head": head, "pushed": pushed}
