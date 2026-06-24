"""Config-loader hardening: edge-case and malformed-input tests.

Covers gaps left by pre-existing suites and exercises the additive code fixes
in ujin/jobs/app.py:

  - matrix:/for_each: with a null (None) value → clear per-job error in failed[]
  - matrix:/for_each: as an empty cross-product mapping ({}) → clear per-job error
  - Non-mapping entries in a plan jobs list → clear error naming file and index
    (new ValueError replaces the cryptic TypeError that dict() raised before)
  - Non-mapping entries in a workflow jobs: list → clear file-level error
  - Three-level deeply-nested defaults: per-job leaf keys win at every depth
  - vars: declared as a list (not a mapping) → file-level error in failed[]
  - ${VAR:-} with an empty default operand expands to "" (additive regression)
  - Cross-product id template that doesn't vary → duplicate id error, sibling loads
  - Full additive regression: valid plan with defaults+include+matrix+vars loads
    to the same specs before and after the code changes

All tests are offline and deterministic (tmp_path / monkeypatch, no live network).
"""
from __future__ import annotations

import pytest
import yaml
from pathlib import Path

pytest.importorskip("yaml")

from ujin.jobs.app import (  # noqa: E402
    _load_ingest_plan,
    _load_workflows_dir,
    _specs_from_workflow_file,
)


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


# ── matrix:/for_each: null value ─────────────────────────────────────────── #


def test_matrix_null_fails_job_sibling_loads(tmp_path):
    """matrix: null (YAML null → Python None) fails the job; sibling still loads."""
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        "- id: good\n"
        "  schedule: {mode: once}\n"
        "  source: {kind: command, config: {argv: [printf, ok]}}\n"
        "- id: bad\n"
        "  matrix: null\n"
        "  source: {kind: command, config: {argv: [printf, x]}}\n",
        encoding="utf-8",
    )
    failed: list = []
    specs = _load_ingest_plan(plan, failed=failed)
    assert [s.id for s in specs] == ["good"]
    assert len(failed) == 1
    assert failed[0]["id"] == "bad"
    assert failed[0]["error"].startswith("ujin:")
    # error names the key and location
    assert "matrix" in failed[0]["error"].lower() or "plan.yaml" in failed[0]["error"]


def test_for_each_null_fails_job_sibling_loads(tmp_path):
    """for_each: null fails the job with a clear message; sibling loads."""
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        "- id: ok\n"
        "  schedule: {mode: once}\n"
        "  source: {kind: command, config: {argv: [printf, ok]}}\n"
        "- id: bad\n"
        "  for_each: null\n"
        "  source: {kind: command, config: {argv: [printf, x]}}\n",
        encoding="utf-8",
    )
    failed: list = []
    specs = _load_ingest_plan(plan, failed=failed)
    assert [s.id for s in specs] == ["ok"]
    assert len(failed) == 1
    assert failed[0]["error"].startswith("ujin:")


# ── matrix:{} empty cross-product ────────────────────────────────────────── #


def test_matrix_empty_dict_fails_job_sibling_loads(tmp_path):
    """matrix: {} (no axes) fails the job with an 'axis' message; sibling loads."""
    _write(tmp_path / "plan.yaml", [
        {"id": "good", **_minimal_job()},
        {"id": "no-axes", "matrix": {}, **_minimal_job()},
    ])
    failed: list = []
    specs = _load_ingest_plan(tmp_path / "plan.yaml", failed=failed)
    assert [s.id for s in specs] == ["good"]
    assert len(failed) == 1
    assert failed[0]["id"] == "no-axes"
    assert failed[0]["error"].startswith("ujin:")
    assert "axis" in failed[0]["error"].lower()


def test_for_each_empty_dict_fails_job_sibling_loads(tmp_path):
    """for_each: {} (no axes) fails the job with the same 'axis' message."""
    _write(tmp_path / "plan.yaml", [
        {"id": "ok", **_minimal_job()},
        {"id": "empty-axes", "for_each": {}, **_minimal_job()},
    ])
    failed: list = []
    specs = _load_ingest_plan(tmp_path / "plan.yaml", failed=failed)
    assert [s.id for s in specs] == ["ok"]
    assert len(failed) == 1
    assert failed[0]["error"].startswith("ujin:")
    assert "axis" in failed[0]["error"].lower()


