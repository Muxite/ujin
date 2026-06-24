"""ujin plan validate — offline, deterministic tests.

Covers: all-valid plan file, all-valid workflows dir, has-failures (bad matrix +
missing include fragment), --json output, missing path, and exit codes.
All tests use tmp_path and the real loaders; no network.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from ujin.cli import main


# ── helpers ─────────────────────────────────────────────────────────────────── #

def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _minimal_job(url: str = "https://example.com/") -> dict:
    return {
        "source": {"kind": "http", "config": {"url": url}},
        "schedule": {"mode": "once"},
    }


# ── all-valid: ingest-plan file ─────────────────────────────────────────────── #

def test_all_valid_plan_exits_zero(tmp_path, capsys):
    plan = tmp_path / "jobs.yaml"
    _write(plan, [
        {"id": "alpha", **_minimal_job("https://a/")},
        {"id": "beta",  **_minimal_job("https://b/")},
    ])
    rc = main(["plan", "validate", str(plan)])
    assert rc == 0


def test_all_valid_plan_prints_ids(tmp_path, capsys):
    plan = tmp_path / "jobs.yaml"
    _write(plan, [
        {"id": "alpha", **_minimal_job("https://a/")},
        {"id": "beta",  **_minimal_job("https://b/")},
    ])
    main(["plan", "validate", str(plan)])
    out = capsys.readouterr().out
    assert "alpha" in out
    assert "beta" in out
    assert out.count("ok  ") == 2


# ── all-valid: workflows directory ──────────────────────────────────────────── #

def test_all_valid_workflows_dir_exits_zero(tmp_path, capsys):
    _write(tmp_path / "job-a.yaml", _minimal_job("https://a/"))
    _write(tmp_path / "job-b.yaml", _minimal_job("https://b/"))
    rc = main(["plan", "validate", str(tmp_path)])
    assert rc == 0


def test_all_valid_workflows_dir_prints_ids(tmp_path, capsys):
    _write(tmp_path / "job-a.yaml", _minimal_job("https://a/"))
    _write(tmp_path / "job-b.yaml", _minimal_job("https://b/"))
    main(["plan", "validate", str(tmp_path)])
    out = capsys.readouterr().out
    assert "job-a" in out
    assert "job-b" in out


# ── has-failures: bad matrix in plan ────────────────────────────────────────── #

def test_bad_matrix_plan_exits_nonzero(tmp_path, capsys):
    plan = tmp_path / "plan.yaml"
    _write(plan, [
        {"id": "good", **_minimal_job("https://a/")},
        # matrix must be a list of dicts; a bare string is malformed
        {"id": "bad", "matrix": "nope", **_minimal_job("https://b/")},
    ])
    rc = main(["plan", "validate", str(plan)])
    assert rc != 0


def test_bad_matrix_plan_reports_good_and_bad(tmp_path, capsys):
    plan = tmp_path / "plan.yaml"
    _write(plan, [
        {"id": "good", **_minimal_job("https://a/")},
        {"id": "bad", "matrix": "nope", **_minimal_job("https://b/")},
    ])
    main(["plan", "validate", str(plan)])
    out = capsys.readouterr().out
    assert "ok  good" in out
    assert "FAIL" in out
    assert "bad" in out


# ── has-failures: missing include fragment (file-level error) ────────────────── #

def test_missing_include_plan_exits_nonzero(tmp_path, capsys):
    plan = tmp_path / "plan.yaml"
    _write(plan, {"jobs": [{"id": "j", "include": "nonexistent-fragment.yaml"}]})
    rc = main(["plan", "validate", str(plan)])
    assert rc != 0


def test_missing_include_plan_reports_failure(tmp_path, capsys):
    plan = tmp_path / "plan.yaml"
    _write(plan, {"jobs": [{"id": "j", "include": "nonexistent-fragment.yaml"}]})
    main(["plan", "validate", str(plan)])
    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "ujin:" in out


def test_bad_workflow_file_exits_nonzero(tmp_path, capsys):
    # One valid workflow, one with invalid YAML — the bad one lands in failed.
    _write(tmp_path / "good.yaml", _minimal_job("https://a/"))
    (tmp_path / "broken.yaml").write_text("key: [unclosed bracket", encoding="utf-8")
    rc = main(["plan", "validate", str(tmp_path)])
    assert rc != 0


def test_bad_workflow_file_good_job_still_listed(tmp_path, capsys):
    _write(tmp_path / "good.yaml", _minimal_job("https://a/"))
    (tmp_path / "broken.yaml").write_text("key: [unclosed bracket", encoding="utf-8")
    main(["plan", "validate", str(tmp_path)])
    out = capsys.readouterr().out
    assert "ok  good" in out
    assert "FAIL" in out


# ── --json output ─────────────────────────────────────────────────────────────── #

def test_json_all_valid(tmp_path, capsys):
    plan = tmp_path / "jobs.yaml"
    _write(plan, [
        {"id": "alpha", **_minimal_job("https://a/")},
        {"id": "beta",  **_minimal_job("https://b/")},
    ])
    rc = main(["plan", "validate", str(plan), "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert data["resolved"] == ["alpha", "beta"]
    assert data["failed"] == []


def test_json_has_failures(tmp_path, capsys):
    plan = tmp_path / "plan.yaml"
    _write(plan, [
        {"id": "good", **_minimal_job("https://a/")},
        {"id": "bad", "matrix": "nope", **_minimal_job("https://b/")},
    ])
    rc = main(["plan", "validate", str(plan), "--json"])
    assert rc != 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False
    assert data["resolved"] == ["good"]
    assert len(data["failed"]) == 1
    assert data["failed"][0]["id"] == "bad"
    assert "ujin:" in data["failed"][0]["error"]


def test_json_workflows_dir(tmp_path, capsys):
    _write(tmp_path / "job-a.yaml", _minimal_job("https://a/"))
    _write(tmp_path / "job-b.yaml", _minimal_job("https://b/"))
    main(["plan", "validate", str(tmp_path), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert set(data["resolved"]) == {"job-a", "job-b"}
    assert data["failed"] == []


# ── missing path ─────────────────────────────────────────────────────────────── #

def test_missing_path_exits_nonzero(tmp_path, capsys):
    rc = main(["plan", "validate", str(tmp_path / "does-not-exist.yaml")])
    assert rc != 0


def test_missing_path_clean_error(tmp_path, capsys):
    path = str(tmp_path / "does-not-exist.yaml")
    main(["plan", "validate", path])
    err = capsys.readouterr().err
    assert "ujin:" in err
    assert "does-not-exist.yaml" in err


def test_missing_path_no_traceback(tmp_path, capsys):
    path = str(tmp_path / "no-such.yaml")
    main(["plan", "validate", path])
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "Exception" not in err


def test_missing_path_json(tmp_path, capsys):
    path = str(tmp_path / "does-not-exist.yaml")
    rc = main(["plan", "validate", path, "--json"])
    assert rc != 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False
    assert "ujin:" in data["error"]
    assert data["resolved"] == []
    assert data["failed"] == []


# ── exit-code contract ────────────────────────────────────────────────────────── #

def test_exit_zero_on_all_valid(tmp_path, capsys):
    plan = tmp_path / "ok.yaml"
    _write(plan, [{"id": "j", **_minimal_job()}])
    assert main(["plan", "validate", str(plan)]) == 0


def test_exit_nonzero_on_any_failure(tmp_path, capsys):
    plan = tmp_path / "mixed.yaml"
    _write(plan, [
        {"id": "good", **_minimal_job()},
        {"id": "bad", "matrix": "not-a-list", **_minimal_job()},
    ])
    assert main(["plan", "validate", str(plan)]) != 0


def test_exit_nonzero_on_missing_path(tmp_path, capsys):
    assert main(["plan", "validate", str(tmp_path / "ghost.yaml")]) != 0
