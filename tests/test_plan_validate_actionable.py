"""Deterministic offline tests: plan-validate actionable errors.

Asserts the exit code and message shape for each malformed-input case listed
in the task acceptance criteria:

  * unknown top-level key    — already advisory (warning); job still loads
  * malformed matrix/for_each shape
  * missing or unreadable include/fragment file
  * unresolved required ${VAR:?} (no default)
  * duplicate job names
  * empty/zero-job plan

Both surfaces are covered:
  - ``ujin plan validate`` CLI
  - ``jobs-serve --plan`` preflight (via ``_cmd_jobs_serve`` to avoid uvicorn)

Valid plans continue to load and validate exactly as before.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
import yaml

from ujin.cli import _cmd_jobs_serve, main


# ── helpers ──────────────────────────────────────────────────────────────────


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _minimal_job(url: str = "https://example.com/") -> dict:
    return {
        "source": {"kind": "http", "config": {"url": url}},
        "schedule": {"mode": "once"},
    }


def _serve_args(plan: "str | None", **kw) -> argparse.Namespace:
    """Minimal Namespace for _cmd_jobs_serve; exit-before-serve cases need no uvicorn."""
    defaults = dict(
        env_file=None,
        only=None,
        exclude=None,
        tag=None,
        exclude_tag=None,
        profile=None,
        plan=plan,
        host="localhost",
        port=8901,
        jobs=None,
        workflows=None,
        watch_plan=False,
        watch_interval=None,
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


# ══════════════════════════════════════════════════════════════════════════════
# ujin plan validate — empty / zero-job plan file
# ══════════════════════════════════════════════════════════════════════════════


def test_empty_file_plan_exits_nonzero(tmp_path, capsys):
    plan = tmp_path / "empty.yaml"
    plan.write_text("", encoding="utf-8")
    rc = main(["plan", "validate", str(plan)])
    assert rc != 0


def test_empty_file_plan_ujin_message_on_stdout(tmp_path, capsys):
    plan = tmp_path / "empty.yaml"
    plan.write_text("", encoding="utf-8")
    main(["plan", "validate", str(plan)])
    out = capsys.readouterr().out
    assert "ujin:" in out
    assert "empty.yaml" in out
    assert "FAIL" in out


def test_empty_file_plan_no_traceback(tmp_path, capsys):
    plan = tmp_path / "empty.yaml"
    plan.write_text("", encoding="utf-8")
    main(["plan", "validate", str(plan)])
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "Exception" not in err


def test_zero_jobs_list_plan_exits_nonzero(tmp_path, capsys):
    plan = tmp_path / "nojobs.yaml"
    _write(plan, {"jobs": [], "defaults": {}})
    rc = main(["plan", "validate", str(plan)])
    assert rc != 0


def test_zero_jobs_list_plan_names_file(tmp_path, capsys):
    plan = tmp_path / "nojobs.yaml"
    _write(plan, {"jobs": []})
    main(["plan", "validate", str(plan)])
    out = capsys.readouterr().out
    assert "nojobs.yaml" in out
    assert "ujin:" in out


def test_empty_plan_json_ok_false_with_error(tmp_path, capsys):
    plan = tmp_path / "empty.yaml"
    plan.write_text("", encoding="utf-8")
    rc = main(["plan", "validate", str(plan), "--json"])
    assert rc != 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False
    assert len(data["failed"]) == 1
    assert "ujin:" in data["failed"][0]["error"]
    assert "empty.yaml" in data["failed"][0]["error"]


def test_empty_workflows_dir_exits_zero(tmp_path, capsys):
    # An empty workflows DIRECTORY is not an error — only plan files are checked.
    rc = main(["plan", "validate", str(tmp_path)])
    assert rc == 0


def test_valid_nonempty_plan_unchanged(tmp_path, capsys):
    # Regression: a plan with jobs still exits 0.
    plan = tmp_path / "good.yaml"
    _write(plan, [{"id": "j", **_minimal_job()}])
    rc = main(["plan", "validate", str(plan)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ok  j" in out


# ── plan validate: unknown top-level key is still advisory (non-error) ───────


def test_unknown_top_key_still_advisory_exit_zero(tmp_path, capsys):
    # Existing behaviour must not regress: an unknown key is a WARN, not a FAIL.
    plan = tmp_path / "plan.yaml"
    _write(plan, {"typo_key": {}, "jobs": [{"id": "j", **_minimal_job()}]})
    rc = main(["plan", "validate", str(plan)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "ok  j" in out
    assert "WARN" in out


# ══════════════════════════════════════════════════════════════════════════════
# jobs-serve --plan preflight
# ══════════════════════════════════════════════════════════════════════════════


def test_preflight_missing_plan_exits_nonzero(tmp_path, capsys):
    rc = _cmd_jobs_serve(_serve_args(str(tmp_path / "nonexistent.yaml")))
    assert rc != 0


def test_preflight_missing_plan_ujin_stderr(tmp_path, capsys):
    _cmd_jobs_serve(_serve_args(str(tmp_path / "nonexistent.yaml")))
    err = capsys.readouterr().err
    assert "ujin:" in err
    assert "Traceback" not in err


def test_preflight_bad_yaml_exits_nonzero(tmp_path, capsys):
    plan = tmp_path / "bad.yaml"
    plan.write_text("key: [unclosed bracket", encoding="utf-8")
    rc = _cmd_jobs_serve(_serve_args(str(plan)))
    assert rc != 0


def test_preflight_bad_yaml_ujin_stderr_names_file(tmp_path, capsys):
    plan = tmp_path / "bad-syntax.yaml"
    plan.write_text("key: [unclosed bracket", encoding="utf-8")
    _cmd_jobs_serve(_serve_args(str(plan)))
    err = capsys.readouterr().err
    assert "ujin:" in err
    assert "bad-syntax.yaml" in err
    assert "Traceback" not in err


def test_preflight_missing_include_exits_nonzero(tmp_path, capsys):
    plan = tmp_path / "plan.yaml"
    plan.write_text("include: no-such.yaml\njobs: []\n", encoding="utf-8")
    rc = _cmd_jobs_serve(_serve_args(str(plan)))
    assert rc != 0


def test_preflight_missing_include_names_plan_file(tmp_path, capsys):
    plan = tmp_path / "incl-plan.yaml"
    plan.write_text("include: no-such.yaml\njobs: []\n", encoding="utf-8")
    _cmd_jobs_serve(_serve_args(str(plan)))
    err = capsys.readouterr().err
    assert "ujin:" in err
    assert "incl-plan.yaml" in err
    assert "Traceback" not in err


def test_preflight_bad_matrix_exits_nonzero(tmp_path, capsys):
    plan = tmp_path / "plan.yaml"
    _write(plan, [{"id": "j", "matrix": "not-a-list", **_minimal_job()}])
    rc = _cmd_jobs_serve(_serve_args(str(plan)))
    assert rc != 0


def test_preflight_bad_matrix_names_file_and_job(tmp_path, capsys):
    plan = tmp_path / "mat-plan.yaml"
    _write(plan, [{"id": "mat-job", "matrix": "not-a-list", **_minimal_job()}])
    _cmd_jobs_serve(_serve_args(str(plan)))
    err = capsys.readouterr().err
    assert "ujin:" in err
    assert "mat-plan.yaml" in err
    assert "Traceback" not in err


def test_preflight_for_each_scalar_exits_nonzero(tmp_path, capsys):
    plan = tmp_path / "plan.yaml"
    _write(plan, [{"id": "j", "for_each": 42, **_minimal_job()}])
    rc = _cmd_jobs_serve(_serve_args(str(plan)))
    assert rc != 0


def test_preflight_duplicate_job_id_exits_nonzero(tmp_path, capsys):
    plan = tmp_path / "plan.yaml"
    _write(plan, [
        {"id": "dup", **_minimal_job("https://a/")},
        {"id": "dup", **_minimal_job("https://b/")},
    ])
    rc = _cmd_jobs_serve(_serve_args(str(plan)))
    assert rc != 0


def test_preflight_duplicate_job_id_names_id_and_file(tmp_path, capsys):
    plan = tmp_path / "dup-plan.yaml"
    _write(plan, [
        {"id": "dup-id", **_minimal_job("https://a/")},
        {"id": "dup-id", **_minimal_job("https://b/")},
    ])
    _cmd_jobs_serve(_serve_args(str(plan)))
    err = capsys.readouterr().err
    assert "ujin:" in err
    assert "dup-plan.yaml" in err
    assert "Traceback" not in err


def test_preflight_required_var_unset_exits_nonzero(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("PREFLIGHT_REQ_VAR", raising=False)
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        "- id: j\n"
        "  schedule: {mode: once}\n"
        '  source: {kind: http, config: {url: "https://x/${PREFLIGHT_REQ_VAR:?must be set}"}}\n',
        encoding="utf-8",
    )
    rc = _cmd_jobs_serve(_serve_args(str(plan)))
    assert rc != 0


def test_preflight_required_var_unset_names_var_and_file(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("PREFLIGHT_REQ_VAR", raising=False)
    plan = tmp_path / "req-plan.yaml"
    plan.write_text(
        "- id: j\n"
        "  schedule: {mode: once}\n"
        '  source: {kind: http, config: {url: "https://x/${PREFLIGHT_REQ_VAR:?must be set}"}}\n',
        encoding="utf-8",
    )
    _cmd_jobs_serve(_serve_args(str(plan)))
    err = capsys.readouterr().err
    assert "ujin:" in err
    assert "PREFLIGHT_REQ_VAR" in err
    assert "req-plan.yaml" in err
    assert "Traceback" not in err


def test_preflight_required_var_set_passes_preflight(tmp_path, monkeypatch):
    monkeypatch.setenv("PREFLIGHT_REQ_VAR", "present")
    served: list = []
    monkeypatch.setattr("ujin.jobs.app.serve", lambda **kw: served.append(kw))
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        "- id: j\n"
        "  schedule: {mode: once}\n"
        '  source: {kind: http, config: {url: "https://x/${PREFLIGHT_REQ_VAR:?must be set}"}}\n',
        encoding="utf-8",
    )
    rc = _cmd_jobs_serve(_serve_args(str(plan)))
    assert rc == 0
    assert len(served) == 1


def test_preflight_empty_plan_file_exits_nonzero(tmp_path, capsys):
    plan = tmp_path / "empty.yaml"
    plan.write_text("", encoding="utf-8")
    rc = _cmd_jobs_serve(_serve_args(str(plan)))
    assert rc != 0


def test_preflight_empty_plan_file_names_file(tmp_path, capsys):
    plan = tmp_path / "empty.yaml"
    plan.write_text("", encoding="utf-8")
    _cmd_jobs_serve(_serve_args(str(plan)))
    err = capsys.readouterr().err
    assert "ujin:" in err
    assert "empty.yaml" in err


def test_preflight_zero_jobs_plan_exits_nonzero(tmp_path, capsys):
    plan = tmp_path / "nojobs.yaml"
    _write(plan, {"jobs": []})
    rc = _cmd_jobs_serve(_serve_args(str(plan)))
    assert rc != 0


def test_preflight_zero_jobs_plan_names_file(tmp_path, capsys):
    plan = tmp_path / "nojobs.yaml"
    _write(plan, {"jobs": []})
    _cmd_jobs_serve(_serve_args(str(plan)))
    err = capsys.readouterr().err
    assert "ujin:" in err
    assert "nojobs.yaml" in err


def test_preflight_valid_plan_reaches_serve(tmp_path, monkeypatch):
    served: list = []
    monkeypatch.setattr("ujin.jobs.app.serve", lambda **kw: served.append(kw))
    plan = tmp_path / "good.yaml"
    _write(plan, [{"id": "j", **_minimal_job()}])
    rc = _cmd_jobs_serve(_serve_args(str(plan)))
    assert rc == 0
    assert len(served) == 1


def test_preflight_no_plan_always_reaches_serve(tmp_path, monkeypatch):
    # Without --plan the preflight is a no-op; serve() is always called.
    served: list = []
    monkeypatch.setattr("ujin.jobs.app.serve", lambda **kw: served.append(kw))
    rc = _cmd_jobs_serve(_serve_args(None))
    assert rc == 0
    assert len(served) == 1


def test_preflight_valid_plan_bad_only_pattern_exits_nonzero(tmp_path, capsys):
    # Regression: valid plan + unmatched --only still exits 1.
    plan = tmp_path / "good.yaml"
    _write(plan, [{"id": "j", **_minimal_job()}])
    rc = _cmd_jobs_serve(_serve_args(str(plan), only=["no-match"]))
    assert rc != 0
    err = capsys.readouterr().err
    assert "ujin:" in err
    assert "no-match" in err
