"""Verify the shipped example files validate and render via the CLI.

Guards against doc/example drift: if an example file referenced in the docs
stops loading (bad YAML, renamed fragment, stale kind), this test fails before
the docs become misleading. All tests are offline and deterministic; no network.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ujin.cli import main

_ROOT = Path(__file__).resolve().parents[1]
_EXAMPLES_PLAN = _ROOT / "examples" / "ingest-plan.yaml"
_EXAMPLES_WORKFLOWS = _ROOT / "examples" / "workflows"
_EXAMPLES_MATRIX_FROM_FILE = _ROOT / "examples" / "matrix-from-file.yaml"


# ── examples/ingest-plan.yaml ─────────────────────────────────────────────── #

def test_ingest_plan_example_validates(capsys):
    """examples/ingest-plan.yaml must resolve to all four expected job ids."""
    rc = main(["plan", "validate", str(_EXAMPLES_PLAN)])
    assert rc == 0


def test_ingest_plan_example_resolves_expected_ids(capsys):
    main(["plan", "validate", str(_EXAMPLES_PLAN)])
    out = capsys.readouterr().out
    for job_id in ("crossref", "feed-tech", "feed-science", "feed-business"):
        assert f"ok  {job_id}" in out, f"expected id {job_id!r} in plan validate output"


def test_ingest_plan_example_json_validate(capsys):
    rc = main(["plan", "validate", str(_EXAMPLES_PLAN), "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert set(data["resolved"]) == {"crossref", "feed-tech", "feed-science", "feed-business"}
    assert data["failed"] == []


def test_ingest_plan_example_renders(capsys):
    """examples/ingest-plan.yaml must fully render (defaults + includes + matrix)."""
    rc = main(["plan", "render", str(_EXAMPLES_PLAN)])
    assert rc == 0


def test_ingest_plan_example_render_expands_matrix(capsys):
    main(["plan", "render", str(_EXAMPLES_PLAN)])
    out = capsys.readouterr().out
    # All four fully-expanded documents must be present
    assert "--- # crossref" in out
    assert "--- # feed-tech" in out
    assert "--- # feed-science" in out
    assert "--- # feed-business" in out


def test_ingest_plan_example_render_json(capsys):
    rc = main(["plan", "render", str(_EXAMPLES_PLAN), "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert isinstance(data, list)
    assert len(data) == 4
    ids = {s["id"] for s in data}
    assert ids == {"crossref", "feed-tech", "feed-science", "feed-business"}


# ── examples/workflows/ directory ────────────────────────────────────────── #

def test_workflows_dir_example_validates(capsys):
    """examples/workflows/ must resolve all expected workflow ids."""
    rc = main(["plan", "validate", str(_EXAMPLES_WORKFLOWS)])
    assert rc == 0


def test_workflows_dir_example_expected_ids(capsys):
    main(["plan", "validate", str(_EXAMPLES_WORKFLOWS)])
    out = capsys.readouterr().out
    # The directory ships: crossref-papers, example-page, marketplace-search (matrix
    # -> search-laptop/gpu/monitor), site-feeds (jobs list -> site-feeds-0/1)
    for job_id in (
        "crossref-papers",
        "example-page",
        "search-laptop",
        "search-gpu",
        "search-monitor",
        "site-feeds-0",
        "site-feeds-1",
    ):
        assert f"ok  {job_id}" in out, f"expected id {job_id!r} in workflows validate output"


def test_workflows_dir_example_renders(capsys):
    """examples/workflows/ must fully render without failures."""
    rc = main(["plan", "render", str(_EXAMPLES_WORKFLOWS)])
    assert rc == 0


def test_workflows_dir_example_render_json(capsys):
    rc = main(["plan", "render", str(_EXAMPLES_WORKFLOWS), "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert isinstance(data, list)
    assert len(data) == 7


# ── examples/matrix-from-file.yaml (0.25: file-backed matrix) ─────────────── #

def test_matrix_from_file_example_validates(capsys):
    """examples/matrix-from-file.yaml must load three jobs from the external data file."""
    rc = main(["plan", "validate", str(_EXAMPLES_MATRIX_FROM_FILE)])
    assert rc == 0


def test_matrix_from_file_example_expected_ids(capsys):
    main(["plan", "validate", str(_EXAMPLES_MATRIX_FROM_FILE)])
    out = capsys.readouterr().out
    for job_id in ("search-laptop", "search-gpu", "search-monitor"):
        assert f"ok  {job_id}" in out, f"expected id {job_id!r} in matrix-from-file output"


def test_matrix_from_file_example_renders(capsys):
    rc = main(["plan", "render", str(_EXAMPLES_MATRIX_FROM_FILE)])
    assert rc == 0


def test_matrix_from_file_example_render_json(capsys):
    rc = main(["plan", "render", str(_EXAMPLES_MATRIX_FROM_FILE), "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert isinstance(data, list)
    assert len(data) == 3
    assert {s["id"] for s in data} == {"search-laptop", "search-gpu", "search-monitor"}


def test_matrix_from_file_example_json_validate(capsys):
    rc = main(["plan", "validate", str(_EXAMPLES_MATRIX_FROM_FILE), "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert set(data["resolved"]) == {"search-laptop", "search-gpu", "search-monitor"}
    assert data["failed"] == []
    assert "warnings" in data
    assert "strict" in data
