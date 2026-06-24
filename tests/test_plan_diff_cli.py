"""ujin plan diff — offline, deterministic tests.

Covers: added/removed/changed jobs between two resolved plans, an identical copy
reporting no diff, --json output with distinct added/removed/changed groupings,
workflows-directory inputs, --env-file ${VAR} expansion, and error inputs
(missing path + schema-invalid file) exiting non-zero with a clean ``ujin: …``
message and no traceback. All tests use tmp_path and the real loaders; no
network.
"""
from __future__ import annotations

import json
from pathlib import Path

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


def _plan(tmp_path: Path, name: str, jobs: list) -> Path:
    p = tmp_path / name
    _write(p, jobs)
    return p


# ── added / removed / changed (human form) ──────────────────────────────────── #

def test_diff_reports_added_removed_changed(tmp_path, capsys):
    old = _plan(tmp_path, "old.yaml", [
        {"id": "alpha", **_minimal_job("https://a/")},
        {"id": "beta", **_minimal_job("https://b/")},
    ])
    new = _plan(tmp_path, "new.yaml", [
        {"id": "beta", **_minimal_job("https://b-changed/")},
        {"id": "gamma", **_minimal_job("https://c/")},
    ])
    rc = main(["plan", "diff", str(old), str(new)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "+ gamma" in out   # added
    assert "- alpha" in out   # removed
    assert "~ beta" in out    # changed


def test_diff_added_only(tmp_path, capsys):
    old = _plan(tmp_path, "old.yaml", [{"id": "alpha", **_minimal_job()}])
    new = _plan(tmp_path, "new.yaml", [
        {"id": "alpha", **_minimal_job()},
        {"id": "beta", **_minimal_job("https://b/")},
    ])
    rc = main(["plan", "diff", str(old), str(new)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "+ beta" in out
    assert "- " not in out
    assert "~ " not in out


def test_diff_removed_only(tmp_path, capsys):
    old = _plan(tmp_path, "old.yaml", [
        {"id": "alpha", **_minimal_job()},
        {"id": "beta", **_minimal_job("https://b/")},
    ])
    new = _plan(tmp_path, "new.yaml", [{"id": "alpha", **_minimal_job()}])
    rc = main(["plan", "diff", str(old), str(new)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "- beta" in out
    assert "+ " not in out


def test_diff_changed_only(tmp_path, capsys):
    old = _plan(tmp_path, "old.yaml", [{"id": "alpha", **_minimal_job("https://a/")}])
    new = _plan(tmp_path, "new.yaml", [{"id": "alpha", **_minimal_job("https://a2/")}])
    rc = main(["plan", "diff", str(old), str(new)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "~ alpha" in out


# ── identical copy → no diff ─────────────────────────────────────────────────── #

def test_diff_identical_reports_no_changes(tmp_path, capsys):
    plan = _plan(tmp_path, "plan.yaml", [
        {"id": "alpha", **_minimal_job("https://a/")},
        {"id": "beta", **_minimal_job("https://b/")},
    ])
    rc = main(["plan", "diff", str(plan), str(plan)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no changes" in out
    assert "+ " not in out
    assert "- " not in out
    assert "~ " not in out


def test_diff_identical_copy_reports_no_changes(tmp_path, capsys):
    # Two distinct files with identical content must still diff empty — the
    # per-load created_at must not leak into the comparison.
    old = _plan(tmp_path, "old.yaml", [{"id": "alpha", **_minimal_job("https://a/")}])
    new = _plan(tmp_path, "new.yaml", [{"id": "alpha", **_minimal_job("https://a/")}])
    rc = main(["plan", "diff", str(old), str(new)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no changes" in out


def test_diff_identical_json_all_groups_empty(tmp_path, capsys):
    plan = _plan(tmp_path, "plan.yaml", [{"id": "alpha", **_minimal_job()}])
    rc = main(["plan", "diff", str(plan), str(plan), "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data == {"added": [], "removed": [], "changed": []}


# ── --json output ────────────────────────────────────────────────────────────── #

def test_diff_json_distinct_groupings(tmp_path, capsys):
    old = _plan(tmp_path, "old.yaml", [
        {"id": "alpha", **_minimal_job("https://a/")},
        {"id": "beta", **_minimal_job("https://b/")},
    ])
    new = _plan(tmp_path, "new.yaml", [
        {"id": "beta", **_minimal_job("https://b-changed/")},
        {"id": "gamma", **_minimal_job("https://c/")},
    ])
    rc = main(["plan", "diff", str(old), str(new), "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert {"added", "removed", "changed"} == set(data)
    assert [e["id"] for e in data["added"]] == ["gamma"]
    assert [e["id"] for e in data["removed"]] == ["alpha"]
    assert [e["id"] for e in data["changed"]] == ["beta"]


def test_diff_json_added_carries_spec(tmp_path, capsys):
    old = _plan(tmp_path, "old.yaml", [{"id": "alpha", **_minimal_job()}])
    new = _plan(tmp_path, "new.yaml", [
        {"id": "alpha", **_minimal_job()},
        {"id": "beta", **_minimal_job("https://b/")},
    ])
    main(["plan", "diff", str(old), str(new), "--json"])
    data = json.loads(capsys.readouterr().out)
    spec = data["added"][0]["spec"]
    assert spec["id"] == "beta"
    assert spec["source"]["config"]["url"] == "https://b/"


def test_diff_json_changed_carries_old_and_new(tmp_path, capsys):
    old = _plan(tmp_path, "old.yaml", [{"id": "alpha", **_minimal_job("https://a/")}])
    new = _plan(tmp_path, "new.yaml", [{"id": "alpha", **_minimal_job("https://a2/")}])
    main(["plan", "diff", str(old), str(new), "--json"])
    entry = json.loads(capsys.readouterr().out)["changed"][0]
    assert entry["old"]["source"]["config"]["url"] == "https://a/"
    assert entry["new"]["source"]["config"]["url"] == "https://a2/"


def test_diff_json_specs_omit_created_at(tmp_path, capsys):
    old = _plan(tmp_path, "old.yaml", [{"id": "alpha", **_minimal_job()}])
    new = _plan(tmp_path, "new.yaml", [
        {"id": "alpha", **_minimal_job()},
        {"id": "beta", **_minimal_job("https://b/")},
    ])
    main(["plan", "diff", str(old), str(new), "--json"])
    spec = json.loads(capsys.readouterr().out)["added"][0]["spec"]
    assert "created_at" not in spec


# ── workflows directory inputs ───────────────────────────────────────────────── #

def test_diff_workflows_dirs(tmp_path, capsys):
    old_dir = tmp_path / "old"
    new_dir = tmp_path / "new"
    _write(old_dir / "job-a.yaml", _minimal_job("https://a/"))
    _write(old_dir / "job-b.yaml", _minimal_job("https://b/"))
    _write(new_dir / "job-a.yaml", _minimal_job("https://a/"))      # unchanged
    _write(new_dir / "job-c.yaml", _minimal_job("https://c/"))      # added
    rc = main(["plan", "diff", str(old_dir), str(new_dir), "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert [e["id"] for e in data["added"]] == ["job-c"]
    assert [e["id"] for e in data["removed"]] == ["job-b"]
    assert data["changed"] == []


# ── matrix fan-out is fully expanded before diffing ──────────────────────────── #

def test_diff_expands_matrix(tmp_path, capsys):
    old = tmp_path / "old.yaml"
    _write(old, {"jobs": [{
        "id": "feed-{{ slug }}",
        "matrix": [{"slug": "tech"}, {"slug": "science"}],
        **_minimal_job("https://feeds/{{ slug }}"),
    }]})
    new = tmp_path / "new.yaml"
    _write(new, {"jobs": [{
        "id": "feed-{{ slug }}",
        "matrix": [{"slug": "tech"}, {"slug": "business"}],
        **_minimal_job("https://feeds/{{ slug }}"),
    }]})
    main(["plan", "diff", str(old), str(new), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert [e["id"] for e in data["added"]] == ["feed-business"]
    assert [e["id"] for e in data["removed"]] == ["feed-science"]


# ── --env-file consistency ───────────────────────────────────────────────────── #

def test_diff_env_file_expands_vars(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("DIFF_HOST", raising=False)
    env = tmp_path / "vars.env"
    env.write_text("DIFF_HOST=example.org\n", encoding="utf-8")
    old = _plan(tmp_path, "old.yaml", [
        {"id": "alpha", **_minimal_job("https://${DIFF_HOST}/old")}])
    new = _plan(tmp_path, "new.yaml", [
        {"id": "alpha", **_minimal_job("https://${DIFF_HOST}/new")}])
    main(["plan", "diff", str(old), str(new), "--json", "--env-file", str(env)])
    entry = json.loads(capsys.readouterr().out)["changed"][0]
    assert entry["old"]["source"]["config"]["url"] == "https://example.org/old"
    assert entry["new"]["source"]["config"]["url"] == "https://example.org/new"


def test_diff_missing_env_file_exits_nonzero(tmp_path, capsys):
    old = _plan(tmp_path, "old.yaml", [{"id": "alpha", **_minimal_job()}])
    new = _plan(tmp_path, "new.yaml", [{"id": "alpha", **_minimal_job()}])
    rc = main(["plan", "diff", str(old), str(new),
               "--env-file", str(tmp_path / "nope.env")])
    assert rc != 0
    assert "ujin:" in capsys.readouterr().err


# ── error inputs: missing / schema-invalid ───────────────────────────────────── #

def test_diff_missing_old_exits_nonzero(tmp_path, capsys):
    new = _plan(tmp_path, "new.yaml", [{"id": "alpha", **_minimal_job()}])
    rc = main(["plan", "diff", str(tmp_path / "nope.yaml"), str(new)])
    assert rc != 0
    err = capsys.readouterr().err
    assert "ujin:" in err
    assert "nope.yaml" in err
    assert "Traceback" not in err


def test_diff_missing_new_exits_nonzero(tmp_path, capsys):
    old = _plan(tmp_path, "old.yaml", [{"id": "alpha", **_minimal_job()}])
    rc = main(["plan", "diff", str(old), str(tmp_path / "gone.yaml")])
    assert rc != 0
    err = capsys.readouterr().err
    assert "ujin:" in err
    assert "Traceback" not in err


def test_diff_invalid_schema_exits_nonzero_no_traceback(tmp_path, capsys):
    old = _plan(tmp_path, "old.yaml", [{"id": "alpha", **_minimal_job()}])
    bad = tmp_path / "bad.yaml"
    _write(bad, [{"id": "bad", "matrix": "not-a-list", **_minimal_job()}])
    rc = main(["plan", "diff", str(old), str(bad)])
    assert rc != 0
    err = capsys.readouterr().err
    assert "ujin:" in err
    assert "Traceback" not in err
    assert "Exception" not in err


def test_diff_invalid_old_schema_exits_nonzero(tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    _write(bad, [{"id": "bad", "matrix": "oops", **_minimal_job()}])
    new = _plan(tmp_path, "new.yaml", [{"id": "alpha", **_minimal_job()}])
    rc = main(["plan", "diff", str(bad), str(new)])
    assert rc != 0
    assert "Traceback" not in capsys.readouterr().err
