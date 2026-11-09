"""Offline validation, shared resolution and editor schema contracts."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from ujin.cli import main
from ujin.jobs.validate import ValidationError, validate_path, workflow_schema

ROOT = Path(__file__).resolve().parents[1]


def write(tmp_path, text, name="job.yaml"):
    path = tmp_path / name
    path.write_text(text)
    return path


def test_examples():
    assert not validate_path(ROOT / "examples/workflows")
    assert not validate_path(ROOT / "examples/jobs.crossref.yaml")
    assert not validate_path(ROOT / "examples/ingest-plan.yaml")


@pytest.mark.parametrize("text,line,message", [
    ("source:\n  kind: mystery\n", 2, "unknown source kind"),
    ("name: empty\n", 1, "source must be a mapping"),
    ("source: {kind: http}\nschedule:\n  mode: sometimes\n", 3, "schedule mode"),
    ("source: {kind: http}\nmatrix: broken\n", 2, "must be a list"),
    ("source: {kind: http}\nfor_each: {x: wrong}\n", 2, "must be a list"),
    ("source: {config: {}}\n", 1, "requires a non-empty kind"),
    ("source: {kind: http}\ntransforms:\n  - kind: wrong\n", 3, "unknown transform"),
    ("source: {kind: http}\nsinks:\n  - kind: wrong\n", 3, "unknown sink"),
])
def test_diagnostics(tmp_path, text, line, message):
    path = write(tmp_path, text)
    errors = validate_path(path)
    assert len(errors) == 1
    assert errors[0].line == line
    assert errors[0].path == str(path)
    assert message in str(errors[0])
    assert str(errors[0]).startswith(f"{path}:{line}:")


def test_all_errors_and_job_scoping(tmp_path):
    path = write(tmp_path, """jobs:
  - source: {kind: wrong}
    sinks: [{kind: nope}]
    schedule: {mode: never}
  - source: {kind: wrong}
  - name: missing
""")
    errors = validate_path(path)
    assert [e.line for e in errors] == [2, 3, 4, 5, 6]


def test_includes_and_cycles(tmp_path):
    write(tmp_path, "include: fragment.yaml\n", "cycle.yaml")
    write(tmp_path, "include: cycle.yaml\n", "fragment.yaml")
    errors = validate_path(tmp_path / "cycle.yaml")
    assert "cyclic include" in errors[0].message
    assert errors[0].line == 1
    write(tmp_path, "source:\n  kind: unknown\n", "fragment.yaml")
    errors = validate_path(tmp_path / "cycle.yaml")
    assert errors == [ValidationError(str(tmp_path / "fragment.yaml"), 2, "unknown source kind 'unknown'")]


def test_resolution_errors_continue(tmp_path):
    path = write(tmp_path, "jobs:\n  - include: missing.yaml\n  - source: {kind: huh}\n  - matrix: broken\n")
    errors = validate_path(path)
    assert len(errors) == 3
    assert [e.line for e in errors] == [2, 3, 4]


def test_plan_templates_matrix_and_errors(tmp_path):
    path = write(tmp_path, """templates:
  feed: {source: {kind: rss}}
defaults: {schedule: {mode: once}}
jobs:
  - use: feed
    matrix: {region: [us, eu]}
  - use: missing
  - matrix: {region: []}
  - source: {kind: broken}
  - nope
""")
    errors = validate_path(path)
    assert len(errors) == 4
    assert [e.line for e in errors] == [7, 8, 9, 10]
    assert "unknown template" in errors[0].message
    assert "non-empty list" in errors[1].message


@pytest.mark.parametrize("text", [
    "jobs: nope\n", "defaults: nope\nsource: {kind: http}\n",
    "templates: []\njobs: []\n", "templates: {}\nextra: 1\njobs: []\n",
    "templates: {}\njobs: nope\n", "42\n", "source: [\n",
    "source: {kind: http}\ninclude: false\n",
])
def test_bad_documents(tmp_path, text):
    assert validate_path(write(tmp_path, text))


def test_yaml_line_and_missing_path(tmp_path):
    assert validate_path(write(tmp_path, "source:\n  kind: [\n"))[0].line == 3
    assert validate_path(tmp_path / "missing")[0].line == 1


@pytest.mark.parametrize("body,message", [
    ("source: {kind: 3}", "non-empty kind"),
    ("source: {kind: http, config: []}", "config must be a mapping"),
    ("source: {kind: http, extra: 1}", "unknown source key"),
    ("transforms: {}", "transforms must be a list"),
    ("sinks: [false]", "sink must be a mapping"),
    ("schedule: []", "schedule must be a mapping"),
    ("schedule: {extra: 1}", "unknown schedule key"),
    ("schedule: {base: nope}", "must be a number"),
    ("schedule: {cron: 1}", "must be a string"),
    ("name: 1", "name must be a string"),
    ("id: 2", "id must be a string"),
    ("enabled: yesplease", "enabled must be a boolean"),
])
def test_structural_errors(tmp_path, body, message):
    errors = validate_path(write(tmp_path, "source: {kind: http}\n" + body + "\n"))
    assert any(message in e.message for e in errors)


def test_defaults_and_matrix(tmp_path):
    path = write(tmp_path, """defaults:
  source: {kind: http}
  schedule: {base: 42}
