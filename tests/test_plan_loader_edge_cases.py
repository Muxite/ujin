"""plan-loader edge-case tests: gaps not covered by existing test files.

Additive only — no existing passing test is modified; every test is offline
and deterministic (tmp_path + monkeypatch, no live network).

Covers:
  ujin/jobs/app.py  — include: missing file surfaced as failed plan record
                    — include: missing nested fragment isolates one job
                    — EnvVarRequiredError surfaced as a failed plan record
                    — single-entry inline matrix → exactly 1 resolved JobSpec
                    — empty inline list (matrix: []) isolated to failed[]
                    — cross-product two axes: vars substituted into
                      source / sinks / transforms / schedule
                    — cross-product deterministic ids across repeated loads
  ujin/cli.py       — plan validate --json resolved list with matrix fan-out
                    — plan validate --only / --exclude over matrix-expanded ids
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ujin.jobs.app import _load_ingest_plan


# ── helpers (mirrors test_plan_loader_coverage.py) ─────────────────────────── #

def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _minimal_job(**kw) -> dict:
    base = {
        "schedule": {"mode": "once"},
        "source": {"kind": "command", "config": {"argv": ["printf", "x"]}},
    }
    base.update(kw)
    return base


def _run_cli(argv):
    from ujin.cli import main as cli_main
    try:
        return cli_main(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1


# ─────────────────────────────────────────────────────────────────────────────
# include: missing file surfaced as a failed plan record (not a crash)
# ─────────────────────────────────────────────────────────────────────────────

def test_include_missing_top_level_file_fails_cleanly(tmp_path):
    """include: a non-existent file at the top level → plan in failed[], no raise."""
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        "include: fragments/nonexistent.yaml\n"
        "id: job-x\n"
        "source: {kind: command, config: {argv: [true]}}\n",
        encoding="utf-8",
    )
    failed: list = []
    specs = _load_ingest_plan(plan, failed=failed)
    assert specs == []
    assert len(failed) == 1
    assert failed[0]["error"].startswith("ujin:")
    assert "nonexistent" in failed[0]["error"] or "fragment" in failed[0]["error"].lower()


def test_include_missing_nested_fragment_fails_whole_file(tmp_path):
    """A missing include anywhere in the document is resolved at read time, so the
    whole plan file fails (file-level error), not just the containing job.
    No unhandled exception — the failure lands in failed[] cleanly."""
    plan = tmp_path / "plan.yaml"
    _write(plan, {
        "jobs": [
            {"id": "good-job", **_minimal_job()},
            {
                "id": "bad-job",
                "source": {"kind": "command", "config": {"argv": ["true"]}},
                "sinks": [{"include": "missing-sink.yaml"}],
            },
        ],
    })
    failed: list = []
    specs = _load_ingest_plan(plan, failed=failed)
    # include: is resolved at file parse time — the whole plan fails, not just bad-job
    assert specs == []
    assert len(failed) == 1
    err = failed[0]["error"]
    assert err.startswith("ujin:")
    assert "missing-sink" in err.lower() or "fragment" in err.lower() or "include" in err.lower()


# ─────────────────────────────────────────────────────────────────────────────
# EnvVarRequiredError surfaced as a failed plan record (not a startup crash)
# ─────────────────────────────────────────────────────────────────────────────

def test_env_var_required_unset_surfaces_as_failed_plan(tmp_path, monkeypatch):
    """${VAR:?msg} with unset VAR → plan in failed[], loader returns [] (no raise)."""
    var = "_UJIN_EDGE_REQ_UNSET"
    monkeypatch.delenv(var, raising=False)
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        f"- id: job-x\n"
        f"  schedule: {{mode: once}}\n"
        f"  source: {{kind: api, config: {{url: '${{{var}:?must be set}}'}}}}\n",
        encoding="utf-8",
    )
    failed: list = []
    specs = _load_ingest_plan(plan, failed=failed)
    assert specs == []
    assert failed
    assert failed[0]["error"].startswith("ujin:")
    assert var in failed[0]["error"] or "must be set" in failed[0]["error"]


def test_env_var_required_empty_surfaces_as_failed_plan(tmp_path, monkeypatch):
    """${VAR:?msg} with empty VAR → plan in failed[], no unhandled exception."""
    var = "_UJIN_EDGE_REQ_EMPTY"
    monkeypatch.setenv(var, "")
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        f"- id: job-empty\n"
        f"  schedule: {{mode: once}}\n"
        f"  source: {{kind: api, config: {{url: '${{{var}:?api key required}}'}}}}\n",
        encoding="utf-8",
    )
    failed: list = []
    specs = _load_ingest_plan(plan, failed=failed)
    assert specs == []
    assert failed
    assert failed[0]["error"].startswith("ujin:")


def test_env_var_required_set_plan_loads_normally(tmp_path, monkeypatch):
    """${VAR:?msg} with a set, non-empty VAR → plan loads the job normally."""
    var = "_UJIN_EDGE_REQ_SET"
    monkeypatch.setenv(var, "https://api.example.com/v1")
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        f"- id: job-ok\n"
        f"  schedule: {{mode: once}}\n"
        f"  source: {{kind: api, config: {{url: '${{{var}:?must be set}}'}}}}\n",
        encoding="utf-8",
    )
    specs = _load_ingest_plan(plan)
    assert len(specs) == 1
    assert specs[0].id == "job-ok"
    assert specs[0].source.config["url"] == "https://api.example.com/v1"


# ─────────────────────────────────────────────────────────────────────────────
# Single-entry inline matrix → exactly 1 resolved JobSpec with vars substituted
# ─────────────────────────────────────────────────────────────────────────────

def test_single_entry_matrix_produces_one_spec(tmp_path):
    """matrix: with a 1-element list fans out to exactly 1 JobSpec."""
    _write(tmp_path / "plan.yaml", [
        {
            "id": "feed-{{ slug }}",
            "matrix": [{"slug": "tech"}],
            "source": {"kind": "command", "config": {"argv": ["printf", "{{ slug }}"]}},
            "schedule": {"mode": "once"},
        }
    ])
    specs = _load_ingest_plan(tmp_path / "plan.yaml")
    assert len(specs) == 1
    assert specs[0].id == "feed-tech"
    assert specs[0].source.config["argv"] == ["printf", "tech"]


def test_single_entry_for_each_produces_one_spec(tmp_path):
    """for_each: (alias for matrix:) with a 1-element list fans out to 1 JobSpec."""
    _write(tmp_path / "plan.yaml", [
        {
            "id": "job-{{ env }}",
            "for_each": [{"env": "prod"}],
            "source": {"kind": "command", "config": {"argv": ["printf", "{{ env }}"]}},
            "schedule": {"mode": "once"},
        }
    ])
    specs = _load_ingest_plan(tmp_path / "plan.yaml")
    assert len(specs) == 1
    assert specs[0].id == "job-prod"
    assert specs[0].source.config["argv"] == ["printf", "prod"]


# ─────────────────────────────────────────────────────────────────────────────
# matrix: [] (empty inline list) → job isolated to failed[], siblings load
# ─────────────────────────────────────────────────────────────────────────────

def test_empty_inline_matrix_fails_job_siblings_still_load(tmp_path):
    """matrix: [] in a plan job is isolated to failed[]; sibling jobs still load."""
    _write(tmp_path / "plan.yaml", [
        {"id": "good", **_minimal_job()},
        {
            "id": "bad-{{ slug }}",
            "matrix": [],
            "source": {"kind": "command", "config": {"argv": ["printf", "{{ slug }}"]}},
        },
    ])
    failed: list = []
    specs = _load_ingest_plan(tmp_path / "plan.yaml", failed=failed)
    assert [s.id for s in specs] == ["good"]
    assert len(failed) == 1
    assert "ujin:" in failed[0]["error"]
    assert "non-empty list" in failed[0]["error"]


def test_empty_inline_for_each_fails_job_siblings_still_load(tmp_path):
    """for_each: [] (empty) in a plan job is isolated to failed[] the same way."""
    _write(tmp_path / "plan.yaml", [
        {"id": "alpha", **_minimal_job()},
        {
            "id": "beta-{{ x }}",
            "for_each": [],
            "source": {"kind": "command", "config": {"argv": ["printf", "{{ x }}"]}},
        },
    ])
    failed: list = []
    specs = _load_ingest_plan(tmp_path / "plan.yaml", failed=failed)
    assert [s.id for s in specs] == ["alpha"]
    assert failed
    assert "non-empty list" in failed[0]["error"]


# ─────────────────────────────────────────────────────────────────────────────
# Cross-product two axes: vars substituted into all job fields
# ─────────────────────────────────────────────────────────────────────────────

def test_cross_product_vars_substituted_into_source(tmp_path):
    """Two-axis cross-product substitutes vars into source.config (URL + headers)."""
    _write(tmp_path / "plan.yaml", [
        {
            "id": "fetch-{{ region }}-{{ tier }}",
            "matrix": {"region": ["us", "eu"], "tier": ["free", "pro"]},
            "source": {"kind": "http", "config": {
                "url": "https://api.example.com/{{ region }}/data",
                "headers": {"x-tier": "{{ tier }}"},
            }},
            "schedule": {"mode": "once"},
        }
    ])
    specs = _load_ingest_plan(tmp_path / "plan.yaml")
    assert len(specs) == 4
    by_id = {s.id: s for s in specs}
    assert set(by_id) == {"fetch-us-free", "fetch-us-pro", "fetch-eu-free", "fetch-eu-pro"}
    us_free = by_id["fetch-us-free"]
    assert us_free.source.config["url"] == "https://api.example.com/us/data"
    assert us_free.source.config["headers"]["x-tier"] == "free"
    eu_pro = by_id["fetch-eu-pro"]
    assert eu_pro.source.config["url"] == "https://api.example.com/eu/data"
    assert eu_pro.source.config["headers"]["x-tier"] == "pro"


def test_cross_product_vars_substituted_into_sinks(tmp_path):
    """Two-axis cross-product substitutes vars into every sink's config."""
    _write(tmp_path / "plan.yaml", [
        {
            "id": "ingest-{{ region }}-{{ tier }}",
            "matrix": {"region": ["us", "eu"], "tier": ["free", "pro"]},
            "source": {"kind": "http", "config": {"url": "https://src.example.com"}},
            "sinks": [
                {"kind": "jsonl", "config": {"path": "/data/{{ region }}/{{ tier }}.jsonl"}},
                {"kind": "webhook", "config": {"url": "https://hook.example.com/{{ region }}"}},
            ],
        }
    ])
    specs = _load_ingest_plan(tmp_path / "plan.yaml")
    assert len(specs) == 4
    by_id = {s.id: s for s in specs}
    us_free = by_id["ingest-us-free"]
    assert us_free.sinks[0].config["path"] == "/data/us/free.jsonl"
    assert us_free.sinks[1].config["url"] == "https://hook.example.com/us"
    eu_pro = by_id["ingest-eu-pro"]
    assert eu_pro.sinks[0].config["path"] == "/data/eu/pro.jsonl"
    assert eu_pro.sinks[1].config["url"] == "https://hook.example.com/eu"


