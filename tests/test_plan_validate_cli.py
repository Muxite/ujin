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


# ── required-variable: ${VAR:?message} in plan validate ──────────────────────── #

def test_required_var_unset_plan_exits_nonzero(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("REQUIRED_CLI_VAR", raising=False)
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        "- id: j\n"
        "  schedule: {mode: once}\n"
        '  source: {kind: command, config: {argv: [printf, "${REQUIRED_CLI_VAR:?set it}"]}}\n',
        encoding="utf-8",
    )
    rc = main(["plan", "validate", str(plan)])
    assert rc != 0


def test_required_var_unset_plan_message_actionable(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("REQUIRED_CLI_VAR", raising=False)
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        "- id: j\n"
        "  schedule: {mode: once}\n"
        '  source: {kind: command, config: {argv: [printf, "${REQUIRED_CLI_VAR:?set it}"]}}\n',
        encoding="utf-8",
    )
    main(["plan", "validate", str(plan)])
    out = capsys.readouterr().out
    assert "ujin:" in out
    assert "REQUIRED_CLI_VAR" in out
    assert "FAIL" in out


def test_required_var_set_plan_exits_zero(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("REQUIRED_CLI_VAR", "present")
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        "- id: j\n"
        "  schedule: {mode: once}\n"
        '  source: {kind: command, config: {argv: [printf, "${REQUIRED_CLI_VAR:?set it}"]}}\n',
        encoding="utf-8",
    )
    rc = main(["plan", "validate", str(plan)])
    assert rc == 0


def test_required_var_no_traceback(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("REQUIRED_CLI_VAR", raising=False)
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        "- id: j\n"
        "  schedule: {mode: once}\n"
        '  source: {kind: command, config: {argv: [printf, "${REQUIRED_CLI_VAR:?set it}"]}}\n',
        encoding="utf-8",
    )
    main(["plan", "validate", str(plan)])
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "EnvVarRequiredError" not in err


# ── improved diagnostics: error messages name the offending file/job ─────────── #
#
# Each test below exercises a distinct invalid-input case and asserts on
# stable outcome-level facts: exit code non-zero, and that the offending
# file name (and job id where applicable) appears somewhere in the output.
# Exact wording is intentionally not pinned — only structural presence.

def test_per_job_error_names_plan_file(tmp_path, capsys):
    # A bad matrix on a named job should produce a per-job error that
    # names both the plan file and the job id.
    plan = tmp_path / "myplan.yaml"
    _write(plan, [
        {"id": "good-job", **_minimal_job()},
        {"id": "bad-job", "matrix": "not-a-list", **_minimal_job()},
    ])
    rc = main(["plan", "validate", str(plan)])
    out = capsys.readouterr().out
    assert rc != 0
    assert "myplan.yaml" in out
    assert "bad-job" in out