# ── non-dict entry in plan jobs list ─────────────────────────────────────── #


def test_plan_integer_entry_gives_clear_error(tmp_path):
    """An integer list entry in a plan is rejected with a clear message.

    Before the fix: dict(42) → TypeError: 'int' object is not iterable.
    After the fix: ValueError naming file and entry index, caught per-job.
    """
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        "- id: good\n"
        "  schedule: {mode: once}\n"
        "  source: {kind: command, config: {argv: [printf, ok]}}\n"
        "- 42\n",
        encoding="utf-8",
    )
    failed: list = []
    specs = _load_ingest_plan(plan, failed=failed)
    assert [s.id for s in specs] == ["good"]
    assert len(failed) == 1
    assert failed[0]["error"].startswith("ujin:")
    # error names the file and says 'mapping'
    assert "plan.yaml" in failed[0]["error"]
    assert "mapping" in failed[0]["error"].lower()


def test_plan_string_entry_gives_clear_error(tmp_path):
    """A bare string in the jobs list is rejected with a clear message."""
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        "- just-a-string\n"
        "- id: good\n"
        "  schedule: {mode: once}\n"
        "  source: {kind: command, config: {argv: [printf, ok]}}\n",
        encoding="utf-8",
    )
    failed: list = []
    specs = _load_ingest_plan(plan, failed=failed)
    assert [s.id for s in specs] == ["good"]
    assert len(failed) == 1
    assert failed[0]["error"].startswith("ujin:")
    assert "plan.yaml" in failed[0]["error"]
    assert "mapping" in failed[0]["error"].lower()


def test_plan_all_nondict_entries_all_fail(tmp_path):
    """Every non-dict entry fails independently; loader returns an empty list."""
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        "- 1\n"
        "- two\n"
        "- 3.0\n",
        encoding="utf-8",
    )
    failed: list = []
    specs = _load_ingest_plan(plan, failed=failed)
    assert specs == []
    assert len(failed) == 3
    assert all(f["error"].startswith("ujin:") for f in failed)


# ── non-dict entry in workflow jobs: list ─────────────────────────────────── #


def test_workflow_integer_entry_raises_clear_error(tmp_path):
    """An integer in a workflow jobs: list raises ValueError naming file + index.

    Before the fix: dict(99) → TypeError; after: clear ValueError.
    """
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "jobs:\n"
        "  - id: ok\n"
        "    source: {kind: command, config: {argv: [printf, ok]}}\n"
        "  - 99\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="mapping"):
        _specs_from_workflow_file(bad)


def test_workflow_nondict_entry_surfaces_in_failed_dir_loader(tmp_path):
    """Via _load_workflows_dir the error surfaces in failed[] without aborting siblings."""
    wf = tmp_path / "wf"
    _write(wf / "good.yaml", {"source": {"kind": "http", "config": {"url": "https://ok/"}}})
    (wf / "bad.yaml").write_text(
        "jobs:\n"
        "  - 99\n",
        encoding="utf-8",
    )
    failed: list = []
    specs = _load_workflows_dir(str(wf), failed=failed)
    assert [s.id for s in specs] == ["good"]
    assert len(failed) == 1
    assert failed[0]["id"] == "bad"
    assert failed[0]["error"].startswith("ujin:")
    assert "mapping" in failed[0]["error"].lower()


# ── 3-level deeply-nested defaults: per-job keys win at every depth ────────── #