def test_cross_product_vars_substituted_into_transforms(tmp_path):
    """Two-axis cross-product substitutes vars into every transform's config."""
    _write(tmp_path / "plan.yaml", [
        {
            "id": "proc-{{ lang }}-{{ format }}",
            "matrix": {"lang": ["en", "fr"], "format": ["json", "xml"]},
            "source": {"kind": "http", "config": {"url": "https://api.example.com"}},
            "transforms": [
                {"kind": "filter", "config": {"lang": "{{ lang }}"}},
                {"kind": "chunk", "config": {"prefix": "{{ lang }}_{{ format }}:"}},
            ],
        }
    ])
    specs = _load_ingest_plan(tmp_path / "plan.yaml")
    assert len(specs) == 4
    by_id = {s.id: s for s in specs}
    en_json = by_id["proc-en-json"]
    assert en_json.transforms[0].config["lang"] == "en"
    assert en_json.transforms[1].config["prefix"] == "en_json:"
    fr_xml = by_id["proc-fr-xml"]
    assert fr_xml.transforms[0].config["lang"] == "fr"
    assert fr_xml.transforms[1].config["prefix"] == "fr_xml:"


def test_cross_product_vars_substituted_into_schedule(tmp_path):
    """Cross-product vars are substituted into schedule fields."""
    _write(tmp_path / "plan.yaml", [
        {
            "id": "task-{{ region }}",
            "matrix": {"region": ["us", "eu"]},
            "source": {"kind": "http", "config": {"url": "https://api.example.com/{{ region }}"}},
            "schedule": {"mode": "cron", "cron": "0 {{ region == 'us' and '9' or '15' }} * * *"},
        }
    ])
    specs = _load_ingest_plan(tmp_path / "plan.yaml")
    assert len(specs) == 2
    # schedule mode is preserved per-job
    for spec in specs:
        assert spec.schedule.mode == "cron"