jobs:
  - id: test-${NAME}
    matrix: [{NAME: a}, {NAME: b}]
  - source: {kind: rss}
""")
    assert validate_path(path) == []
    from ujin.jobs.app import _specs_from_workflow_file
    assert len(_specs_from_workflow_file(path)) == 3


def test_plugins_restored(tmp_path):
    from ujin.registry import register
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    write(plugins, "from ujin import register\n@register.source('test_validate')\ndef factory(cfg):\n    raise AssertionError('must not run')\n", "custom.py")
    path = write(tmp_path, "source: {kind: 'plugin:test_validate'}\n")
    assert validate_path(path)
    assert not validate_path(path, plugins=plugins)
    assert not register.has("source", "test_validate")
    write(plugins, "raise RuntimeError('bad plugin')\n", "broken.py")
    errors = validate_path(path, plugins=plugins)
    assert len(errors) == 1 and "failed to load" in errors[0].message
    assert not validate_path(write(tmp_path, "source: {kind: http}"), plugins=tmp_path / "absent")


def test_directory_glob(tmp_path):
    write(tmp_path, "source: {kind: unknown}\n", "bad.yml")
    write(tmp_path, "source: {kind: wrong}\n", "also.yaml")
    write(tmp_path, "broken", "ignored.txt")
    (tmp_path / "fragments").mkdir()
    write(tmp_path / "fragments", "broken")
    assert len(validate_path(tmp_path)) == 2
    empty = tmp_path / "empty"
    empty.mkdir()
    assert not validate_path(empty)


def test_schema_drift_and_cli(capsys):
    expected = json.dumps(workflow_schema(), indent=2, sort_keys=True) + "\n"
    assert (ROOT / "docs/schema/workflow.schema.json").read_text() == expected
    assert main(["validate", "--print-schema"]) == 0
    assert capsys.readouterr().out == expected


def test_cli(tmp_path, capsys):
    good = write(tmp_path, "source: {kind: http}\n")
    assert main(["validate", str(good)]) == 0
    assert capsys.readouterr().out == ""
    bad = write(tmp_path, "source: {kind: missing}\n", "bad.yaml")
    assert main(["validate", str(good), str(bad)]) == 1
    assert f"{bad}:1:" in capsys.readouterr().out
    assert main(["validate", str(bad), "--format", "json"]) == 1
    assert json.loads(capsys.readouterr().out)[0]["line"] == 1
    assert main(["validate", str(good), "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert main(["validate"]) == 1
    assert "at least one PATH" in capsys.readouterr().out


def test_imports_are_lazy():
    code = "import sys; import ujin.cli; import ujin.jobs.validate; assert 'yaml' not in sys.modules; assert 'pydantic' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], check=True, cwd=ROOT)


def test_malformed_fragment_does_not_hide_sibling_errors(tmp_path):
    fragment = write(tmp_path, "source:\n  kind: [\n", "fragment.yaml")
    path = write(tmp_path, "jobs:\n  - include: fragment.yaml\n  - source: {kind: typo}\n")
    errors = validate_path(path)
    assert len(errors) == 2
    assert (errors[0].path, errors[0].line) == (str(fragment), 3)
    assert errors[1].line == 3


def test_aliases_and_empty_yaml(tmp_path):
    # Node mark traversal must tolerate recursive YAML aliases.
    assert not validate_path(write(tmp_path, "source: {kind: http, config: &cfg {self: *cfg}}\n"))
    assert validate_path(write(tmp_path, ""))
    assert validate_path(write(tmp_path, "---\n"))
    # Matrix diagnostics identical across expansions are emitted once.
    errors = validate_path(write(tmp_path, "source: {kind: typo}\nmatrix: [{x: a}, {x: b}]\n"))
    assert len(errors) == 1


def test_bare_job_list_and_fragments_alias(tmp_path):
    errors = validate_path(write(tmp_path, "- source: {kind: http}\n- source: {kind: typo}\n"))
    assert len(errors) == 1 and errors[0].line == 2
    path = write(tmp_path, "fragments: {feed: {source: {kind: rss}}}\njobs: [{use: feed}]\n")
    assert not validate_path(path)


def test_schema_accepts_examples():
    jsonschema = pytest.importorskip("jsonschema")
    import yaml
    schema = workflow_schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    paths = [*sorted((ROOT / "examples/workflows").glob("*.yaml")), ROOT / "examples/jobs.crossref.yaml", ROOT / "examples/ingest-plan.yaml"]
    for path in paths:
        jsonschema.validate(yaml.safe_load(path.read_text()), schema)


def test_missing_yaml_dependency(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "yaml", None)
    errors = validate_path(tmp_path)
    assert len(errors) == 1 and "ujin[yaml]" in errors[0].message


def test_null_or_empty_jobs_load_zero_specs(tmp_path):
    """Regression: a bare `jobs:` once loaded no jobs; the shared-resolver refactor
    must not turn it into an error (server startup would report it as failed)."""
    from ujin.jobs.app import _specs_from_workflow_file

    for text in ("jobs:\n", "jobs: []\n"):
        assert _specs_from_workflow_file(write(tmp_path, text)) == []
