"""Agent backends: real ``claude -p`` headless agents, and a deterministic fake.

The fake backend (no LLM cost) is what the end-to-end dry-run and unit tests use to
exercise the worktree -> gate -> verify -> merge plumbing. The real backend is what
runs under systemd in production. Both satisfy the same ``AgentBackend`` protocol so
the cycle state machine is identical in test and prod.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from . import gitutil
from .config import PACKAGE_DIR, Config
from .state import FocusState
from .worktree import env_for

PROMPTS_DIR = PACKAGE_DIR / "prompts"

_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _read_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text()


def extract_json(text: str) -> Any:
    """Best-effort JSON extraction from an agent's free-text reply."""
    text = text.strip()
    if m := _FENCE.search(text):
        text = m.group(1).strip()
    # Trim to the outermost JSON array/object.
    start = min((i for i in (text.find("{"), text.find("[")) if i != -1), default=-1)
    if start == -1:
        raise ValueError("no JSON found in agent reply")
    end = max(text.rfind("}"), text.rfind("]"))
    return json.loads(text[start : end + 1])


@dataclass
class AgentResult:
    text: str
    cost_usd: float
    ok: bool
    error: str = ""


class AgentBackend(Protocol):
    def run_planner(self, cfg: Config, context: str) -> tuple[list[dict], float]: ...
    def run_builder(self, cfg: Config, fs: FocusState, wt: Path, feedback: str) -> tuple[str, float]: ...
    def run_verifier(self, cfg: Config, fs: FocusState, wt: Path, diff_text: str) -> tuple[dict, float]: ...