def test_defaults_three_level_deep_merge_per_job_wins(tmp_path):
    """A per-job key at the 3rd nesting level overrides the defaults value there
    while keeping all sibling keys at that level and every ancestor level intact."""
    _write(tmp_path / "plan.yaml", {
        "defaults": {
            "source": {
                "kind": "http",
                "config": {
                    "auth": {
                        "type": "bearer",
                        "token": "base-token",
                        "retry": True,
                    },
                    "timeout": 30,
                },
            },
            "schedule": {"mode": "adaptive", "base": 300},
        },
        "jobs": [
            {
                "id": "a",
                # Only the 3rd-level `token` key is overridden.
                "source": {"config": {"auth": {"token": "per-job-token"}}},
            },
            {
                "id": "b",
                # Nothing overridden: every default inherited as-is.
            },
        ],
    })
    specs = _load_ingest_plan(tmp_path / "plan.yaml")
    by_id = {s.id: s for s in specs}

    # job a: token replaced at level 3; sibling keys and ancestors from defaults
    assert by_id["a"].source.kind == "http"
    assert by_id["a"].source.config["timeout"] == 30
    auth_a = by_id["a"].source.config["auth"]
    assert auth_a["token"] == "per-job-token"   # per-job wins
    assert auth_a["type"] == "bearer"            # sibling kept from defaults
    assert auth_a["retry"] is True              # sibling kept from defaults
    assert by_id["a"].schedule.base == 300.0

    # job b: everything from defaults
    auth_b = by_id["b"].source.config["auth"]
    assert auth_b["token"] == "base-token"
    assert by_id["b"].source.config["timeout"] == 30


def test_defaults_four_level_deep_merge(tmp_path):
    """Even at 4 nesting levels per-job keys win; ancestor + sibling keys stay."""
    _write(tmp_path / "plan.yaml", {
        "defaults": {
            "source": {
                "kind": "api",
                "config": {
                    "credentials": {
                        "oauth": {
                            "client_id": "base-client",
                            "scopes": ["read"],
                        },
                    },
                },
            },
        },
        "jobs": [
            {
                "id": "j",
                "source": {
                    "config": {
                        "credentials": {
                            "oauth": {
                                "client_id": "override-client",
                            },
                        },
                    },
                },
            },
        ],
    })
    (spec,) = _load_ingest_plan(tmp_path / "plan.yaml")
    oauth = spec.source.config["credentials"]["oauth"]
    assert oauth["client_id"] == "override-client"  # per-job wins at level 4
    assert oauth["scopes"] == ["read"]              # sibling kept from defaults


# ── vars: as list in plan ────────────────────────────────────────────────── #


def test_plan_vars_as_list_fails_whole_plan(tmp_path):
    """`vars:` declared as a list (not a mapping) fails the whole plan cleanly."""
    _write(tmp_path / "plan.yaml", {
        "vars": ["a", "b"],
        "jobs": [_minimal_job(id="j")],
    })
    failed: list = []
    specs = _load_ingest_plan(tmp_path / "plan.yaml", failed=failed)
    assert specs == []
    assert len(failed) == 1
    assert failed[0]["error"].startswith("ujin:")
    assert "vars" in failed[0]["error"].lower()
    assert "plan.yaml" in failed[0]["error"]


def test_plan_vars_as_scalar_fails_whole_plan(tmp_path):
    """`vars:` declared as a scalar integer also fails the whole plan cleanly."""
    _write(tmp_path / "plan.yaml", {
        "vars": 42,
        "jobs": [_minimal_job(id="j")],
    })
    failed: list = []
    specs = _load_ingest_plan(tmp_path / "plan.yaml", failed=failed)
    assert specs == []
    assert len(failed) == 1
    assert failed[0]["error"].startswith("ujin:")
    assert "vars" in failed[0]["error"].lower()


# ── ${VAR:-} with empty default operand ──────────────────────────────────── #


def test_env_expand_empty_default_unset(tmp_path, monkeypatch):
    """${VAR:-} (empty default operand) expands to '' when the variable is unset."""
    monkeypatch.delenv("_UJIN_HARDEN_EMPTYDEF", raising=False)
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        "- id: j\n"
        "  schedule: {mode: once}\n"
        '  source: {kind: command, config: {argv: [printf, "${_UJIN_HARDEN_EMPTYDEF:-}"]}}\n',
        encoding="utf-8",
    )
    specs = _load_ingest_plan(plan)
    assert len(specs) == 1
    assert specs[0].source.config["argv"][-1] == ""


