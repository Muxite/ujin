"""ujin plan eject — offline, deterministic tests.

Covers: basic eject writing one file per job id; defaults/matrix fully resolved
in the ejected files; --only/--exclude selectors; round-trip (each ejected file
loaded back through _specs_from_workflow_file yields the same JobSpec it had
inside the plan); missing path / failed job error paths; output directory
creation. All tests use tmp_path and the real loaders; no network.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ujin.cli import main
from ujin.jobs.app import _load_ingest_plan, _specs_from_workflow_file


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _minimal_job(url: str = "https://example.com/") -> dict:
    return {
        "source": {"kind": "http", "config": {"url": url}},
        "schedule": {"mode": "once"},
    }


def _spec_key(spec) -> dict:
    """Fields that must survive the eject/reload round-trip (created_at excluded)."""
    d = spec.to_dict()
    d.pop("created_at", None)
    return d


# ── basic eject ─────────────────────────────────────────────────────────────── #

def test_eject_writes_one_file_per_job(tmp_path):
    plan = tmp_path / "jobs.yaml"
    out = tmp_path / "out"
    _write(plan, [
        {"id": "alpha", **_minimal_job("https://a/")},
        {"id": "beta",  **_minimal_job("https://b/")},
    ])
    rc = main(["plan", "eject", str(plan), "--out", str(out)])
    assert rc == 0
    assert (out / "alpha.yaml").exists()
    assert (out / "beta.yaml").exists()
    assert len(list(out.glob("*.yaml"))) == 2


def test_eject_file_contains_resolved_source_url(tmp_path):
    plan = tmp_path / "jobs.yaml"
    out = tmp_path / "out"
    _write(plan, [{"id": "myjob", **_minimal_job("https://example.com/api")}])
    rc = main(["plan", "eject", str(plan), "--out", str(out)])
    assert rc == 0
    content = (out / "myjob.yaml").read_text()
    assert "https://example.com/api" in content


def test_eject_creates_out_dir_including_parents(tmp_path):
    plan = tmp_path / "jobs.yaml"
    out = tmp_path / "does" / "not" / "exist"
    _write(plan, [{"id": "alpha", **_minimal_job()}])
    rc = main(["plan", "eject", str(plan), "--out", str(out)])
    assert rc == 0
    assert out.is_dir()


def test_eject_empty_plan_exits_zero_no_files(tmp_path):
    plan = tmp_path / "empty.yaml"
    plan.write_text("", encoding="utf-8")
    out = tmp_path / "out"
    rc = main(["plan", "eject", str(plan), "--out", str(out)])
    assert rc == 0
    assert list(out.glob("*.yaml")) == []


def test_eject_file_is_valid_yaml(tmp_path):
    plan = tmp_path / "jobs.yaml"
    out = tmp_path / "out"
    _write(plan, [{"id": "myjob", **_minimal_job("https://example.com/")}])
    main(["plan", "eject", str(plan), "--out", str(out)])
    data = yaml.safe_load((out / "myjob.yaml").read_text())
    assert isinstance(data, dict)
    assert data["id"] == "myjob"


# ── defaults and matrix fully resolved ──────────────────────────────────────── #

def test_eject_resolves_defaults_into_ejected_file(tmp_path):
    plan = tmp_path / "plan.yaml"
    out = tmp_path / "out"
    _write(plan, {
        "defaults": {"schedule": {"mode": "once", "base": 999.0}},
        "jobs": [{"id": "j1", "source": {"kind": "http", "config": {"url": "https://x/"}}}],
    })
    rc = main(["plan", "eject", str(plan), "--out", str(out)])
    assert rc == 0
    data = yaml.safe_load((out / "j1.yaml").read_text())
    assert data["schedule"]["mode"] == "once"
    assert data["schedule"]["base"] == 999.0


def test_eject_matrix_writes_one_file_per_row(tmp_path):
    plan = tmp_path / "plan.yaml"
    out = tmp_path / "out"
    _write(plan, {
        "jobs": [{
            "id": "feed-{{ slug }}",
            "source": {"kind": "http", "config": {"url": "https://x/{{ slug }}"}},
            "schedule": {"mode": "once"},
            "matrix": [{"slug": "tech"}, {"slug": "science"}],
        }]
    })
    rc = main(["plan", "eject", str(plan), "--out", str(out)])
    assert rc == 0
    files = {f.stem for f in out.glob("*.yaml")}
    assert "feed-tech" in files
    assert "feed-science" in files
    assert len(files) == 2


def test_eject_matrix_resolves_placeholders_in_ejected_file(tmp_path):
    plan = tmp_path / "plan.yaml"
    out = tmp_path / "out"
    _write(plan, {
        "jobs": [{
            "id": "feed-{{ slug }}",
            "source": {"kind": "http", "config": {"url": "https://feeds/{{ slug }}"}},
            "schedule": {"mode": "once"},
            "matrix": [{"slug": "tech"}],
        }]
    })
    main(["plan", "eject", str(plan), "--out", str(out)])
    data = yaml.safe_load((out / "feed-tech.yaml").read_text())
    assert data["source"]["config"]["url"] == "https://feeds/tech"
    assert "{{" not in str(data)


def test_eject_ejected_file_has_no_matrix_key(tmp_path):
    plan = tmp_path / "plan.yaml"
    out = tmp_path / "out"
    _write(plan, {
        "jobs": [{
            "id": "feed-{{ slug }}",
            "source": {"kind": "http", "config": {"url": "https://x/{{ slug }}"}},
            "schedule": {"mode": "once"},
            "matrix": [{"slug": "tech"}],
        }]
    })
    main(["plan", "eject", str(plan), "--out", str(out)])
    data = yaml.safe_load((out / "feed-tech.yaml").read_text())
    assert "matrix" not in data
    assert "defaults" not in data


# ── round-trip through the single-file loader ────────────────────────────────── #

def test_eject_round_trips_plain_jobs(tmp_path):
    plan = tmp_path / "plan.yaml"
    out = tmp_path / "out"
    _write(plan, [
        {"id": "alpha", **_minimal_job("https://a/")},
        {"id": "beta",  **_minimal_job("https://b/")},
    ])
    original = {s.id: s for s in _load_ingest_plan(plan)}
    main(["plan", "eject", str(plan), "--out", str(out)])

    for ejected_file in sorted(out.glob("*.yaml")):
        [loaded] = _specs_from_workflow_file(ejected_file)
        orig = original[loaded.id]
        assert _spec_key(loaded) == _spec_key(orig)


def test_eject_round_trips_matrix_jobs(tmp_path):
    plan = tmp_path / "plan.yaml"
    out = tmp_path / "out"
    _write(plan, {
        "jobs": [{
            "id": "feed-{{ slug }}",
            "source": {"kind": "http", "config": {"url": "https://x/{{ slug }}"}},
            "schedule": {"mode": "once"},
            "matrix": [{"slug": "tech"}, {"slug": "science"}],
        }]
    })
    original = {s.id: s for s in _load_ingest_plan(plan)}
    main(["plan", "eject", str(plan), "--out", str(out)])

    for ejected_file in sorted(out.glob("*.yaml")):
        [loaded] = _specs_from_workflow_file(ejected_file)
        orig = original[loaded.id]
        assert _spec_key(loaded) == _spec_key(orig)


def test_eject_round_trips_defaults_merged_job(tmp_path):
    plan = tmp_path / "plan.yaml"
    out = tmp_path / "out"
    _write(plan, {
        "defaults": {
            "schedule": {"mode": "once", "base": 120.0},
            "sinks": [{"kind": "stdout", "config": {}}],
        },
        "jobs": [{"id": "j1", "source": {"kind": "http", "config": {"url": "https://x/"}}}],
    })
    original = {s.id: s for s in _load_ingest_plan(plan)}
    main(["plan", "eject", str(plan), "--out", str(out)])

    [loaded] = _specs_from_workflow_file(out / "j1.yaml")
    assert _spec_key(loaded) == _spec_key(original["j1"])
    assert loaded.schedule.base == 120.0
    assert len(loaded.sinks) == 1


# ── selectors ──────────────────────────────────────────────────────────────── #

def test_eject_only_selector_subset(tmp_path):
    plan = tmp_path / "jobs.yaml"
    out = tmp_path / "out"
    _write(plan, [
        {"id": "alpha", **_minimal_job("https://a/")},
        {"id": "beta",  **_minimal_job("https://b/")},
        {"id": "gamma", **_minimal_job("https://c/")},
    ])
    rc = main(["plan", "eject", str(plan), "--out", str(out), "--only", "alpha,beta"])
    assert rc == 0
    assert (out / "alpha.yaml").exists()
    assert (out / "beta.yaml").exists()
    assert not (out / "gamma.yaml").exists()


def test_eject_exclude_selector(tmp_path):
    plan = tmp_path / "jobs.yaml"
    out = tmp_path / "out"
    _write(plan, [
        {"id": "alpha", **_minimal_job("https://a/")},
        {"id": "beta",  **_minimal_job("https://b/")},
    ])
    rc = main(["plan", "eject", str(plan), "--out", str(out), "--exclude", "beta"])
    assert rc == 0
    assert (out / "alpha.yaml").exists()
    assert not (out / "beta.yaml").exists()


def test_eject_only_glob_pattern(tmp_path):
    plan = tmp_path / "plan.yaml"
    out = tmp_path / "out"
    _write(plan, {
        "jobs": [{
            "id": "feed-{{ slug }}",
            "source": {"kind": "http", "config": {"url": "https://x/{{ slug }}"}},
            "schedule": {"mode": "once"},
            "matrix": [{"slug": "tech"}, {"slug": "science"}],
        }]
    })
    rc = main(["plan", "eject", str(plan), "--out", str(out), "--only", "feed-*"])
    assert rc == 0
    assert (out / "feed-tech.yaml").exists()
    assert (out / "feed-science.yaml").exists()


def test_eject_only_no_match_exits_nonzero(tmp_path):
    plan = tmp_path / "jobs.yaml"
    out = tmp_path / "out"
    _write(plan, [{"id": "alpha", **_minimal_job()}])
    rc = main(["plan", "eject", str(plan), "--out", str(out), "--only", "nope"])
    assert rc != 0


# ── error paths ─────────────────────────────────────────────────────────────── #

def test_eject_missing_path_exits_nonzero(tmp_path, capsys):
    out = tmp_path / "out"
    rc = main(["plan", "eject", str(tmp_path / "nope.yaml"), "--out", str(out)])
    assert rc != 0
    err = capsys.readouterr().err
    assert "ujin:" in err


def test_eject_failed_job_exits_nonzero(tmp_path):
    plan = tmp_path / "jobs.yaml"
    out = tmp_path / "out"
    plan.write_text(
        "- id: bad\n"
        "  source:\n    kind: http\n    config:\n      url: https://b/\n"
        "  schedule:\n    mode: once\n"
        "  matrix: 42\n",
        encoding="utf-8",
    )
    rc = main(["plan", "eject", str(plan), "--out", str(out)])
    assert rc != 0