# --------------------------------------------------------------------------- #
# Real backend: claude -p
# --------------------------------------------------------------------------- #
class ClaudeAgentBackend:
    def _invoke(
        self,
        cfg: Config,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        cwd: Path,
        timeout: int,
        budget: float,
        read_only: bool = False,
    ) -> AgentResult:
        argv = [
            cfg.claude_bin,
            "-p", user_prompt,
            "--output-format", "json",
            "--model", model,
            "--max-budget-usd", str(budget),
            "--add-dir", str(cwd),
            "--append-system-prompt", system_prompt,
        ]
        if read_only:
            # Planner/verifier only emit JSON — deny file mutation so they are safe to
            # run anywhere. Read/grep tools stay available for inspection.
            argv += ["--disallowedTools", "Edit", "Write", "NotebookEdit"]
        argv.append(cfg.permission_flag)
        try:
            proc = subprocess.run(
                argv, cwd=str(cwd), env=env_for(cwd),
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return AgentResult("", 0.0, False, f"timeout after {timeout}s")
        if proc.returncode != 0:
            return AgentResult("", 0.0, False, proc.stderr.strip()[-500:])
        try:
            obj = json.loads(proc.stdout)
        except json.JSONDecodeError:
            # text leaked outside the JSON envelope; treat stdout as the reply.
            return AgentResult(proc.stdout, 0.0, True)
        return AgentResult(
            text=str(obj.get("result", "")),
            cost_usd=float(obj.get("total_cost_usd", 0.0) or 0.0),
            ok=not obj.get("is_error", False),
            error=str(obj.get("error", "")),
        )

    def run_planner(self, cfg: Config, context: str) -> tuple[list[dict], float]:
        r = self._invoke(
            cfg, model=cfg.models.planner, system_prompt=_read_prompt("planner"),
            user_prompt=context, cwd=cfg.repo_root, timeout=cfg.planner_timeout_s,
            budget=cfg.budgets.per_agent, read_only=True,
        )
        if not r.ok:
            return [], r.cost_usd
        try:
            items = extract_json(r.text)
            return (items if isinstance(items, list) else []), r.cost_usd
        except (ValueError, json.JSONDecodeError):
            return [], r.cost_usd

    def run_builder(self, cfg: Config, fs: FocusState, wt: Path, feedback: str) -> tuple[str, float]:
        task = _format_builder_task(cfg, fs, feedback)
        r = self._invoke(
            cfg, model=cfg.models.builder_for(fs.difficulty),
            system_prompt=_read_prompt("builder"), user_prompt=task, cwd=wt,
            timeout=cfg.builder_timeout_s, budget=cfg.budgets.per_agent,
        )
        # Ensure the builder's work is committed even if it forgot to.
        _autocommit(wt, fs)
        return (r.text or r.error or "(no summary)"), r.cost_usd

    def run_verifier(self, cfg: Config, fs: FocusState, wt: Path, diff_text: str) -> tuple[dict, float]:
        prompt = _format_verifier_task(fs, diff_text)
        total_cost = 0.0
        for _ in range(3):  # re-prompt on unparseable JSON, then fail-safe REJECT
            r = self._invoke(
                cfg, model=cfg.models.verifier, system_prompt=_read_prompt("verifier"),
                user_prompt=prompt, cwd=wt, timeout=cfg.verifier_timeout_s,
                budget=cfg.budgets.per_agent, read_only=True,
            )
            total_cost += r.cost_usd
            if not r.ok:
                continue
            try:
                verdict = extract_json(r.text)
                if isinstance(verdict, dict) and "verdict" in verdict:
                    return verdict, total_cost
            except (ValueError, json.JSONDecodeError):
                continue
        return _failsafe_reject(fs, "verifier produced no parseable verdict"), total_cost


# --------------------------------------------------------------------------- #
# Fake backend: deterministic, no LLM — for the dry-run and unit tests
# --------------------------------------------------------------------------- #
class FakeAgentBackend:
    """Performs a real, trivial, additive edit so the full pipeline runs offline."""

    def __init__(self, backlog: list[dict] | None = None) -> None:
        self._backlog = backlog or [{
            "focus": "dryrun-noop",
            "difficulty": "routine",
            "priority": 1,
            "tasks": ["append a marker line to a docs file"],
            "acceptance": ["docs file contains the marker", "gate stays green"],
        }]

    def run_planner(self, cfg: Config, context: str) -> tuple[list[dict], float]:
        return list(self._backlog), 0.0

    def run_builder(self, cfg: Config, fs: FocusState, wt: Path, feedback: str) -> tuple[str, float]:
        marker = wt / "docs" / f"dryrun-{fs.focus}.md"
        marker.parent.mkdir(parents=True, exist_ok=True)
        line = f"- dry-run marker for {fs.focus} (attempt {fs.attempts})\n"
        with marker.open("a") as fh:
            fh.write(line)
        _autocommit(wt, fs)
        return f"Fake builder appended a marker for {fs.focus}.", 0.0

    def run_verifier(self, cfg: Config, fs: FocusState, wt: Path, diff_text: str) -> tuple[dict, float]:
        passed = bool(fs.test.get("tests_green") and fs.test.get("coverage_ok") and fs.test.get("bench_ok"))
        verdict = {
            "verdict": "APPROVE" if passed else "NEEDS_WORK",
            "branch": fs.branch,
            "checks": {
                "tests_green": bool(fs.test.get("tests_green")),
                "coverage_ok": bool(fs.test.get("coverage_ok")),
                "bench_ok": bool(fs.test.get("bench_ok")),
                "contracts_untouched": True,
                "diff_matches_acceptance": True,
                "no_scope_creep": True,
            },
            "blocking_issues": [] if passed else ["gate not green"],
            "confidence": 1.0,
        }
        return verdict, 0.0


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _format_builder_task(cfg: Config, fs: FocusState, feedback: str) -> str:
    parts = [
        f"FOCUS: {fs.focus}",
        f"Coverage floor: {fs.test.get('cov_floor', cfg.coverage_floor)}%",
        "TASKS:\n" + "\n".join(f"- {t}" for t in fs.tasks),
        "ACCEPTANCE CRITERIA:\n" + "\n".join(f"- {a}" for a in fs.acceptance),
    ]
    if feedback:
        parts.append("PRIOR REVIEW FEEDBACK (address this):\n" + feedback)
    return "\n\n".join(parts)


def _format_verifier_task(fs: FocusState, diff_text: str) -> str:
    return (
        f"BRANCH: {fs.branch}\n\n"
        f"ACCEPTANCE CRITERIA:\n" + "\n".join(f"- {a}" for a in fs.acceptance) + "\n\n"
        f"test.json:\n{json.dumps(fs.test, indent=2)}\n\n"
        f"UNIFIED DIFF:\n{diff_text[:60000]}"
    )


def _autocommit(wt: Path, fs: FocusState) -> None:
    """Stage and commit any uncommitted work on the branch (idempotent)."""
    status = gitutil.git("status", "--porcelain", cwd=wt, check=False)
    if not status.stdout.strip():
        return
    gitutil.git("add", "-A", cwd=wt, check=False)
    gitutil.git(
        "commit", "-m", f"wip({fs.focus}): autocommit attempt {fs.attempts}",
        cwd=wt, check=False,
    )


def _failsafe_reject(fs: FocusState, reason: str) -> dict:
    return {
        "verdict": "REJECT",
        "branch": fs.branch,
        "checks": {k: False for k in (
            "tests_green", "coverage_ok", "bench_ok",
            "contracts_untouched", "diff_matches_acceptance", "no_scope_creep")},
        "blocking_issues": [reason],
        "confidence": 0.0,
    }