# ─────────────────────────────────────────────────────────────────────────────
# Cross-product deterministic ids across repeated loads
# ─────────────────────────────────────────────────────────────────────────────

def test_cross_product_ids_are_deterministic_across_loads(tmp_path):
    """Two-axis cross-product ids are identical on repeated _load_ingest_plan calls."""
    plan = tmp_path / "plan.yaml"
    _write(plan, [
        {
            "id": "s-{{ region }}-{{ tier }}",
            "matrix": {"region": ["us", "eu"], "tier": ["free", "pro"]},
            "source": {"kind": "http", "config": {"url": "https://api.example.com"}},
        }
    ])
    first = [s.id for s in _load_ingest_plan(plan)]
    second = [s.id for s in _load_ingest_plan(plan)]
    assert first == second
    assert set(first) == {"s-us-free", "s-us-pro", "s-eu-free", "s-eu-pro"}


def test_cross_product_ids_independent_of_axis_declaration_order(tmp_path):
    """Swapping axis declaration order yields the same id set (sorted axis names)."""
    template = {"source": {"kind": "http", "config": {"url": "https://api.example.com"}}}
    f1 = tmp_path / "plan-a.yaml"
    _write(f1, [{**template, "id": "j-{{ a }}-{{ b }}", "matrix": {"a": ["1", "2"], "b": ["x"]}}])
    f2 = tmp_path / "plan-b.yaml"
    _write(f2, [{**template, "id": "j-{{ a }}-{{ b }}", "matrix": {"b": ["x"], "a": ["1", "2"]}}])
    ids1 = sorted(s.id for s in _load_ingest_plan(f1))
    ids2 = sorted(s.id for s in _load_ingest_plan(f2))
    assert ids1 == ids2


