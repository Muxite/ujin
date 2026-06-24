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
from ujin.jobs.schema import SCHEMA_DIALECT, plan_schema, validate_document

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


# ── validate_document: the stdlib-only walker behind `plan validate --schema` ── #
#
# Same accept/reject behaviour as the jsonschema-based tests above, but exercising
# the hand-rolled walker directly (it must reject a structurally-malformed plan
# without depending on jsonschema, and name the offending JSON path).

def test_validate_document_accepts_every_shipped_example():
    schema = plan_schema()
    for path in _PLAN_FILES:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert validate_document(doc, schema) == [], path.name


@pytest.mark.parametrize("bad, where", [
    ("just a string", "$"),
    (42, "$"),
    (None, "$"),
    ({"jobs": "not-a-list"}, "$.jobs"),
    ({"jobs": [{"id": "bad", "source": "https://example.com/"}]}, "$.jobs[0].source"),
    ({"source": 123}, "$.source"),
    ({"matrix": "everything"}, "$.matrix"),
    ({"jobs": [{"id": "j", "transforms": "nope"}]}, "$.jobs[0].transforms"),
    ({"defaults": [1, 2]}, "$.defaults"),
    ({"schedule": "soon"}, "$.schedule"),
    ({"jobs": [{"sinks": [{"include": [1, 2]}]}]}, "$.jobs[0].sinks[0].include"),
])
def test_validate_document_rejects_and_names_location(bad, where):
    errors = validate_document(bad)
    assert errors, bad
    assert any(e.startswith(f"{where}:") for e in errors), errors


def test_validate_document_returns_empty_on_valid():
    doc = {"jobs": [{"id": "a", "source": {"kind": "http", "config": {"url": "x"}}}]}
    assert validate_document(doc) == []


def test_validate_document_accepts_both_matrix_shapes():
    # anyOf: an inline list OR an axes mapping both satisfy `matrix:`.
    assert validate_document({"matrix": [{"slug": "a"}]}) == []
    assert validate_document({"matrix": {"region": ["us", "eu"]}}) == []
    assert validate_document({"matrix": {"from": "rows.yaml"}}) == []


def test_validate_document_accepts_both_fragment_ref_shapes():
    # fragmentRef anyOf: a single path string OR a list of path strings.
    assert validate_document({"include": "frag.yaml"}) == []
    assert validate_document({"include": ["a.yaml", "b.yaml"]}) == []


def test_validate_document_message_is_actionable():
    (msg,) = validate_document({"jobs": "nope"})
    assert msg == "$.jobs: expected array, got string"


def test_validate_document_matches_jsonschema_on_malformed_cases():
    # The hand-rolled walker rejects exactly the cases the jsonschema validator does.
    schema = plan_schema()
    v = Draft202012Validator(schema)
    for bad in ["just a string", 42, {"jobs": "not-a-list"},
                {"jobs": [{"id": "bad", "source": "x"}]}, {"source": 123},
                {"matrix": "everything"}]:
        assert validate_document(bad, schema)          # walker rejects
        assert not v.is_valid(bad)                     # so does jsonschema
