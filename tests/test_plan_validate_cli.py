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


# ── schema validation: unrecognized / misspelled keys ───────────────────────── #
#
# A misspelled key is reported (a `WARN` line, or `warnings` in --json) but is
# advisory by default — the job still loads and the exit code is unaffected, so no
# previously-valid file becomes invalid. `--strict` promotes warnings to failures.

def _job_with(extra: dict) -> dict:
    return {**_minimal_job(), **extra}


def test_misspelled_top_level_key_warns_nonstrict_exit_zero(tmp_path, capsys):
    # `defaluts:` is ignored by the loader; the one valid job still resolves.
    plan = tmp_path / "plan.yaml"
    _write(plan, {"defaluts": {"schedule": {"mode": "once"}},
                  "jobs": [{"id": "j", **_minimal_job()}]})
    rc = main(["plan", "validate", str(plan)])
    out = capsys.readouterr().out
    assert rc == 0                       # advisory by default — still valid
    assert "ok  j" in out
    assert "WARN" in out
    assert "defaluts" in out             # names the offending key


def test_misspelled_top_level_key_suggests_closest(tmp_path, capsys):
    plan = tmp_path / "plan.yaml"
    _write(plan, {"defaluts": {}, "jobs": [{"id": "j", **_minimal_job()}]})
    main(["plan", "validate", str(plan)])
    out = capsys.readouterr().out
    assert "did you mean 'defaults'?" in out


def test_misspelled_top_level_key_strict_exits_nonzero(tmp_path, capsys):
    plan = tmp_path / "plan.yaml"
    _write(plan, {"defaluts": {}, "jobs": [{"id": "j", **_minimal_job()}]})
    rc = main(["plan", "validate", str(plan), "--strict"])
    out = capsys.readouterr().out
    assert rc != 0                       # strict promotes the warning to a failure
    assert "FAIL" in out
    assert "defaluts" in out


def test_misspelled_per_job_key_warns_and_names_job(tmp_path, capsys):
    # Job is otherwise complete, so it still loads; the stray `shedule:` is flagged.
    plan = tmp_path / "plan.yaml"
    _write(plan, [{"id": "good", **_job_with({"shedule": {"mode": "cron"}})}])
    rc = main(["plan", "validate", str(plan)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "ok  good" in out
    assert "shedule" in out
    assert "good" in out                 # names the job id
    assert "did you mean 'schedule'?" in out


def test_misspelled_per_job_key_strict_fails(tmp_path, capsys):
    plan = tmp_path / "plan.yaml"
    _write(plan, [{"id": "good", **_job_with({"shedule": {"mode": "cron"}})}])
    assert main(["plan", "validate", str(plan), "--strict"]) != 0


def test_unknown_key_with_no_close_match_omits_hint(tmp_path, capsys):
    plan = tmp_path / "plan.yaml"
    _write(plan, [{"id": "j", **_job_with({"zzzzzzzz": 1})}])
    main(["plan", "validate", str(plan)])
    out = capsys.readouterr().out
    assert "zzzzzzzz" in out
    assert "did you mean" not in out     # nothing close enough to suggest


def test_warnings_in_json_nonstrict_stays_ok(tmp_path, capsys):
    plan = tmp_path / "plan.yaml"
    _write(plan, [{"id": "j", **_job_with({"shedule": {"mode": "cron"}})}])
    rc = main(["plan", "validate", str(plan), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["ok"] is True            # additive default: still valid
    assert data["resolved"] == ["j"]
    assert data["failed"] == []
    assert len(data["warnings"]) == 1
    assert "shedule" in data["warnings"][0]
    assert data["strict"] is False


def test_warnings_in_json_strict_marks_not_ok(tmp_path, capsys):
    plan = tmp_path / "plan.yaml"
    _write(plan, [{"id": "j", **_job_with({"shedule": {"mode": "cron"}})}])
    rc = main(["plan", "validate", str(plan), "--strict", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc != 0
    assert data["ok"] is False
    assert data["strict"] is True
    assert len(data["warnings"]) == 1


def test_workflow_dir_misspelled_top_level_key(tmp_path, capsys):
    # A single-job workflow file: top-level keys ARE the job keys, so a typo there
    # is caught. Job still loads (exit 0); --strict flips it to a failure.
    _write(tmp_path / "page.yaml", _job_with({"retreis": 3}))
    rc = main(["plan", "validate", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "ok  page" in out
    assert "retreis" in out
    assert main(["plan", "validate", str(tmp_path), "--strict"]) != 0


# ── examples/ all validate cleanly (no schema warnings, strict-clean) ────────── #

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.mark.parametrize("rel", [
    "ingest-plan.yaml",          # plan: defaults + include + matrix
    "jobs.crossref.yaml",        # plan: a {jobs: [...]} preload file
    "workflows",                 # the whole workflows directory
])
def test_examples_validate_clean_even_strict(rel, capsys, monkeypatch):
    # No env for ${INGEST_URL}; it expands to "" (a load-time, not schema, warning).
    monkeypatch.delenv("INGEST_URL", raising=False)
    target = _EXAMPLES / rel
    rc = main(["plan", "validate", str(target), "--strict", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0, data
    assert data["ok"] is True
    assert data["failed"] == []
    assert data["warnings"] == []        # shipped examples carry no unknown keys
