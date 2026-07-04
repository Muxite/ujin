"""Offline validation of jobs/workflow config (ujin.jobs.validate)."""
from __future__ import annotations

from ujin.jobs.validate import validate

VALID_JOB = """\
jobs:
  - id: watch-example
    name: watch-example
    source:
      kind: html
      config: { url: "https://example.com" }
    sinks:
      - kind: jsonl
        config: { path: "/tmp/out.jsonl" }
"""


def test_validate_valid_jobs_file_ok(tmp_path):
    p = tmp_path / "jobs.yaml"
    p.write_text(VALID_JOB)
    result = validate(str(p))
    assert result.ok
    assert result.loaded == ["watch-example"]
    assert result.issues == []


def test_validate_empty_jobs_file_ok(tmp_path):
    p = tmp_path / "jobs.yaml"
    p.write_text("")
    result = validate(str(p))
    assert result.ok
    assert result.loaded == []


def test_validate_missing_file_reports_issue(tmp_path):
    p = tmp_path / "nope.yaml"
    result = validate(str(p))
    assert not result.ok
    assert "could not read file" in str(result.issues[0])


def test_validate_invalid_yaml_reports_issue(tmp_path):
    p = tmp_path / "jobs.yaml"
    p.write_text("jobs: [\n  - broken: [unterminated\n")
    result = validate(str(p))
    assert not result.ok
    assert "invalid YAML" in str(result.issues[0])


def test_validate_unknown_top_level_key(tmp_path):
    p = tmp_path / "jobs.yaml"
    p.write_text("jobs: []\nbogus: true\n")
    result = validate(str(p))
    assert not result.ok
    assert "unknown top-level key" in str(result.issues[0])
    assert "bogus" in str(result.issues[0])


def test_validate_non_mapping_job_entry(tmp_path):
    p = tmp_path / "jobs.yaml"
    p.write_text("jobs:\n  - not-a-mapping\n")
    result = validate(str(p))
    assert not result.ok
    assert "job entry must be a mapping" in str(result.issues[0])


def test_validate_missing_source_field(tmp_path):
    p = tmp_path / "jobs.yaml"
    p.write_text("jobs:\n  - id: bad\n    name: bad\n")
    result = validate(str(p))
    assert not result.ok
    assert "job #0" in str(result.issues[0])


def test_validate_malformed_source_not_a_mapping(tmp_path):
    p = tmp_path / "jobs.yaml"
    p.write_text("jobs:\n  - id: bad\n    name: bad\n    source: not-a-mapping\n")
    result = validate(str(p))
    assert not result.ok


def test_validate_non_mapping_matrix(tmp_path):
    p = tmp_path / "jobs.yaml"
    p.write_text(
        "jobs:\n"
        "  - id: bad\n"
        "    name: bad\n"
        "    matrix: 5\n"
        "    source: { kind: html, config: { url: 'https://example.com' } }\n"
    )
    result = validate(str(p))
    assert not result.ok
    assert "'matrix:' must be a mapping" in str(result.issues[0])
    assert "job #0" in str(result.issues[0])


def test_validate_matrix_variable_not_a_list(tmp_path):
    p = tmp_path / "jobs.yaml"
    p.write_text(
        "jobs:\n"
        "  - id: bad\n"
        "    name: bad\n"
        "    matrix: { region: not-a-list }\n"
        "    source: { kind: html, config: { url: 'https://example.com' } }\n"
    )
    result = validate(str(p))
    assert not result.ok
    assert "matrix variable 'region' must be a list" in str(result.issues[0])


def test_validate_duplicate_job_id_within_file(tmp_path):
    p = tmp_path / "jobs.yaml"
    p.write_text(
        "jobs:\n"
        "  - id: dup\n"
        "    name: dup\n"
        "    source: { kind: html, config: { url: 'https://example.com/a' } }\n"
        "  - id: dup\n"
        "    name: dup\n"
        "    source: { kind: html, config: { url: 'https://example.com/b' } }\n"
    )
    result = validate(str(p))
    assert not result.ok
    assert result.loaded == ["dup"]
    assert any("duplicate job id" in str(i) for i in result.issues)


def test_validate_env_var_substitution(tmp_path, monkeypatch):
    monkeypatch.setenv("UJIN_TEST_URL", "https://example.com/from-env")
    p = tmp_path / "jobs.yaml"
    p.write_text(
        "jobs:\n"
        "  - id: envjob\n"
        "    name: envjob\n"
        "    source: { kind: html, config: { url: '${UJIN_TEST_URL}' } }\n"
    )
    result = validate(str(p))
    assert result.ok
    assert result.loaded == ["envjob"]


