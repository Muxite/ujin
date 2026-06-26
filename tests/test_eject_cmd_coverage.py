"""Targeted coverage for the two uncovered branches in _cmd_plan_eject.

Covers:
  ujin/cli.py:822-824  — plan eject --env-file <bad> returns 1 with a clean message
  ujin/cli.py:822-823  — plan eject --env-file <good> succeeds and writes ejected files
  ujin/cli.py:840      — plan eject <directory> loads via _load_workflows_dir

All tests are offline and deterministic (tmp_path only, no network, no real clock).
"""
from __future__ import annotations

import yaml
from pathlib import Path


# ── helpers ────────────────────────────────────────────────────────────────── #

def _run_cli(argv):
    from ujin.cli import main as cli_main
    try:
        return cli_main(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1


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


# ── plan eject --env-file error path (lines 822-824) ─────────────────────── #

def test_eject_bad_env_file_exits_nonzero(tmp_path, capsys):
    """plan eject --env-file pointing at a missing file returns 1 with a clean message."""
    plan = tmp_path / "plan.yaml"
    _write(plan, [_minimal_job(id="job-a")])
    out = tmp_path / "out"

    rc = _run_cli([
        "plan", "eject", str(plan),
        "--out", str(out),
        "--env-file", str(tmp_path / "nonexistent.env"),
    ])

    assert rc == 1
    err = capsys.readouterr().err
    assert "ujin:" in err
    assert "Traceback" not in err
    assert not out.exists(), "out dir must not be created on early-exit"


def test_eject_malformed_env_file_exits_nonzero(tmp_path, capsys):
    """plan eject --env-file with a malformed KEY=VALUE line returns 1 cleanly."""
    plan = tmp_path / "plan.yaml"
    _write(plan, [_minimal_job(id="job-b")])
    env_file = tmp_path / "bad.env"
    env_file.write_text("NOT_VALID_LINE_NO_EQUALS\n", encoding="utf-8")
    out = tmp_path / "out"

    rc = _run_cli([
        "plan", "eject", str(plan),
        "--out", str(out),
        "--env-file", str(env_file),
    ])

    assert rc == 1
    err = capsys.readouterr().err
    assert "ujin:" in err
    assert "Traceback" not in err


def test_eject_valid_env_file_succeeds(tmp_path, capsys, monkeypatch):
    """plan eject --env-file with a good file loads env vars and ejects the job."""
    monkeypatch.delenv("MY_CMD", raising=False)

    plan = tmp_path / "plan.yaml"
    _write(plan, [{
        "id": "job-c",
        "schedule": {"mode": "once"},
        "source": {"kind": "command", "config": {"argv": ["${MY_CMD:-printf}", "x"]}},
    }])
    env_file = tmp_path / "vars.env"
    env_file.write_text("MY_CMD=echo\n", encoding="utf-8")
    out = tmp_path / "out"

    rc = _run_cli([
        "plan", "eject", str(plan),
        "--out", str(out),
        "--env-file", str(env_file),
    ])

    assert rc == 0
    assert (out / "job-c.yaml").exists()


# ── plan eject <directory> (line 840) ────────────────────────────────────── #

def test_eject_directory_loads_workflows_dir(tmp_path, capsys):
    """plan eject <dir> resolves workflows via _load_workflows_dir and writes one file per job."""
    wdir = tmp_path / "workflows"
    wdir.mkdir()
    _write(wdir / "alpha.yaml", _minimal_job(id="alpha"))
    _write(wdir / "beta.yaml", _minimal_job(id="beta"))
    out = tmp_path / "out"

    rc = _run_cli(["plan", "eject", str(wdir), "--out", str(out)])

    assert rc == 0
    written = {p.stem for p in out.glob("*.yaml")}
    assert written == {"alpha", "beta"}
    capsys.readouterr()  # consume output


def test_eject_directory_subdirs_excluded(tmp_path, capsys):
    """Workflow fragments in subdirs are not treated as workflows by plan eject."""
    wdir = tmp_path / "workflows"
    (wdir / "frags").mkdir(parents=True)
    _write(wdir / "main.yaml", _minimal_job(id="main"))
    # This fragment lives in a subdir and must NOT appear in the ejected output.
    _write(wdir / "frags" / "shared.yaml", _minimal_job(id="shared"))
    out = tmp_path / "out"

    rc = _run_cli(["plan", "eject", str(wdir), "--out", str(out)])

    assert rc == 0
    written = {p.stem for p in out.glob("*.yaml")}
    assert written == {"main"}
    capsys.readouterr()
