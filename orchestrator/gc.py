"""Garbage collection: prune stale worktrees and old quarantined branches.

Run by ``ujin-orchestrator-gc.timer`` (or ``python3 -m orchestrator.gc``). Deletes
``dead/*`` branches whose tip is older than ``max_age_days`` and prunes detached
worktrees. Conservative: only touches ``dead/*``, never active branches.
"""

from __future__ import annotations

import sys
import time

from . import config as config_mod
from . import gitutil
from .config import Config


def run_gc(cfg: Config, max_age_days: int = 7) -> dict:
    root = cfg.repo_root
    gitutil.git("worktree", "prune", cwd=root, check=False)

    cutoff = time.time() - max_age_days * 86400
    listed = gitutil.git(
        "for-each-ref", "--format=%(refname:short) %(committerdate:unix)",
        f"refs/heads/{cfg.dead_prefix}", cwd=root, check=False,
    ).stdout
    deleted = []
    for line in listed.splitlines():
        if not line.strip():
            continue
        name, _, ts = line.rpartition(" ")
        try:
            when = int(ts)
        except ValueError:
            continue
        if when < cutoff:
            gitutil.git("branch", "-D", name, cwd=root, check=False)
            deleted.append(name)
    return {"pruned_worktrees": True, "deleted_dead_branches": deleted}


def main(argv: list[str] | None = None) -> int:
    cfg = config_mod.load()
    print(run_gc(cfg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
