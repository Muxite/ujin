"""Deterministic quality gates — the source of truth for green/coverage/bench.

No LLM here. ``run_gates`` executes the project's own ``make`` targets inside a
worktree (with the PYTHONPATH editable-install fix) and parses their output into a
``test.json``-shaped dict. ``contracts_touched`` is the full-auto hard block: a branch
that edits the frozen consumer-contract test file is rejected outright.

The two gate layers together cover the "additive only" rule:
  * removing/renaming a public symbol -> the consumer-contract *tests* fail (make test
    goes red) -> caught by ``tests_green``.
  * weakening the contract tests themselves -> caught by ``contracts_touched``.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from . import gitutil, worktree
from .config import Config

_COV_TOTAL = re.compile(r"^TOTAL\s+.*?(\d+(?:\.\d+)?)%\s*$", re.MULTILINE)
_PASSED = re.compile(r"(\d+)\s+passed")
_FAILED = re.compile(r"(\d+)\s+failed")
_ERRORS = re.compile(r"(\d+)\s+error")


def _run(cmd: tuple[str, ...], cwd: Path, env: dict[str, str], timeout: int) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            list(cmd), cwd=str(cwd), env=env,
            capture_output=True, text=True, timeout=timeout,
        )
        return proc.returncode, (proc.stdout + "\n" + proc.stderr)
    except subprocess.TimeoutExpired as exc:
        return 124, f"TIMEOUT after {timeout}s\n{exc.stdout or ''}{exc.stderr or ''}"


def parse_coverage(output: str) -> float | None:
    m = _COV_TOTAL.search(output)
    return float(m.group(1)) if m else None


def parse_tests(output: str) -> tuple[int, int]:
    passed = int(m.group(1)) if (m := _PASSED.search(output)) else 0
    failed = int(m.group(1)) if (m := _FAILED.search(output)) else 0
    errors = int(m.group(1)) if (m := _ERRORS.search(output)) else 0
    return passed, failed + errors


def contracts_touched(cfg: Config, head_ref: str) -> list[str]:
    """Contract files changed between the integration base and ``head_ref``."""
    changed = gitutil.changed_files(cfg.integration_branch, head_ref, cwd=cfg.repo_root)
    return [p for p in changed if p in cfg.contract_paths]


def run_gates(cfg: Config, wt: Path, floor: float, timeout: int = 1800) -> dict[str, Any]:
    """Run cov (= tests + coverage) and bench in the worktree; return a test.json dict."""
    worktree.verify_import(wt)  # fail loud on wrong-tree before trusting any result
    env = worktree.env_for(wt)

    cov_rc, cov_out = _run(cfg.cov_cmd, wt, env, timeout)
    passed, failed = parse_tests(cov_out)
    cov_pct = parse_coverage(cov_out)
    tests_green = cov_rc == 0 or (failed == 0 and cov_rc != 124 and "TIMEOUT" not in cov_out)
    # pytest-cov exits non-zero on EITHER a test failure OR coverage < fail_under.
    if failed > 0:
        tests_green = False
    coverage_ok = cov_pct is not None and cov_pct >= floor

    bench_rc, bench_out = _run(cfg.bench_cmd, wt, env, timeout)
    bench_ok = bench_rc == 0

    return {
        "exit": cov_rc,
        "tests_passed": passed,
        "tests_failed": failed,
        "tests_green": bool(tests_green and failed == 0),
        "cov_pct": cov_pct,
        "cov_floor": floor,
        "coverage_ok": bool(coverage_ok),
        "bench_ok": bool(bench_ok),
        "cov_tail": "\n".join(cov_out.strip().splitlines()[-25:]),
        "bench_tail": "\n".join(bench_out.strip().splitlines()[-15:]),
    }


def gate_passed(test: dict[str, Any]) -> bool:
    return bool(test.get("tests_green") and test.get("coverage_ok") and test.get("bench_ok"))
