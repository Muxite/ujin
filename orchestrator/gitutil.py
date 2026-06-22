"""Thin, testable wrapper around the ``git`` CLI.

Every call is explicit about ``cwd`` so worktrees and the main checkout never get
confused. Raises ``GitError`` with captured stderr on failure unless ``check=False``.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    def __init__(self, args: list[str], result: "subprocess.CompletedProcess") -> None:
        self.args_run = args
        self.returncode = result.returncode
        self.stdout = result.stdout
        self.stderr = result.stderr
        super().__init__(
            f"git {' '.join(args)} -> exit {result.returncode}\n{result.stderr.strip()}"
        )


@dataclass
class GitResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def git(*args: str, cwd: Path | str, check: bool = True, timeout: int = 300) -> GitResult:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        raise GitError(list(args), proc)
    return GitResult(proc.returncode, proc.stdout, proc.stderr)


def current_branch(cwd: Path | str) -> str:
    return git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd).stdout.strip()


def branch_exists(name: str, cwd: Path | str) -> bool:
    r = git("rev-parse", "--verify", "--quiet", f"refs/heads/{name}", cwd=cwd, check=False)
    return r.ok


def changed_files(base: str, head: str, cwd: Path | str) -> list[str]:
    """Files changed between ``base`` and ``head`` (name-only, three-dot diff)."""
    r = git("diff", "--name-only", f"{base}...{head}", cwd=cwd)
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def diff_hash(base: str, cwd: Path | str) -> str:
    """A stable hash of the working diff vs ``base`` (no-progress detection)."""
    import hashlib

    r = git("diff", base, cwd=cwd, check=False)
    return hashlib.sha256(r.stdout.encode()).hexdigest()[:16]
