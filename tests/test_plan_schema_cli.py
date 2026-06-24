"""ujin plan schema — offline, deterministic tests.

Covers: the subcommand emits a single JSON document to stdout that parses as JSON
and is a draft-2020-12 JSON Schema; the emitted schema accepts every shipped
example plan/workflow under ``examples/`` and rejects deliberately-malformed
plans; ``--out PATH`` writes the schema to a file (and keeps stdout clean); output
is deterministic; and the write-error path is handled. All offline — no network,
no live server.
"""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
import yaml
from jsonschema import Draft202012Validator

from ujin.cli import main
from ujin.jobs.schema import SCHEMA_DIALECT, plan_schema

# Repo root → the shipped examples directory.
_ROOT = Path(__file__).resolve().parents[1]
_EXAMPLES = _ROOT / "examples"

# Every shipped plan/workflow file. ``marketplace_profiles.yaml`` is a marketplace
# source-profiles reference (consumed by the marketplace_search source), not an
# ingest plan, so it is excluded; the ``data/`` and ``workflows/fragments/``
# subdirectories hold matrix rows / spliced fragments rather than standalone plans.
_PLAN_FILES = [
    p
    for p in (sorted(_EXAMPLES.glob("*.yaml")) + sorted((_EXAMPLES / "workflows").glob("*.yaml")))
    if p.name != "marketplace_profiles.yaml"
]


# ── helpers ─────────────────────────────────────────────────────────────────── #

def _emit_schema(capsys) -> dict:
    """Run ``ujin plan schema`` and return the JSON parsed from stdout."""
    rc = main(["plan", "schema"])
    assert rc == 0
    out = capsys.readouterr().out
    return json.loads(out)


# ── stdout: a single JSON Schema document ───────────────────────────────────── #

def test_schema_exits_zero(capsys):
    rc = main(["plan", "schema"])
    assert rc == 0


def test_schema_stdout_parses_as_json(capsys):
    main(["plan", "schema"])
    out = capsys.readouterr().out
    schema = json.loads(out)  # raises if stdout is not a single JSON document
    assert isinstance(schema, dict)


def test_schema_has_top_level_schema_keys(capsys):
    schema = _emit_schema(capsys)
    # A JSON Schema: declares its dialect and constrains a typed, propertied document.
    assert "$schema" in schema
    assert "type" in schema
    assert "properties" in schema
    assert "$defs" in schema


def test_schema_declares_draft_2020_12(capsys):
    schema = _emit_schema(capsys)
    assert schema["$schema"] == SCHEMA_DIALECT
    assert schema["$schema"].endswith("draft/2020-12/schema")


def test_emitted_schema_matches_module(capsys):
    # The CLI must emit exactly what ujin.jobs.schema.plan_schema() builds.
    schema = _emit_schema(capsys)
    assert schema == plan_schema()


def test_schema_is_itself_a_valid_json_schema(capsys):
    schema = _emit_schema(capsys)
    Draft202012Validator.check_schema(schema)  # raises on a malformed meta-schema


def test_schema_output_is_deterministic(capsys):
    main(["plan", "schema"])
    first = capsys.readouterr().out
    main(["plan", "schema"])
    second = capsys.readouterr().out
    assert first == second


# ── accepts every shipped example ───────────────────────────────────────────── #

def test_example_files_were_discovered():
    # Guard against a glob that silently finds nothing (which would make the
    # parametrized acceptance test vacuously pass).
    names = {p.name for p in _PLAN_FILES}
    assert "ingest-plan.yaml" in names
    assert "crossref-papers.yaml" in names
    assert len(_PLAN_FILES) >= 8


@pytest.mark.parametrize("path", _PLAN_FILES, ids=lambda p: p.name)
def test_emitted_schema_accepts_shipped_example(path, capsys):
    schema = _emit_schema(capsys)
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    # validate() raises jsonschema.ValidationError if the example does not conform.
    Draft202012Validator(schema).validate(doc)


def test_schema_accepts_ingest_plan_and_a_workflow(capsys):
    # The headline acceptance criterion, asserted explicitly.
    schema = _emit_schema(capsys)
    v = Draft202012Validator(schema)
    plan = yaml.safe_load((_EXAMPLES / "ingest-plan.yaml").read_text(encoding="utf-8"))
    workflow = yaml.safe_load(
        (_EXAMPLES / "workflows" / "crossref-papers.yaml").read_text(encoding="utf-8")
    )
    v.validate(plan)
    v.validate(workflow)


# ── rejects malformed plans ─────────────────────────────────────────────────── #

_MALFORMED = [
    pytest.param("just a string", id="scalar-string-document"),
    pytest.param(42, id="scalar-number-document"),
    pytest.param({"jobs": "not-a-list"}, id="jobs-not-a-list"),
    pytest.param(
        {"jobs": [{"id": "bad", "source": "https://example.com/"}]},
        id="job-source-is-a-string",
    ),
    pytest.param({"source": 123}, id="single-job-source-not-object"),
    pytest.param({"matrix": "everything"}, id="matrix-is-a-scalar"),
]


@pytest.mark.parametrize("bad", _MALFORMED)
def test_emitted_schema_rejects_malformed_plan(bad, capsys):
    schema = _emit_schema(capsys)
    with pytest.raises(jsonschema.ValidationError):
        Draft202012Validator(schema).validate(bad)


# ── --out PATH ──────────────────────────────────────────────────────────────── #

def test_out_writes_file_and_keeps_stdout_clean(tmp_path, capsys):
    dest = tmp_path / "ingest-plan.schema.json"
    rc = main(["plan", "schema", "--out", str(dest)])
    assert rc == 0

    captured = capsys.readouterr()
    assert captured.out == ""  # nothing on stdout — the schema went to the file
    assert str(dest) in captured.err  # a confirmation line on stderr

    written = json.loads(dest.read_text(encoding="utf-8"))
    assert written == plan_schema()


def test_out_file_matches_stdout(tmp_path, capsys):
    main(["plan", "schema"])
    stdout_schema = json.loads(capsys.readouterr().out)

    dest = tmp_path / "schema.json"
    main(["plan", "schema", "--out", str(dest)])
    capsys.readouterr()
    file_schema = json.loads(dest.read_text(encoding="utf-8"))

    assert file_schema == stdout_schema


def test_out_write_error_exits_nonzero(tmp_path, capsys):
    # Writing to a path whose parent does not exist raises OSError → clean exit 1.
    dest = tmp_path / "missing-dir" / "schema.json"
    rc = main(["plan", "schema", "--out", str(dest)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "ujin: cannot write schema to" in err
    assert not dest.exists()
