"""Unit tests for config loading, state persistence, and gate parsing."""

from __future__ import annotations

import textwrap
from pathlib import Path

from orchestrator import config as config_mod
from orchestrator.config import Config
from orchestrator.gates import gate_passed, parse_coverage, parse_tests
from orchestrator.state import FocusState, StateStore, today


def test_config_defaults_and_derived():
    cfg = Config(repo_root=Path("/tmp/x"), cycle="0.6")
    assert cfg.integration_branch == "integration/cycle-0.6"
    assert cfg.agent_branch("site-store") == "agent/site-store"
    assert cfg.dead_branch("site-store") == "dead/agent/site-store-0.6"
    assert cfg.models.builder_for("hard") == "opus"
    assert cfg.models.builder_for("routine") == "sonnet"


def test_config_toml_override(tmp_path: Path):
    p = tmp_path / "c.toml"
    p.write_text(textwrap.dedent("""
        cycle = "1.2"
        max_concurrent = 7
        autonomy = "supervised"
        [models]
        builder_routine = "haiku"
        [budgets]
        per_cycle = 12.5
    """))
    cfg = config_mod.load(p)
    assert cfg.cycle == "1.2"
    assert cfg.max_concurrent == 7
    assert cfg.autonomy == "supervised"
    assert cfg.models.builder_routine == "haiku"
    assert cfg.budgets.per_cycle == 12.5


def test_parse_coverage_and_tests():
    out = "stuff\nTOTAL      1234    56    200    10   87.5%\n=== 502 passed, 1 skipped ==="
    assert parse_coverage(out) == 87.5
    assert parse_tests(out) == (502, 0)
    fail = "=== 3 failed, 499 passed, 1 error in 5s ==="
    assert parse_tests(fail) == (499, 4)


def test_gate_passed():
    assert gate_passed({"tests_green": True, "coverage_ok": True, "bench_ok": True})
    assert not gate_passed({"tests_green": True, "coverage_ok": False, "bench_ok": True})


def test_state_focus_roundtrip(tmp_path: Path):
    store = StateStore(tmp_path / "state")
    fs = FocusState(focus="demo", branch="agent/demo", cycle="0.6", priority=2)
    store.write_focus(fs)
    got = store.read_focus("demo")
    assert got is not None and got.focus == "demo" and got.priority == 2
    assert [f.focus for f in store.all_foci("0.6")] == ["demo"]
    assert store.all_foci("9.9") == []


def test_state_budget_and_floor(tmp_path: Path):
    store = StateStore(tmp_path / "state")
    b = store.add_spend(3.0, "0.6")
    assert b["spent_today"] == 3.0 and b["spent_cycle"] == 3.0 and b["day"] == today()
    b = store.add_spend(2.0, "0.6")
    assert b["spent_cycle"] == 5.0
    # New cycle resets the per-cycle counter but not the daily counter.
    b = store.add_spend(1.0, "0.7")
    assert b["spent_cycle"] == 1.0 and b["spent_today"] == 6.0

    assert store.read_cov_floor(85.0) == 85.0
    store.write_cov_floor(88.2)
    assert store.read_cov_floor(85.0) == 88.2


def test_claude_backend_retries_transient(monkeypatch):
    from orchestrator import agents
    from orchestrator.agents import AgentResult, ClaudeAgentBackend

    calls = {"n": 0}

    def fake_run_once(argv, cwd, env, timeout):
        calls["n"] += 1
        if calls["n"] < 3:  # two 529s, then success
            return AgentResult("", 0.0, False, "API Error: 529 Overloaded"), True
        return AgentResult("OK", 0.5, True), False

    monkeypatch.setattr(ClaudeAgentBackend, "_run_once", staticmethod(fake_run_once))
    monkeypatch.setattr(agents.time, "sleep", lambda s: None)
    cfg = Config(repo_root=Path("/tmp/x"))
    r = ClaudeAgentBackend()._invoke(cfg, model="sonnet", system_prompt="s",
                                     user_prompt="u", cwd=Path("/tmp/x"), timeout=10, budget=1.0)
    assert r.ok and r.text == "OK" and calls["n"] == 3


def test_claude_backend_no_retry_on_terminal(monkeypatch):
    from orchestrator import agents
    from orchestrator.agents import AgentResult, ClaudeAgentBackend

    calls = {"n": 0}

    def fake_run_once(argv, cwd, env, timeout):
        calls["n"] += 1
        return AgentResult("", 0.0, False, "invalid request"), False  # terminal

    monkeypatch.setattr(ClaudeAgentBackend, "_run_once", staticmethod(fake_run_once))
    monkeypatch.setattr(agents.time, "sleep", lambda s: None)
    cfg = Config(repo_root=Path("/tmp/x"))
    r = ClaudeAgentBackend()._invoke(cfg, model="sonnet", system_prompt="s",
                                     user_prompt="u", cwd=Path("/tmp/x"), timeout=10, budget=1.0)
    assert not r.ok and calls["n"] == 1


def test_state_events(tmp_path: Path):
    store = StateStore(tmp_path / "state")
    store.log_event("0.6", "built", focus="demo")
    store.log_event("0.6", "approved", focus="demo")
    events = store.read_events("0.6")
    assert [e["event"] for e in events] == ["built", "approved"]
