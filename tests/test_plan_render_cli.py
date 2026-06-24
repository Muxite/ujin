"""ujin plan render — offline, deterministic tests.

Covers: render of an ingest-plan file, render of a matrix template (fan-out
visible in output), --json output (valid JSON list, length matches resolved
count), and failure cases (missing path + plan with a failing job).
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


# ── render of an ingest-plan file ───────────────────────────────────────────── #

def test_render_ingest_plan_exits_zero(tmp_path, capsys):
    plan = tmp_path / "jobs.yaml"
    _write(plan, [
        {"id": "alpha", **_minimal_job("https://a/")},
        {"id": "beta",  **_minimal_job("https://b/")},
    ])
    rc = main(["plan", "render", str(plan)])
    assert rc == 0


def test_render_ingest_plan_contains_all_job_ids(tmp_path, capsys):
    plan = tmp_path / "jobs.yaml"
    _write(plan, [
        {"id": "alpha", **_minimal_job("https://a/")},
        {"id": "beta",  **_minimal_job("https://b/")},
    ])
    main(["plan", "render", str(plan)])
    out = capsys.readouterr().out
    assert "alpha" in out
    assert "beta" in out


def test_render_ingest_plan_contains_source_url(tmp_path, capsys):
    plan = tmp_path / "jobs.yaml"
    _write(plan, [{"id": "myjob", **_minimal_job("https://example.com/api")}])
    main(["plan", "render", str(plan)])
    out = capsys.readouterr().out
    assert "https://example.com/api" in out


def test_render_ingest_plan_uses_yaml_separators(tmp_path, capsys):
    plan = tmp_path / "jobs.yaml"
    _write(plan, [
        {"id": "alpha", **_minimal_job("https://a/")},
        {"id": "beta",  **_minimal_job("https://b/")},
    ])
    main(["plan", "render", str(plan)])
    out = capsys.readouterr().out
    # Each job starts with a "--- # <id>" header line
    assert "--- # alpha" in out
    assert "--- # beta" in out


# ── render of a matrix template — one entry per row ─────────────────────────── #

def test_render_matrix_expands_to_one_entry_per_row(tmp_path, capsys):
    plan = tmp_path / "plan.yaml"
    _write(plan, {
        "jobs": [
            {
                "id": "feed-{{ slug }}",
                "matrix": [
                    {"slug": "tech"},
                    {"slug": "science"},
                    {"slug": "business"},
                ],
                **_minimal_job("https://feeds.example.com/{{ slug }}"),
            }
        ]
    })
    rc = main(["plan", "render", str(plan)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "feed-tech" in out
    assert "feed-science" in out
    assert "feed-business" in out
    # All three matrix rows visible — one per entry
    assert out.count("--- #") == 3


def test_render_matrix_substitutes_url_placeholders(tmp_path, capsys):
    plan = tmp_path / "plan.yaml"
    _write(plan, {
        "jobs": [
            {
                "id": "feed-{{ slug }}",
                "matrix": [{"slug": "tech"}, {"slug": "science"}],
                **_minimal_job("https://feeds.example.com/{{ slug }}"),
            }
        ]
    })
    main(["plan", "render", str(plan)])
    out = capsys.readouterr().out
    assert "feeds.example.com/tech" in out
    assert "feeds.example.com/science" in out
    assert "{{ slug }}" not in out


# ── defaults: deep-merge visible in render output ────────────────────────────── #

def test_render_shows_defaults_merged_into_jobs(tmp_path, capsys):
    plan = tmp_path / "plan.yaml"
    _write(plan, {
        "defaults": {
            "schedule": {"mode": "once"},
            "sinks": [{"kind": "stdout", "config": {}}],
        },
        "jobs": [
            {"id": "j1", "source": {"kind": "http", "config": {"url": "https://a/"}}},
        ],
    })
    main(["plan", "render", str(plan)])
    out = capsys.readouterr().out
    # The stdout sink from defaults must appear in the merged output
    assert "stdout" in out
    assert "once" in out


# ── --json output ─────────────────────────────────────────────────────────────── #

def test_json_render_exits_zero(tmp_path, capsys):
    plan = tmp_path / "jobs.yaml"
    _write(plan, [
        {"id": "alpha", **_minimal_job("https://a/")},
        {"id": "beta",  **_minimal_job("https://b/")},
    ])
    rc = main(["plan", "render", str(plan), "--json"])
    assert rc == 0


def test_json_render_emits_valid_json_list(tmp_path, capsys):
    plan = tmp_path / "jobs.yaml"
    _write(plan, [
        {"id": "alpha", **_minimal_job("https://a/")},
        {"id": "beta",  **_minimal_job("https://b/")},
    ])
    main(["plan", "render", str(plan), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert isinstance(data, list)


def test_json_render_length_equals_resolved_count(tmp_path, capsys):
    plan = tmp_path / "jobs.yaml"
    _write(plan, [
        {"id": "alpha", **_minimal_job("https://a/")},
        {"id": "beta",  **_minimal_job("https://b/")},
    ])
    main(["plan", "render", str(plan), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 2


def test_json_render_contains_spec_fields(tmp_path, capsys):
    plan = tmp_path / "jobs.yaml"
    _write(plan, [{"id": "myjob", **_minimal_job("https://example.com/")}])
    main(["plan", "render", str(plan), "--json"])
    data = json.loads(capsys.readouterr().out)
    spec = data[0]
    assert spec["id"] == "myjob"
    assert "source" in spec
    assert "schedule" in spec


def test_json_render_matrix_list_length(tmp_path, capsys):
    plan = tmp_path / "plan.yaml"
    _write(plan, {
        "jobs": [
            {
                "id": "feed-{{ slug }}",
                "matrix": [{"slug": "tech"}, {"slug": "science"}, {"slug": "business"}],
                **_minimal_job("https://feeds.example.com/{{ slug }}"),
            }
        ]
    })
    main(["plan", "render", str(plan), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 3
    ids = {s["id"] for s in data}
    assert ids == {"feed-tech", "feed-science", "feed-business"}


def test_json_render_workflows_dir(tmp_path, capsys):
    _write(tmp_path / "job-a.yaml", _minimal_job("https://a/"))
    _write(tmp_path / "job-b.yaml", _minimal_job("https://b/"))
    rc = main(["plan", "render", str(tmp_path), "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert isinstance(data, list)
    assert len(data) == 2
    assert {s["id"] for s in data} == {"job-a", "job-b"}


# ── failure cases ─────────────────────────────────────────────────────────────── #

def test_missing_path_exits_nonzero(tmp_path, capsys):
    rc = main(["plan", "render", str(tmp_path / "does-not-exist.yaml")])
    assert rc != 0


def test_missing_path_clean_error_message(tmp_path, capsys):
    path = str(tmp_path / "does-not-exist.yaml")
    main(["plan", "render", path])
    err = capsys.readouterr().err
    assert "ujin:" in err
    assert "does-not-exist.yaml" in err


def test_missing_path_no_traceback(tmp_path, capsys):
    path = str(tmp_path / "no-such.yaml")
    main(["plan", "render", path])
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "Exception" not in err


def test_failing_job_exits_nonzero(tmp_path, capsys):
    plan = tmp_path / "plan.yaml"
    _write(plan, [
        {"id": "good", **_minimal_job("https://a/")},
        {"id": "bad", "matrix": "not-a-list", **_minimal_job("https://b/")},
    ])
    rc = main(["plan", "render", str(plan)])
    assert rc != 0


def test_failing_job_clean_error_message(tmp_path, capsys):
    plan = tmp_path / "plan.yaml"
    _write(plan, [
        {"id": "bad", "matrix": "not-a-list", **_minimal_job("https://b/")},
    ])
    main(["plan", "render", str(plan)])
    err = capsys.readouterr().err
    assert "ujin:" in err
    assert "Traceback" not in err


def test_failing_job_no_traceback(tmp_path, capsys):
    plan = tmp_path / "plan.yaml"
    _write(plan, [{"id": "bad", "matrix": "oops", **_minimal_job()}])
    main(["plan", "render", str(plan)])
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "Exception" not in err
