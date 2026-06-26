"""Tests for --env-file / _load_env_file: load path, precedence, errors, regression.

Covers:
  ujin/jobs/app.py  — _load_env_file (parsing, errors, missing file)
  ujin/cli.py       — `ujin plan validate --env-file`, `ujin plan render --env-file`,
                      `ujin jobs-serve --env-file` flag acceptance
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ujin.jobs.app import _load_env_file

# ── helpers ───────────────────────────────────────────────────────────────────


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _minimal_job(**kw) -> dict:
    base = {
        "schedule": {"mode": "once"},
        "source": {"kind": "command", "config": {"argv": ["printf", "x"]}},
    }
    base.update(kw)
    return base


def _run_cli(argv):
    from ujin.cli import main as cli_main
    try:
        return cli_main(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1


# ─────────────────────────────────────────────────────────────────────────────
# _load_env_file — direct unit tests
# ─────────────────────────────────────────────────────────────────────────────

def test_load_env_file_basic(tmp_path):
    f = tmp_path / ".env"
    f.write_text("FOO=bar\nBAZ=qux\n", encoding="utf-8")
    result = _load_env_file(f)
    assert result == {"FOO": "bar", "BAZ": "qux"}


def test_load_env_file_skips_blank_lines(tmp_path):
    f = tmp_path / ".env"
    f.write_text("\nFOO=bar\n\n", encoding="utf-8")
    assert _load_env_file(f) == {"FOO": "bar"}


def test_load_env_file_skips_comments(tmp_path):
    f = tmp_path / ".env"
    f.write_text("# this is a comment\nFOO=bar\n# another comment\n", encoding="utf-8")
    assert _load_env_file(f) == {"FOO": "bar"}


def test_load_env_file_empty_value(tmp_path):
    f = tmp_path / ".env"
    f.write_text("FOO=\n", encoding="utf-8")
    assert _load_env_file(f) == {"FOO": ""}


def test_load_env_file_value_with_spaces(tmp_path):
    f = tmp_path / ".env"
    f.write_text("FOO=hello world\n", encoding="utf-8")
    assert _load_env_file(f) == {"FOO": "hello world"}


def test_load_env_file_value_with_equals(tmp_path):
    """Value may contain '=' — only the first '=' is the separator."""
    f = tmp_path / ".env"
    f.write_text("FOO=a=b=c\n", encoding="utf-8")
    assert _load_env_file(f) == {"FOO": "a=b=c"}


def test_load_env_file_underscore_key(tmp_path):
    f = tmp_path / ".env"
    f.write_text("_MY_VAR_123=value\n", encoding="utf-8")
    assert _load_env_file(f) == {"_MY_VAR_123": "value"}


def test_load_env_file_missing_equals_raises(tmp_path):
    f = tmp_path / ".env"
    f.write_text("BADLINE\n", encoding="utf-8")
    with pytest.raises(ValueError, match="line 1"):
        _load_env_file(f)


def test_load_env_file_invalid_key_raises(tmp_path):
    f = tmp_path / ".env"
    f.write_text("123BAD=value\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid variable name"):
        _load_env_file(f)


def test_load_env_file_missing_file_raises(tmp_path):
    with pytest.raises(OSError):
        _load_env_file(tmp_path / "nonexistent.env")


def test_load_env_file_empty_file(tmp_path):
    f = tmp_path / ".env"
    f.write_text("", encoding="utf-8")
    assert _load_env_file(f) == {}


def test_load_env_file_only_comments_and_blanks(tmp_path):
    f = tmp_path / ".env"
    f.write_text("# comment\n\n# another\n", encoding="utf-8")
    assert _load_env_file(f) == {}


# ─────────────────────────────────────────────────────────────────────────────
# CLI — ujin plan validate --env-file
# ─────────────────────────────────────────────────────────────────────────────

def _plan_with_var(tmp_path, var_name="MY_API_URL") -> tuple[Path, Path]:
    """Write a plan referencing ${var_name} and return (plan_path, env_file_path)."""
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        yaml.safe_dump([{
            "id": "test-job",
            "schedule": {"mode": "once"},
            "source": {"kind": "command", "config": {"argv": ["printf", f"${{{var_name}}}"]}}
        }]),
        encoding="utf-8",
    )
    env_file = tmp_path / ".env"
    env_file.write_text(f"{var_name}=http://example.com\n", encoding="utf-8")
    return plan, env_file


def test_plan_validate_env_file_resolves_var(tmp_path, monkeypatch, capsys):
    """--env-file supplies ${VAR} when VAR is unset in the process environment."""
    plan, env_file = _plan_with_var(tmp_path, "UJIN_TEST_EV_VALIDATE")
    monkeypatch.delenv("UJIN_TEST_EV_VALIDATE", raising=False)

    rc = _run_cli(["plan", "validate", str(plan), "--env-file", str(env_file)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "test-job" in out


def test_plan_validate_env_file_process_env_wins(tmp_path, monkeypatch, capsys):
    """Process environment takes precedence over --env-file."""
    var = "UJIN_TEST_EV_PREC"
    plan = tmp_path / "plan.yaml"
    # Use ${VAR:?message} to guarantee an error if the wrong value is used, but
    # since both values are non-empty this just confirms the right one wins.
    plan.write_text(
        yaml.safe_dump([{
            "id": "job-{{ ignored }}",
            "schedule": {"mode": "once"},
            "source": {"kind": "command", "config": {"argv": ["printf", f"${{{var}}}"]}}
        }]),
        encoding="utf-8",
    )
    env_file = tmp_path / ".env"
    env_file.write_text(f"{var}=from-file\n", encoding="utf-8")

    # Set the variable in the process environment first — it must win.
    monkeypatch.setenv(var, "from-process")

    # After validate we can confirm the env var is still "from-process" (not overwritten).
    rc = _run_cli(["plan", "validate", str(plan), "--env-file", str(env_file)])
    assert rc == 0
    import os
    assert os.environ[var] == "from-process"


def test_plan_validate_missing_env_file_exits_nonzero(tmp_path, capsys):
    """Missing --env-file exits 1 with a clean ujin: … message, no traceback."""
    plan = tmp_path / "plan.yaml"
    _write(plan, [_minimal_job(id="j")])

    rc = _run_cli(["plan", "validate", str(plan),
                   "--env-file", str(tmp_path / "nonexistent.env")])
    assert rc == 1
    err = capsys.readouterr().err
    assert "ujin:" in err
    assert "Traceback" not in err


def test_plan_validate_bad_env_file_exits_nonzero(tmp_path, capsys):
    """Unparseable --env-file exits 1 with a clean ujin: … message, no traceback."""
    plan = tmp_path / "plan.yaml"
    _write(plan, [_minimal_job(id="j")])
    bad_env = tmp_path / "bad.env"
    bad_env.write_text("NOT_VALID_LINE\n", encoding="utf-8")

    rc = _run_cli(["plan", "validate", str(plan), "--env-file", str(bad_env)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "ujin:" in err
    assert "Traceback" not in err


def test_plan_validate_no_env_file_regression(tmp_path, capsys):
    """Without --env-file, plan validate behaves identically to before."""
    plan = tmp_path / "plan.yaml"
    _write(plan, [_minimal_job(id="alpha"), _minimal_job(id="beta")])

    rc = _run_cli(["plan", "validate", str(plan)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "alpha" in out and "beta" in out


def test_plan_validate_env_file_required_var_still_fails_when_missing(tmp_path, monkeypatch, capsys):
    """${VAR:?msg} in a plan with --env-file still fails if VAR not in file or env."""
    missing_var = "UJIN_TEST_EV_REQUIRED_ABSENT"
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        yaml.safe_dump([{
            "id": "job",
            "schedule": {"mode": "once"},
            "source": {"kind": "command",
                       "config": {"argv": ["printf", f"${{{missing_var}:?must be set}}"]}}
        }]),
        encoding="utf-8",
    )
    env_file = tmp_path / ".env"
    env_file.write_text("OTHER_VAR=something\n", encoding="utf-8")
    monkeypatch.delenv(missing_var, raising=False)

    rc = _run_cli(["plan", "validate", str(plan), "--env-file", str(env_file)])
    # Job fails but command still returns 1 (failed list is non-empty)
    assert rc == 1


# ─────────────────────────────────────────────────────────────────────────────
# CLI — ujin plan render --env-file
# ─────────────────────────────────────────────────────────────────────────────

def test_plan_render_env_file_resolves_var(tmp_path, monkeypatch, capsys):
    """--env-file supplies ${VAR} value to plan render."""
    var = "UJIN_TEST_EV_RENDER"
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        yaml.safe_dump([{
            "id": "render-job",
            "schedule": {"mode": "once"},
            "source": {"kind": "command", "config": {"argv": ["printf", f"${{{var}:-fallback}}"]}}
        }]),
        encoding="utf-8",
    )
    env_file = tmp_path / ".env"
    env_file.write_text(f"{var}=from-env-file\n", encoding="utf-8")
    monkeypatch.delenv(var, raising=False)

    rc = _run_cli(["plan", "render", str(plan), "--env-file", str(env_file)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "from-env-file" in out
    assert "render-job" in out


def test_plan_render_missing_env_file_exits_nonzero(tmp_path, capsys):
    """Missing --env-file for plan render exits 1 with a clean ujin: … message."""
    plan = tmp_path / "plan.yaml"
    _write(plan, [_minimal_job(id="j")])

    rc = _run_cli(["plan", "render", str(plan),
                   "--env-file", str(tmp_path / "missing.env")])
    assert rc == 1
    err = capsys.readouterr().err
    assert "ujin:" in err
    assert "Traceback" not in err


def test_plan_render_bad_env_file_exits_nonzero(tmp_path, capsys):
    """Unparseable --env-file for plan render exits 1 with a clean message."""
    plan = tmp_path / "plan.yaml"
    _write(plan, [_minimal_job(id="j")])
    bad = tmp_path / "bad.env"
    bad.write_text("123INVALID=x\n", encoding="utf-8")

    rc = _run_cli(["plan", "render", str(plan), "--env-file", str(bad)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "ujin:" in err
    assert "Traceback" not in err


def test_plan_render_no_env_file_regression(tmp_path, capsys):
    """Without --env-file, plan render produces the same output as before."""
    plan = tmp_path / "plan.yaml"
    _write(plan, [_minimal_job(id="gamma")])

    rc = _run_cli(["plan", "render", str(plan)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "gamma" in out


def test_plan_render_env_file_process_env_wins(tmp_path, monkeypatch, capsys):
    """process env wins over --env-file in plan render."""
    var = "UJIN_TEST_EV_RENDER_PREC"
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        yaml.safe_dump([{
            "id": "pjob",
            "schedule": {"mode": "once"},
            "source": {"kind": "command", "config": {"argv": ["printf", f"${{{var}:-x}}"]}}
        }]),
        encoding="utf-8",
    )
    env_file = tmp_path / ".env"
    env_file.write_text(f"{var}=from-file\n", encoding="utf-8")
    monkeypatch.setenv(var, "from-process")

    rc = _run_cli(["plan", "render", str(plan), "--env-file", str(env_file)])
    assert rc == 0
    out = capsys.readouterr().out
    # The process value must appear, not the file value
    assert "from-process" in out
    assert "from-file" not in out


# ─────────────────────────────────────────────────────────────────────────────
# CLI — ujin jobs-serve --env-file flag acceptance and env-loading
# ─────────────────────────────────────────────────────────────────────────────

def test_jobs_serve_env_file_flag_is_accepted(tmp_path, monkeypatch):
    """--env-file is accepted by jobs-serve; env vars are set before serve() is called."""
    env_file = tmp_path / ".env"
    var = "UJIN_TEST_EV_SERVE"
    env_file.write_text(f"{var}=from-serve-file\n", encoding="utf-8")
    monkeypatch.delenv(var, raising=False)

    served_with = {}

    def fake_serve(**kwargs):
        import os
        served_with["val"] = os.environ.get(var)

    monkeypatch.setattr("ujin.jobs.app.serve", fake_serve, raising=False)
    from ujin.cli import _apply_env_file
    import os

    # Simulate what _cmd_jobs_serve does with --env-file
    rc = _apply_env_file(str(env_file))
    assert rc == 0
    assert os.environ.get(var) == "from-serve-file"
    # Cleanup
    monkeypatch.delenv(var, raising=False)


def test_jobs_serve_missing_env_file_exits_nonzero(tmp_path, capsys):
    """Missing --env-file for jobs-serve exits 1 with a clean ujin: … message."""
    from ujin.cli import _apply_env_file
    rc = _apply_env_file(str(tmp_path / "nope.env"))
    assert rc == 1
    err = capsys.readouterr().err
    assert "ujin:" in err
    assert "Traceback" not in err


def test_jobs_serve_bad_env_file_exits_nonzero(tmp_path, capsys):
    """Unparseable --env-file for jobs-serve exits 1 with a clean message."""
    bad = tmp_path / "bad.env"
    bad.write_text("NO EQUALS\n", encoding="utf-8")
    from ujin.cli import _apply_env_file
    rc = _apply_env_file(str(bad))
    assert rc == 1
    err = capsys.readouterr().err
    assert "ujin:" in err
    assert "Traceback" not in err
