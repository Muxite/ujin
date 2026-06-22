"""Git worktree lifecycle for isolated parallel agents.

Each ``agent/<focus>`` gets its own worktree under ``.claude/worktrees/<focus>``
(already gitignored). The critical gotcha: ujin is installed editable, so any process
in a worktree imports the *main checkout's* ``ujin`` unless ``PYTHONPATH`` points at
the worktree. ``env_for`` and ``verify_import`` enforce that.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import gitutil
from .config import Config


def path_for(cfg: Config, focus: str) -> Path:
    return cfg.worktrees_dir / focus


def env_for(worktree: Path) -> dict[str, str]:
    """Process env that forces imports to resolve inside the worktree."""
    env = dict(os.environ)
    wt = str(Path(worktree).resolve())
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{wt}{os.pathsep}{existing}" if existing else wt
    return env


def verify_import(worktree: Path, package: str = "ujin") -> None:
    """Assert ``import <package>`` resolves inside the worktree (not the main checkout).

    Guards against the wrong-tree false-green: tests passing against code that isn't
    the code being reviewed. Raises RuntimeError on mismatch.
    """
    wt = Path(worktree).resolve()
    proc = subprocess.run(
        ["python3", "-c", f"import {package}, sys; print({package}.__file__)"],
        cwd=str(wt),
        env=env_for(wt),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"import {package} failed in worktree:\n{proc.stderr.strip()}")
    resolved = Path(proc.stdout.strip()).resolve()
    if wt not in resolved.parents:
        raise RuntimeError(
            f"WRONG-TREE: {package} resolves to {resolved}, expected inside {wt}. "
            "Set PYTHONPATH to the worktree."
        )


def create(cfg: Config, focus: str, base: str) -> Path:
    """Create (or reuse) a worktree + ``agent/<focus>`` branch off ``base``."""
    wt = path_for(cfg, focus)
    branch = cfg.agent_branch(focus)
    root = cfg.repo_root

    if wt.exists():
        return wt
    wt.parent.mkdir(parents=True, exist_ok=True)

    if gitutil.branch_exists(branch, root):
        # Re-attach an existing branch (resume after a crash).
        gitutil.git("worktree", "add", "--force", str(wt), branch, cwd=root)
    else:
        gitutil.git("worktree", "add", "--no-track", "-b", branch, str(wt), base, cwd=root)
    return wt


def remove(cfg: Config, focus: str, *, delete_branch: bool = False, force_branch: bool = False) -> None:
    wt = path_for(cfg, focus)
    root = cfg.repo_root
    if wt.exists():
        gitutil.git("worktree", "remove", "--force", str(wt), cwd=root, check=False)
    gitutil.git("worktree", "prune", cwd=root, check=False)
    if delete_branch:
        flag = "-D" if force_branch else "-d"
        gitutil.git("branch", flag, cfg.agent_branch(focus), cwd=root, check=False)


def quarantine(cfg: Config, focus: str) -> str:
    """Rename a dead ``agent/<focus>`` branch to ``dead/...`` and drop its worktree."""
    root = cfg.repo_root
    src = cfg.agent_branch(focus)
    dst = cfg.dead_branch(focus)
    wt = path_for(cfg, focus)
    if wt.exists():
        gitutil.git("worktree", "remove", "--force", str(wt), cwd=root, check=False)
    gitutil.git("worktree", "prune", cwd=root, check=False)
    if gitutil.branch_exists(src, root):
        gitutil.git("branch", "-m", src, dst, cwd=root, check=False)
    return dst