def test_env_expand_empty_default_set_returns_value(tmp_path, monkeypatch):
    """${VAR:-} returns the var's actual value when the variable is set."""
    monkeypatch.setenv("_UJIN_HARDEN_EMPTYDEF", "real-value")
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        "- id: j\n"
        "  schedule: {mode: once}\n"
        '  source: {kind: command, config: {argv: [printf, "${_UJIN_HARDEN_EMPTYDEF:-}"]}}\n',
        encoding="utf-8",
    )
    specs = _load_ingest_plan(plan)
    assert specs[0].source.config["argv"][-1] == "real-value"


# ── cross-product duplicate ids from fan-out ──────────────────────────────── #


def test_cross_product_static_id_collides_sibling_loads(tmp_path):
    """A static (non-templated) id: for a cross-product collides on the 2nd row.

    The per-job error names the offending id and the file; the sibling job loads.
    """
    _write(tmp_path / "plan.yaml", [
        {"id": "good", **_minimal_job()},
        {
            "id": "same-for-all",  # not parameterised → every row produces same id
            "matrix": {"region": ["us", "eu"]},
            "source": {"kind": "http", "config": {"url": "https://api.example.com/{{ region }}"}},
        },
    ])
    failed: list = []
    specs = _load_ingest_plan(tmp_path / "plan.yaml", failed=failed)
    assert [s.id for s in specs] == ["good"]
    assert len(failed) == 1
    assert failed[0]["id"] == "same-for-all"
    assert "duplicate id" in failed[0]["error"]
    assert failed[0]["error"].startswith("ujin:")
    assert "plan.yaml" in failed[0]["error"]


def test_inline_list_duplicate_id_template_collides(tmp_path):
    """An inline-list matrix whose id template doesn't vary produces duplicate ids."""
    _write(tmp_path / "plan.yaml", [
        {"id": "good", **_minimal_job()},
        {
            "id": "fixed",  # no {{ var }} → same id from both rows
            "matrix": [{"n": "a"}, {"n": "b"}],
            "source": {"kind": "http", "config": {"url": "https://api.example.com/{{ n }}"}},
        },
    ])
    failed: list = []
    specs = _load_ingest_plan(tmp_path / "plan.yaml", failed=failed)
    assert [s.id for s in specs] == ["good"]
    assert len(failed) == 1
    assert "duplicate id" in failed[0]["error"]
    assert failed[0]["error"].startswith("ujin:")


# ── additive regression: valid plans unchanged ───────────────────────────── #


def test_valid_plan_all_features_unchanged(tmp_path, monkeypatch):
    """A valid plan with defaults + include + matrix + vars loads identically."""
    monkeypatch.setenv("_UJIN_HARDEN_BASE_URL", "https://feeds.example.com")
    frag = tmp_path / "fragments" / "sink.yaml"
    _write(frag, {"kind": "jsonl", "config": {"path": "/data/out.jsonl"}})
    _write(tmp_path / "plan.yaml", {
        "vars": {"suffix": "v1"},
        "defaults": {
            "schedule": {"mode": "once"},
            "source": {
                "kind": "http",
                "config": {"url": "${_UJIN_HARDEN_BASE_URL}/data"},
            },
            "sinks": [{"include": "fragments/sink.yaml"}],
        },
        "jobs": [
            {
                "id": "feed-{{ slug }}",
                "matrix": [{"slug": "tech"}, {"slug": "bio"}],
                "source": {"config": {"url": "${_UJIN_HARDEN_BASE_URL}/{{ slug }}"}},
            },
        ],
    })
    specs = _load_ingest_plan(tmp_path / "plan.yaml")
    assert [s.id for s in specs] == ["feed-tech", "feed-bio"]
    assert specs[0].source.config["url"] == "https://feeds.example.com/tech"
    assert specs[1].source.config["url"] == "https://feeds.example.com/bio"
    assert specs[0].sinks[0].kind == "jsonl"
    assert specs[0].schedule.mode == "once"
