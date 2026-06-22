"""Hermetic fixtures: a throwaway git repo + a Config wired to fake gate commands.

Tests never touch the real ujin repo. The temp repo carries a minimal importable
`ujin` package (so verify_import passes), a CHANGELOG with an [Unreleased] section, and
a placeholder consumer-contract file (so the contract-block rail is exercisable).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from orchestrator.config import Config, Models, Budgets

# Fake gate commands: deterministic, fast, no real pytest. `cov` prints a parseable
# coverage TOTAL line; `bench` just succeeds. Override per-test for failure cases.
COV_OK = ("bash", "-c", "echo '1 passed in 0.01s'; echo 'TOTAL      10      0     95%'")
COV_LOW = ("bash", "-c", "echo '1 passed in 0.01s'; echo 'TOTAL      10      6     50%'")
COV_FAIL = ("bash", "-c", "echo '1 failed, 0 passed'; echo 'TOTAL 10 0 95%'; exit 1")
BENCH_OK = ("bash", "-c", "exit 0")


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(root), check=True,
                   capture_output=True, text=True)


@pytest.fixture
def temp_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "master")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "commit.gpgsign", "false")

    (root / "ujin").mkdir()
    (root / "ujin" / "__init__.py").write_text('__version__ = "0.5.0"\n')
    (root / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n## 0.5.0 — 2026-06-17\n\nInitial.\n"
    )
    (root / "tests").mkdir()
    (root / "tests" / "test_consumer_contracts.py").write_text(
        "def test_contract():\n    assert True\n"
    )
    (root / "docs").mkdir()
    (root / "docs" / ".keep").write_text("")
    (root / "Makefile").write_text("gate:\n\ttrue\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "initial")
    return root


def make_cfg(root: Path, **over) -> Config:
    base = dict(
        repo_root=root,
        state_dirname="orchestrator/state",
        worktrees_dirname=".wt",
        base_branch="master",
        cycle="0.6",
        cov_cmd=COV_OK,
        bench_cmd=BENCH_OK,
        max_concurrent=3,
        max_build_retries=2,
        coverage_floor=85.0,
        models=Models(),
        budgets=Budgets(),
    )
    base.update(over)
    return Config(**base)