def test_validate_workflows_dir_valid(tmp_path):
    wf = tmp_path / "workflows"
    wf.mkdir()
    (wf / "watch-example.yaml").write_text(
        "source: { kind: html, config: { url: 'https://example.com' } }\n"
    )
    result = validate(workflows_dir=str(wf))
    assert result.ok
    assert result.loaded == ["watch-example"]


def test_validate_workflows_dir_missing(tmp_path):
    result = validate(workflows_dir=str(tmp_path / "nope"))
    assert not result.ok
    assert "workflows directory not found" in str(result.issues[0])


def test_validate_workflows_dir_bad_file_reported_not_fatal(tmp_path):
    wf = tmp_path / "workflows"
    wf.mkdir()
    (wf / "broken.yaml").write_text("source: not-a-mapping\n")
    (wf / "good.yaml").write_text(
        "source: { kind: html, config: { url: 'https://example.com' } }\n"
    )
    result = validate(workflows_dir=str(wf))
    assert not result.ok
    assert result.loaded == ["good"]


def test_validate_workflows_dir_duplicate_id_across_files(tmp_path):
    wf = tmp_path / "workflows"
    wf.mkdir()
    (wf / "one.yaml").write_text(
        "jobs:\n"
        "  - id: dup\n"
        "    source: { kind: html, config: { url: 'https://example.com/a' } }\n"
    )
    (wf / "two.yaml").write_text(
        "jobs:\n"
        "  - id: dup\n"
        "    source: { kind: html, config: { url: 'https://example.com/b' } }\n"
    )
    result = validate(workflows_dir=str(wf))
    assert not result.ok
    assert any("duplicate job id" in str(i) for i in result.issues)


def test_validate_jobs_file_and_workflows_dir_together(tmp_path):
    p = tmp_path / "jobs.yaml"
    p.write_text(VALID_JOB)
    wf = tmp_path / "workflows"
    wf.mkdir()
    (wf / "watch-docs.yaml").write_text(
        "source: { kind: html, config: { url: 'https://example.com/docs' } }\n"
    )
    result = validate(str(p), workflows_dir=str(wf))
    assert result.ok
    assert sorted(result.loaded) == ["watch-docs", "watch-example"]


def test_validate_matrix_with_valid_lists_ok(tmp_path):
    p = tmp_path / "jobs.yaml"
    p.write_text(
        "jobs:\n"
        "  - id: fanout\n"
        "    name: fanout\n"
        "    matrix: { region: [us, eu] }\n"
        "    source: { kind: html, config: { url: 'https://example.com' } }\n"
    )
    result = validate(str(p))
    assert result.ok
    assert result.loaded == ["fanout"]


def test_validate_jobs_file_top_level_plain_list(tmp_path):
    p = tmp_path / "jobs.yaml"
    p.write_text(
        "- id: watch-example\n"
        "  name: watch-example\n"
        "  source: { kind: html, config: { url: 'https://example.com' } }\n"
    )
    result = validate(str(p))
    assert result.ok
    assert result.loaded == ["watch-example"]


def test_validate_workflow_file_invalid_yaml(tmp_path):
    wf = tmp_path / "workflows"
    wf.mkdir()
    (wf / "broken.yaml").write_text("source: [\n  - unterminated\n")
    result = validate(workflows_dir=str(wf))
    assert not result.ok
    assert "invalid YAML" in str(result.issues[0])


def test_validate_workflow_file_non_mapping_entry_in_jobs_list(tmp_path):
    wf = tmp_path / "workflows"
    wf.mkdir()
    (wf / "mixed.yaml").write_text(
        "jobs:\n"
        "  - not-a-mapping\n"
        "  - source: { kind: html, config: { url: 'https://example.com' } }\n"
    )
    result = validate(workflows_dir=str(wf))
    assert not result.ok
    assert result.loaded == ["mixed-1"]
    assert any("job entry must be a mapping" in str(i) for i in result.issues)


def test_validation_issue_str_format():
    from ujin.jobs.validate import ValidationIssue

    issue = ValidationIssue("jobs.yaml", "job #0", "bad thing")
    assert str(issue) == "jobs.yaml [job #0]: bad thing"


