"""File-level ``vars:`` defaults for workflow/plan files (offline, deterministic).

An optional top-level ``vars:`` mapping (variable-name -> default value) supplies
fallbacks for the ``{{ var }}`` substitution a file already uses: a placeholder no
matrix entry and no process-environment value sets resolves from these defaults
instead of being left verbatim. Precedence for a ``{{ NAME }}`` placeholder is
``matrix per-entry var > process env > vars: default``; the ``${VAR}`` /
``${VAR:-default}`` read-time env syntax is unrelated and unchanged.

These tests drive the pure loader helpers in :mod:`ujin.jobs.app` plus the
``ujin plan validate`` CLI — all offline, no network.
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


# ── vars resolves a placeholder the environment/matrix leave unset ──────────── #
def test_vars_resolves_unset_placeholder_workflow(tmp_path, monkeypatch):
    monkeypatch.delenv("region", raising=False)
    f = tmp_path / "feed.yaml"
    _write(f, {
        "vars": {"region": "us-east"},
        "source": {"kind": "http", "config": {"url": "https://x/{{ region }}"}},
        "schedule": {"mode": "once"},
    })
    (spec,) = _specs_from_workflow_file(f)
    assert spec.source.config["url"] == "https://x/us-east"


def test_vars_resolves_unset_placeholder_plan(tmp_path, monkeypatch):
    monkeypatch.delenv("region", raising=False)
    plan = tmp_path / "plan.yaml"
    _write(plan, {
        "vars": {"region": "us-east"},
        "jobs": [
            {"id": "status",
             "source": {"kind": "http", "config": {"url": "https://x/{{ region }}"}},
             "schedule": {"mode": "once"}},
        ],
    })
    (spec,) = _load_ingest_plan(plan)
    assert spec.source.config["url"] == "https://x/us-east"


def test_vars_whole_value_preserves_type(tmp_path, monkeypatch):
    # A string that is exactly one placeholder keeps the default's native type.
    monkeypatch.delenv("floor", raising=False)
    f = tmp_path / "typed.yaml"
    _write(f, {
        "vars": {"floor": 500},
        "source": {"kind": "api", "config": {"min_price": "{{ floor }}"}},
        "schedule": {"mode": "once"},
    })
    (spec,) = _specs_from_workflow_file(f)
    assert spec.source.config["min_price"] == 500


# ── precedence: the process environment overrides a vars: default ──────────── #
def test_env_overrides_vars_workflow(tmp_path, monkeypatch):
    monkeypatch.setenv("region", "eu-west")
    f = tmp_path / "feed.yaml"
    _write(f, {
        "vars": {"region": "us-east"},
        "source": {"kind": "http", "config": {"url": "https://x/{{ region }}"}},
        "schedule": {"mode": "once"},
    })
    (spec,) = _specs_from_workflow_file(f)
    assert spec.source.config["url"] == "https://x/eu-west"


def test_env_overrides_vars_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("region", "eu-west")
    plan = tmp_path / "plan.yaml"
    _write(plan, {
        "vars": {"region": "us-east"},
        "jobs": [
            {"id": "status",
             "source": {"kind": "http", "config": {"url": "https://x/{{ region }}"}},
             "schedule": {"mode": "once"}},
        ],
    })
    (spec,) = _load_ingest_plan(plan)
    assert spec.source.config["url"] == "https://x/eu-west"


# ── precedence: a matrix per-entry var overrides a vars: default ───────────── #
def test_matrix_entry_overrides_vars(tmp_path, monkeypatch):
    monkeypatch.delenv("region", raising=False)
    f = tmp_path / "feed.yaml"
    _write(f, {
        "vars": {"region": "us-east"},
        "id": "status-{{ region }}",
        # entry 0 overrides region; entry 1 leaves it to the vars: default
        "matrix": [{"region": "eu-west"}, {"slug": "x"}],
        "source": {"kind": "http", "config": {"url": "https://x/{{ region }}"}},
        "schedule": {"mode": "once"},
    })
    specs = _specs_from_workflow_file(f)
    urls = [s.source.config["url"] for s in specs]
    assert urls == ["https://x/eu-west", "https://x/us-east"]
    # the matrix-overridden entry takes its id from the per-entry value, the other
    # from the vars: default
    assert [s.id for s in specs] == ["status-eu-west", "status-us-east"]


def test_matrix_entry_overrides_env_overlaid_vars(tmp_path, monkeypatch):
    # Full precedence chain: matrix entry > env > vars: default. Env sets region but
    # the per-entry matrix var still wins for the entry that declares it.
    monkeypatch.setenv("region", "env-region")
    f = tmp_path / "feed.yaml"
    _write(f, {
        "vars": {"region": "vars-region"},
        "matrix": [{"region": "matrix-region"}, {"other": "z"}],
        "id": "j-{{ region }}",
        "source": {"kind": "http", "config": {"url": "https://x/{{ region }}"}},
        "schedule": {"mode": "once"},
    })
    specs = _specs_from_workflow_file(f)
    urls = [s.source.config["url"] for s in specs]
    # entry 0: matrix wins; entry 1: no matrix var, so env wins over the vars default
    assert urls == ["https://x/matrix-region", "https://x/env-region"]


# ── interaction with ${VAR:-default}: orthogonal layers compose ────────────── #
def test_interaction_with_env_default_form(tmp_path, monkeypatch):
    # The ${VAR:-default} read-time form is unrelated to vars: and unchanged: its
    # inline default still applies when the env var is unset, and a `{{ var }}` in the
    # same string resolves from vars: — the two layers compose.
    monkeypatch.delenv("HOST", raising=False)
    monkeypatch.delenv("slug", raising=False)
    f = tmp_path / "mix.yaml"
    f.write_text(
        "vars:\n"
        "  slug: news\n"
        "source:\n"
        "  kind: http\n"
        "  config:\n"
        '    url: "${HOST:-https://localhost}/{{ slug }}"\n'
        "schedule: {mode: once}\n",
        encoding="utf-8",
    )
    (spec,) = _specs_from_workflow_file(f)
    # ${HOST:-...} expanded at read time -> localhost; {{ slug }} from vars: -> news
    assert spec.source.config["url"] == "https://localhost/news"


def test_env_default_form_not_shadowed_by_vars(tmp_path, monkeypatch):
    # A vars: entry that happens to share a name with a ${VAR:-default} placeholder
    # does NOT feed the ${...} form — env substitution is text-level and runs first,
    # so the inline default is what applies when the env var is unset.
    monkeypatch.delenv("TOKEN", raising=False)
    f = tmp_path / "tok.yaml"
    f.write_text(
        "vars:\n"
        "  TOKEN: from-vars\n"
        "source:\n"
        "  kind: http\n"
        "  config:\n"
        "    url: https://x/\n"
        '    headers: {Authorization: "${TOKEN:-inline}"}\n'
        "schedule: {mode: once}\n",
        encoding="utf-8",
    )
    (spec,) = _specs_from_workflow_file(f)
    assert spec.source.config["headers"]["Authorization"] == "inline"


# ── strict additivity: no vars: key -> byte-for-byte identical specs ───────── #
def test_no_vars_key_is_byte_for_byte_identical(tmp_path):
    body = {
        "id": "feed-{{ slug }}",
        "matrix": [{"slug": "tech"}, {"slug": "bio"}],
        "source": {"kind": "http", "config": {"url": "https://x/{{ slug }}"}},
        "schedule": {"mode": "once"},
    }
    f = tmp_path / "feed.yaml"
    _write(f, body)
    before = [s.to_dict() for s in _specs_from_workflow_file(f)]
    # adding an EMPTY vars: must also leave the result unchanged
    f2 = tmp_path / "feed2.yaml"
    _write(f2, {**body, "vars": {}})
    after = [s.to_dict() for s in _specs_from_workflow_file(f2)]
    for a, b in zip(before, after):
        a.pop("created_at", None)
        b.pop("created_at", None)
    # ids differ by stem only; compare content sans id/name
    for d in (*before, *after):
        d.pop("id", None)
        d.pop("name", None)
    assert before == after


def test_unknown_placeholder_left_verbatim_with_vars(tmp_path, monkeypatch):
    # A `{{ x }}` neither declared in vars: nor set by env/matrix is still left
    # verbatim (permissive), matching matrix behaviour.
    monkeypatch.delenv("x", raising=False)
    f = tmp_path / "feed.yaml"
    _write(f, {
        "vars": {"region": "us"},
        "source": {"kind": "http", "config": {"url": "https://x/{{ x }}/{{ region }}"}},
        "schedule": {"mode": "once"},
    })
    (spec,) = _specs_from_workflow_file(f)
    assert spec.source.config["url"] == "https://x/{{ x }}/us"


# ── invalid (non-mapping) vars: -> clean error, no traceback ───────────────── #
def test_invalid_vars_workflow_raises_clean(tmp_path):
    f = tmp_path / "bad.yaml"
    _write(f, {"vars": ["a", "b"], **_minimal_job()})
    with pytest.raises(ValueError) as exc:
        _specs_from_workflow_file(f)
    assert "`vars:`" in str(exc.value)
    assert "mapping" in str(exc.value)


def test_invalid_vars_plan_routes_to_failed(tmp_path):
    plan = tmp_path / "bad-plan.yaml"
    _write(plan, {"vars": [1, 2], "jobs": [{"id": "j", **_minimal_job()}]})
    failed: list = []
    specs = _load_ingest_plan(plan, failed=failed)
    assert specs == []
    assert len(failed) == 1
    assert "ujin:" in failed[0]["error"]
    assert "`vars:`" in failed[0]["error"]
    assert "bad-plan.yaml" in failed[0]["error"]


def test_invalid_vars_plan_validate_cli_exit_nonzero(tmp_path, capsys):
    plan = tmp_path / "bad-vars.yaml"
    _write(plan, {"vars": "not-a-map", "jobs": [{"id": "j", **_minimal_job()}]})
    rc = main(["plan", "validate", str(plan)])
    out = capsys.readouterr().out
    assert rc != 0
    assert "bad-vars.yaml" in out
    assert "ujin:" in out
    assert "FAIL" in out


def test_invalid_vars_workflow_dir_validate_cli(tmp_path, capsys):
    wf = tmp_path / "bad-wf.yaml"
    _write(wf, {"vars": 42, **_minimal_job()})
    rc = main(["plan", "validate", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc != 0
    assert "bad-wf.yaml" in out
    assert "ujin:" in out


# ── a valid vars: block validates clean ────────────────────────────────────── #
def test_valid_vars_validates_zero(tmp_path):
    plan = tmp_path / "ok.yaml"
    _write(plan, {
        "vars": {"region": "us-east", "rows": 50},
        "jobs": [
            {"id": "feed",
             "source": {"kind": "http",
                        "config": {"url": "https://x/{{ region }}?rows={{ rows }}"}},
             "schedule": {"mode": "once"}},
        ],
    })
    rc = main(["plan", "validate", str(plan)])
    assert rc == 0