def test_workflow_dir_failure_names_workflow_file(tmp_path, capsys):
    # A workflow file that fails to parse should produce a failure record
    # that names the workflow file.
    (tmp_path / "broken-workflow.yaml").write_text(
        "key: [unclosed bracket", encoding="utf-8"
    )
    rc = main(["plan", "validate", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc != 0
    assert "broken-workflow.yaml" in out
    assert "ujin:" in out


def test_duplicate_job_id_exits_nonzero_names_file(tmp_path, capsys):
    # Two entries sharing an explicit id in the same plan → per-job failure
    # naming the plan file.
    plan = tmp_path / "dup-plan.yaml"
    _write(plan, [
        {"id": "same-id", **_minimal_job("https://a/")},
        {"id": "same-id", **_minimal_job("https://b/")},
    ])
    rc = main(["plan", "validate", str(plan)])
    out = capsys.readouterr().out
    assert rc != 0
    assert "dup-plan.yaml" in out
    assert "same-id" in out


def test_defaults_type_mismatch_names_file(tmp_path, capsys):
    # `defaults:` must be a mapping; a list here is a file-level error
    # that should name the plan file.
    plan = tmp_path / "bad-defaults.yaml"
    _write(plan, {"defaults": [1, 2], "jobs": [{"id": "j", **_minimal_job()}]})
    rc = main(["plan", "validate", str(plan)])
    out = capsys.readouterr().out
    assert rc != 0
    assert "bad-defaults.yaml" in out
    assert "ujin:" in out


def test_matrix_non_dict_row_names_job_and_file(tmp_path, capsys):
    # A matrix row that is a string (not a variable map) → per-job error
    # naming the job id and the plan file.
    plan = tmp_path / "matrix-plan.yaml"
    _write(plan, [{"id": "matrix-job", "matrix": ["not-a-map"], **_minimal_job()}])
    rc = main(["plan", "validate", str(plan)])
    out = capsys.readouterr().out
    assert rc != 0
    assert "matrix-plan.yaml" in out
    assert "matrix-job" in out


def test_include_missing_names_plan_file(tmp_path, capsys):
    # A top-level include of a nonexistent fragment → file-level error
    # that names the plan file.
    plan = tmp_path / "include-plan.yaml"
    plan.write_text("include: no-such-fragment.yaml\njobs: []\n", encoding="utf-8")
    rc = main(["plan", "validate", str(plan)])
    out = capsys.readouterr().out
    assert rc != 0
    assert "include-plan.yaml" in out
    assert "ujin:" in out


def test_workflow_required_var_unset_names_file(tmp_path, capsys, monkeypatch):
    # A workflow file with ${VAR:?} and the var unset → failure record that
    # names the workflow file.
    monkeypatch.delenv("REQUIRED_WF_VAR", raising=False)
    wf = tmp_path / "secret-workflow.yaml"
    wf.write_text(
        "source:\n"
        "  kind: http\n"
        "  config:\n"
        '    url: "https://api.example.com/${REQUIRED_WF_VAR:?set REQUIRED_WF_VAR}"\n'
        "schedule: {mode: once}\n",
        encoding="utf-8",
    )
    rc = main(["plan", "validate", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc != 0
    assert "secret-workflow.yaml" in out
    assert "ujin:" in out


def test_per_job_error_json_names_plan_file(tmp_path, capsys):
    # --json output for a per-job failure must include the plan file name
    # in the error field.
    plan = tmp_path / "json-plan.yaml"
    _write(plan, [{"id": "failing", "matrix": 42, **_minimal_job()}])
    rc = main(["plan", "validate", str(plan), "--json"])
    assert rc != 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False
    assert len(data["failed"]) == 1
    assert "json-plan.yaml" in data["failed"][0]["error"]
    assert "failing" in data["failed"][0]["error"]


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


# ── missing source ────────────────────────────────────────────────────────────── #
#
# A job mapping with no `source:` key must fail with a clear message naming the
# offending job and file — not a bare Python TypeError from SourceSpec.__init__.

def test_plan_missing_source_exits_nonzero(tmp_path, capsys):
    plan = tmp_path / "plan.yaml"
    _write(plan, [{"id": "no-source", "schedule": {"mode": "once"}}])
    rc = main(["plan", "validate", str(plan)])
    assert rc != 0


def test_plan_missing_source_message_names_job_file_and_key(tmp_path, capsys):
    # Good job still loads; bad job names file, job id, and the missing key.
    plan = tmp_path / "src-plan.yaml"
    _write(plan, [
        {"id": "good", **_minimal_job()},
        {"id": "no-source", "schedule": {"mode": "once"}},
    ])
    main(["plan", "validate", str(plan)])
    out = capsys.readouterr().out
    assert "ok  good" in out
    assert "FAIL" in out
    assert "no-source" in out
    assert "src-plan.yaml" in out
    assert "source" in out
    # Must not be a raw Python exception type
    assert "TypeError" not in out
    assert "Traceback" not in out


def test_plan_missing_source_no_traceback(tmp_path, capsys):
    plan = tmp_path / "plan.yaml"
    _write(plan, [{"id": "j", "schedule": {"mode": "once"}}])
    main(["plan", "validate", str(plan)])
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "TypeError" not in err


def test_workflow_missing_source_exits_nonzero(tmp_path, capsys):
    _write(tmp_path / "no-src.yaml", {"schedule": {"mode": "once"}})
    rc = main(["plan", "validate", str(tmp_path)])
    assert rc != 0


def test_workflow_missing_source_message_names_file_and_key(tmp_path, capsys):
    _write(tmp_path / "needs-source.yaml", {"schedule": {"mode": "once"}})
    main(["plan", "validate", str(tmp_path)])
    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "needs-source" in out
    assert "source" in out
    assert "TypeError" not in out


# ── non-conforming top-level in workflow file ────────────────────────────────── #
#
# A workflow file that parses to a bare scalar (integer, plain string, etc.) must
# produce a clean ujin: … message naming the file — not a cryptic Python error
# like "'int' object is not iterable".

def test_workflow_bare_integer_exits_nonzero(tmp_path, capsys):
    (tmp_path / "bad.yaml").write_text("42\n", encoding="utf-8")
    rc = main(["plan", "validate", str(tmp_path)])
    assert rc != 0


def test_workflow_bare_integer_names_file(tmp_path, capsys):
    (tmp_path / "bad-shape.yaml").write_text("42\n", encoding="utf-8")
    main(["plan", "validate", str(tmp_path)])
    out = capsys.readouterr().out
    assert "bad-shape" in out
    assert "ujin:" in out
    # Message must be actionable — not a raw Python exception
    assert "object is not iterable" not in out
    assert "Traceback" not in out


def test_workflow_bare_string_names_file(tmp_path, capsys):
    (tmp_path / "string-doc.yaml").write_text("just a string\n", encoding="utf-8")
    main(["plan", "validate", str(tmp_path)])
    out = capsys.readouterr().out
    assert "string-doc" in out
    assert "ujin:" in out
    assert "dictionary update" not in out


def test_workflow_bare_scalar_good_sibling_still_loads(tmp_path, capsys):
    # The scalar-doc file fails; the valid sibling still loads.
    _write(tmp_path / "good.yaml", _minimal_job())
    (tmp_path / "bad.yaml").write_text("42\n", encoding="utf-8")
    main(["plan", "validate", str(tmp_path)])
    out = capsys.readouterr().out
    assert "ok  good" in out
    assert "FAIL" in out


# ── matrix scalar value: error describes both valid shapes ───────────────────── #
#
# `matrix: 42` (a scalar, neither list nor axis-mapping) used to say only "must be
# a non-empty list of variable maps", omitting the cross-product form. The message
# should now name both valid shapes so users know either form is accepted.

def test_matrix_scalar_value_message_mentions_both_shapes(tmp_path, capsys):
    plan = tmp_path / "plan.yaml"
    _write(plan, [{"id": "j", "matrix": 42, **_minimal_job()}])
    main(["plan", "validate", str(plan)])
    out = capsys.readouterr().out
    assert "FAIL" in out
    # Both valid forms must be described
    assert "list" in out.lower()
    assert "mapping" in out.lower()


def test_for_each_scalar_value_message_mentions_both_shapes(tmp_path, capsys):
    plan = tmp_path / "plan.yaml"
    _write(plan, [{"id": "j", "for_each": "bad", **_minimal_job()}])
    main(["plan", "validate", str(plan)])
    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "list" in out.lower()
    assert "mapping" in out.lower()