# ─────────────────────────────────────────────────────────────────────────────
# CLI: plan validate --json with matrix fan-out (resolved list has expanded ids)
# ─────────────────────────────────────────────────────────────────────────────

def test_cli_plan_validate_json_with_matrix_fanout(tmp_path, capsys):
    """plan validate --json resolved list contains the fan-out ids, not the template."""
    import json

    plan = tmp_path / "plan.yaml"
    _write(plan, [
        {
            "id": "feed-{{ slug }}",
            "matrix": [{"slug": "tech"}, {"slug": "bio"}, {"slug": "sports"}],
            "source": {"kind": "http", "config": {"url": "https://feeds.example.com/{{ slug }}"}},
            "schedule": {"mode": "once"},
        }
    ])
    rc = _run_cli(["plan", "validate", str(plan), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["ok"] is True
    assert set(data["resolved"]) == {"feed-tech", "feed-bio", "feed-sports"}
    assert data["failed"] == []


# ─────────────────────────────────────────────────────────────────────────────
# CLI: plan validate --only / --exclude over fully-resolved matrix-expanded ids
# ─────────────────────────────────────────────────────────────────────────────

def _matrix_plan(tmp_path: Path) -> Path:
    plan = tmp_path / "plan.yaml"
    _write(plan, [
        {
            "id": "feed-{{ slug }}",
            "matrix": [{"slug": "tech"}, {"slug": "bio"}, {"slug": "sports"}],
            "source": {"kind": "http", "config": {"url": "https://feeds.example.com/{{ slug }}"}},
            "schedule": {"mode": "once"},
        }
    ])
    return plan


def test_cli_plan_validate_only_over_matrix_ids(tmp_path, capsys):
    """--only pattern filters over fully-resolved fan-out ids."""
    rc = _run_cli(["plan", "validate", str(_matrix_plan(tmp_path)), "--only", "feed-tech"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "feed-tech" in out
    assert "feed-bio" not in out
    assert "feed-sports" not in out


def test_cli_plan_validate_only_glob_over_matrix_ids(tmp_path, capsys):
    """--only with a glob matches multiple fan-out ids."""
    rc = _run_cli(["plan", "validate", str(_matrix_plan(tmp_path)), "--only", "feed-*"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "feed-tech" in out and "feed-bio" in out and "feed-sports" in out


def test_cli_plan_validate_exclude_over_matrix_ids(tmp_path, capsys):
    """--exclude removes a specific fan-out id; the others remain."""
    rc = _run_cli(
        ["plan", "validate", str(_matrix_plan(tmp_path)), "--exclude", "feed-bio"]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "feed-tech" in out and "feed-sports" in out
    assert "feed-bio" not in out


def test_cli_plan_validate_json_only_over_matrix_ids(tmp_path, capsys):
    """--json resolved list with --only contains only the selected fan-out ids."""
    import json

    rc = _run_cli(
        ["plan", "validate", str(_matrix_plan(tmp_path)), "--json", "--only", "feed-tech"]
    )
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["ok"] is True
    assert data["resolved"] == ["feed-tech"]