# --- jobs.yaml: defaults / include / use --------------------------------

def test_validate_jobs_file_defaults_and_include_ok(tmp_path):
    (tmp_path / "sink.yaml").write_text("sinks:\n  - kind: sqlite\n")
    p = tmp_path / "jobs.yaml"
    p.write_text(
        "defaults:\n"
        "  schedule: { mode: adaptive, base: 60 }\n"
        "use: sink.yaml\n"
        "jobs:\n"
        "  - id: withdef\n"
        "    name: withdef\n"
        "    source: { kind: html, config: { url: 'https://example.com' } }\n"
    )
    result = validate(str(p))
    assert result.ok
    assert result.loaded == ["withdef"]


def test_validate_jobs_file_missing_include_reports_issue(tmp_path):
    p = tmp_path / "jobs.yaml"
    p.write_text(
        "jobs:\n"
        "  - id: bad\n"
        "    name: bad\n"
        "    include: does-not-exist.yaml\n"
        "    source: { kind: html, config: { url: 'https://example.com' } }\n"
    )
    result = validate(str(p))
    assert not result.ok
    assert "not found" in str(result.issues[0])
    assert "job #0" in str(result.issues[0])


# --- workflow files: matrix / for_each fan-out ---------------------------

def test_validate_workflow_matrix_fanout_ok(tmp_path):
    wf = tmp_path / "workflows"
    wf.mkdir()
    (wf / "sites.yaml").write_text(
        "matrix:\n"
        "  - { site: a }\n"
        "  - { site: b }\n"
        "name: \"watch-{{ site }}\"\n"
        "source: { kind: html, config: { url: 'https://{{ site }}.example.com' } }\n"
        "sinks: [{ kind: sqlite }]\n"
    )
    result = validate(workflows_dir=str(wf))
    assert result.ok
    assert sorted(result.loaded) == ["sites-0", "sites-1"]


def test_validate_workflow_matrix_bad_shape_reports_issue(tmp_path):
    wf = tmp_path / "workflows"
    wf.mkdir()
    (wf / "bad.yaml").write_text(
        "matrix: 5\n"
        "source: { kind: html, config: { url: 'https://example.com' } }\n"
    )
    result = validate(workflows_dir=str(wf))
    assert not result.ok
    assert "must be a non-empty list" in str(result.issues[0])
    assert result.loaded == []


def test_validate_workflow_matrix_duplicate_id_across_files(tmp_path):
    wf = tmp_path / "workflows"
    wf.mkdir()
    (wf / "one.yaml").write_text(
        "jobs:\n"
        "  - id: dup\n"
        "    source: { kind: html, config: { url: 'https://example.com/a' } }\n"
    )
    (wf / "two.yaml").write_text(
        "matrix:\n"
        "  - { site: dup }\n"
        "id: \"{{ site }}\"\n"
        "source: { kind: html, config: { url: 'https://example.com/b' } }\n"
        "sinks: [{ kind: sqlite }]\n"
    )
    result = validate(workflows_dir=str(wf))
    assert not result.ok
    assert result.loaded == ["dup"]
    assert any("duplicate job id" in str(i) for i in result.issues)


def test_validate_workflow_file_missing_include_single_job(tmp_path):
    wf = tmp_path / "workflows"
    wf.mkdir()
    (wf / "one.yaml").write_text(
        "use: nope.yaml\n"
        "source: { kind: html, config: { url: 'https://example.com' } }\n"
    )
    result = validate(workflows_dir=str(wf))
    assert not result.ok
    assert "not found" in str(result.issues[0])


def test_validate_workflow_file_missing_include_in_jobs_list(tmp_path):
    wf = tmp_path / "workflows"
    wf.mkdir()
    (wf / "one.yaml").write_text(
        "jobs:\n"
        "  - id: a\n"
        "    include: nope.yaml\n"
        "    source: { kind: html, config: { url: 'https://example.com/a' } }\n"
        "  - id: b\n"
        "    source: { kind: html, config: { url: 'https://example.com/b' } }\n"
    )
    result = validate(workflows_dir=str(wf))
    assert not result.ok
    assert result.loaded == ["b"]
    assert any("not found" in str(i) for i in result.issues)


def test_validate_workflow_file_defaults_and_use_ok(tmp_path):
    wf = tmp_path / "workflows"
    wf.mkdir()
    frag_dir = wf / "fragments"
    frag_dir.mkdir()
    (frag_dir / "sink.yaml").write_text("sinks:\n  - kind: sqlite\n")
    (wf / "one.yaml").write_text(
        "defaults:\n"
        "  schedule: { mode: adaptive, base: 60 }\n"
        "use: fragments/sink.yaml\n"
        "source: { kind: html, config: { url: 'https://example.com' } }\n"
    )
    result = validate(workflows_dir=str(wf))
    assert result.ok
    assert result.loaded == ["one"]


# --- ingest plan (--plan) -------------------------------------------------

VALID_PLAN = """\
defaults:
  sinks: [{ kind: sqlite }]
jobs:
  - id: status-${name}
    matrix:
      name: [github, npm]
    source:
      kind: html
      config: { url: "https://example.com/${name}" }
"""


def test_validate_plan_file_ok(tmp_path):
    p = tmp_path / "plan.yaml"
    p.write_text(VALID_PLAN)
    result = validate(plan_path=str(p))
    assert result.ok
    assert sorted(result.loaded) == ["status-github", "status-npm"]


def test_validate_plan_missing_path_reports_issue(tmp_path):
    result = validate(plan_path=str(tmp_path / "nope.yaml"))
    assert not result.ok
    assert "not found" in str(result.issues[0])


def test_validate_plan_malformed_file_reports_issue(tmp_path):
    p = tmp_path / "plan.yaml"
    p.write_text("jobs: not-a-list\n")
    result = validate(plan_path=str(p))
    assert not result.ok
    assert "must be a list" in str(result.issues[0])


def test_validate_plan_bad_matrix_shape_reports_issue(tmp_path):
    p = tmp_path / "plan.yaml"
    p.write_text(
        "jobs:\n"
        "  - id: bad\n"
        "    matrix: not-a-mapping-or-list\n"
        "    source: { kind: html, config: { url: 'https://example.com' } }\n"
    )
    result = validate(plan_path=str(p))
    assert not result.ok
    assert "matrix" in str(result.issues[0])


def test_validate_plan_duplicate_job_id_reports_issue(tmp_path):
    p = tmp_path / "plan.yaml"
    p.write_text(
        "jobs:\n"
        "  - id: dup\n"
        "    source: { kind: html, config: { url: 'https://example.com/a' } }\n"
        "  - id: dup\n"
        "    source: { kind: html, config: { url: 'https://example.com/b' } }\n"
    )
    result = validate(plan_path=str(p))
    assert not result.ok
    assert result.loaded == ["dup"]
    assert any("duplicate job id" in str(i) for i in result.issues)


def test_validate_plan_directory_ok(tmp_path):
    d = tmp_path / "plan.d"
    d.mkdir()
    (d / "00-a.yaml").write_text(
        "jobs:\n  - id: a\n    source: { kind: html, config: { url: 'https://example.com/a' } }\n"
    )
    (d / "01-b.yaml").write_text(
        "jobs:\n  - id: b\n    source: { kind: html, config: { url: 'https://example.com/b' } }\n"
    )
    result = validate(plan_path=str(d))
    assert result.ok
    assert sorted(result.loaded) == ["a", "b"]


def test_validate_plan_directory_bad_file_reported_not_fatal(tmp_path):
    d = tmp_path / "plan.d"
    d.mkdir()
    (d / "00-bad.yaml").write_text("jobs: not-a-list\n")
    (d / "01-good.yaml").write_text(
        "jobs:\n  - id: good\n    source: { kind: html, config: { url: 'https://example.com' } }\n"
    )
    result = validate(plan_path=str(d))
    assert not result.ok
    assert result.loaded == ["good"]
    assert any("must be a list" in str(i) for i in result.issues)


def test_validate_all_three_sources_together(tmp_path):
    jobs = tmp_path / "jobs.yaml"
    jobs.write_text(VALID_JOB)
    wf = tmp_path / "workflows"
    wf.mkdir()
    (wf / "watch-docs.yaml").write_text(
        "source: { kind: html, config: { url: 'https://example.com/docs' } }\n"
    )
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        "jobs:\n  - id: from-plan\n    source: { kind: html, config: { url: 'https://example.com/plan' } }\n"
    )
    result = validate(str(jobs), workflows_dir=str(wf), plan_path=str(plan))
    assert result.ok
    assert sorted(result.loaded) == ["from-plan", "watch-docs", "watch-example"]
