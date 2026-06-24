"""Plan-loader coverage: include, defaults, matrix, env-var, job-id selector, plan-reload.

Targeted offline, deterministic tests for the external-config loader and
control-plane branches added in cycles 0.22-0.24. All tests are hermetic — no
network, no real clock/random dependence — using tmp_path and monkeypatch only.

Covers:
  ujin/jobs/app.py  — _expand_env, _report_warnings, _resolve_includes, _expand_matrix,
                      _select_jobs, _load_ingest_plan error paths
  ujin/cli.py       — `ujin plan validate` and `ujin plan render` with
                      --only / --exclude / --json / --strict
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

# ── write helper ──────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────────────────────
# _expand_env — direct unit tests
# ─────────────────────────────────────────────────────────────────────────────

from ujin.jobs.app import EnvVarRequiredError, _expand_env  # noqa: E402


def test_expand_env_plain_var_set(monkeypatch):
    monkeypatch.setenv("_UJIN_COV_SET", "hello")
    assert _expand_env("${_UJIN_COV_SET}") == "hello"


def test_expand_env_plain_var_unset_expands_to_empty(monkeypatch, caplog):
    import logging

    monkeypatch.delenv("_UJIN_COV_MISSING", raising=False)
    with caplog.at_level(logging.WARNING, logger="ujin.jobs.app"):
        result = _expand_env("${_UJIN_COV_MISSING}")
    assert result == ""
    assert any("_UJIN_COV_MISSING" in r.message for r in caplog.records)


def test_expand_env_default_var_set(monkeypatch):
    monkeypatch.setenv("_UJIN_COV_DEF", "actual")
    assert _expand_env("${_UJIN_COV_DEF:-fallback}") == "actual"


def test_expand_env_default_var_unset_returns_fallback(monkeypatch):
    monkeypatch.delenv("_UJIN_COV_DEF_UNSET", raising=False)
    assert _expand_env("${_UJIN_COV_DEF_UNSET:-fallback}") == "fallback"


def test_expand_env_required_var_set(monkeypatch):
    monkeypatch.setenv("_UJIN_COV_REQ", "value")
    assert _expand_env("${_UJIN_COV_REQ:?must be set}") == "value"


def test_expand_env_required_var_unset_raises(monkeypatch):
    monkeypatch.delenv("_UJIN_COV_REQ_UNSET", raising=False)
    with pytest.raises(EnvVarRequiredError, match="_UJIN_COV_REQ_UNSET"):
        _expand_env("${_UJIN_COV_REQ_UNSET:?provide the token}")


def test_expand_env_required_var_empty_raises(monkeypatch):
    monkeypatch.setenv("_UJIN_COV_REQ_EMPTY", "")
    with pytest.raises(EnvVarRequiredError, match="_UJIN_COV_REQ_EMPTY"):
        _expand_env("${_UJIN_COV_REQ_EMPTY:?cannot be empty}")


def test_expand_env_required_message_included_in_error(monkeypatch):
    monkeypatch.delenv("_UJIN_COV_MSG", raising=False)
    with pytest.raises(EnvVarRequiredError, match="the required api key"):
        _expand_env("${_UJIN_COV_MSG:?the required api key}")


def test_expand_env_no_op_on_plain_text():
    assert _expand_env("no variables here") == "no variables here"


# ─────────────────────────────────────────────────────────────────────────────
# _report_warnings — warnings=None branch (branch 392->390 in coverage)
# ─────────────────────────────────────────────────────────────────────────────

from ujin.jobs.app import _report_warnings  # noqa: E402


def test_report_warnings_with_none_only_logs(caplog):
    """_report_warnings(msgs, None) logs but does not crash — covers the
    `if warnings is not None:` False branch (no list to append to)."""
    import logging

    msgs = ["ujin: test.yaml: unrecognized key 'foo'"]
    with caplog.at_level(logging.WARNING, logger="ujin.jobs.app"):
        _report_warnings(msgs, None)
    assert any("foo" in r.message for r in caplog.records)


def test_report_warnings_appends_to_list():
    out: list = []
    _report_warnings(["msg1", "msg2"], out)
    assert out == ["msg1", "msg2"]


def test_report_warnings_empty_messages_is_noop(caplog):
    out: list = []
    _report_warnings([], out)
    assert out == []


# ─────────────────────────────────────────────────────────────────────────────
# include / use error paths
# ─────────────────────────────────────────────────────────────────────────────

from ujin.jobs.app import WorkflowIncludeError, _load_ingest_plan  # noqa: E402


def test_cyclic_include_direct_raises(tmp_path):
    """A file that includes itself must be caught as cyclic and reported in failed."""
    loop = tmp_path / "loop.yaml"
    loop.write_text(
        "include: loop.yaml\nid: x\nsource: {kind: command, config: {argv: [true]}}\n",
        encoding="utf-8",
    )
    failed: list = []
    specs = _load_ingest_plan(loop, failed=failed)
    assert specs == []
    assert failed
    assert "cyclic" in failed[0]["error"].lower()
    assert failed[0]["error"].startswith("ujin:")


def test_cyclic_include_indirect_raises(tmp_path):
    """A -> B -> A cycle is also detected and reported, not an infinite loop."""
    a = tmp_path / "a.yaml"
    b = tmp_path / "b.yaml"
    a.write_text(
        "include: b.yaml\nid: x\nsource: {kind: command, config: {argv: [true]}}\n",
        encoding="utf-8",
    )
    b.write_text(
        "include: a.yaml\nid: y\nsource: {kind: command, config: {argv: [true]}}\n",
        encoding="utf-8",
    )
    failed: list = []
    _load_ingest_plan(a, failed=failed)
    assert failed
    assert "cyclic" in failed[0]["error"].lower()


def test_include_invalid_type_raises(tmp_path):
    """`include: 123` (integer, not a string or list) raises WorkflowIncludeError."""
    f = tmp_path / "bad_include_type.yaml"
    f.write_text(
        "include: 123\nid: x\nsource: {kind: command, config: {argv: [true]}}\n",
        encoding="utf-8",
    )
    failed: list = []
    _load_ingest_plan(f, failed=failed)
    assert failed
    assert "fragment path or list" in failed[0]["error"].lower() or "include" in failed[0]["error"]
    assert failed[0]["error"].startswith("ujin:")


def test_include_list_fragment_with_extra_keys_errors(tmp_path):
    """An `include:` that resolves to a list cannot have sibling keys — errors cleanly."""
    frag = tmp_path / "list_frag.yaml"
    frag.write_text("- item_a\n- item_b\n", encoding="utf-8")
    main = tmp_path / "main.yaml"
    main.write_text(
        "include: list_frag.yaml\nextra_key: value\n",
        encoding="utf-8",
    )
    failed: list = []
    _load_ingest_plan(main, failed=failed)
    assert failed
    assert "cannot merge" in failed[0]["error"].lower()
    assert failed[0]["error"].startswith("ujin:")


# ─────────────────────────────────────────────────────────────────────────────
# _deep_merge + defaults precedence (per-job keys win)
# ─────────────────────────────────────────────────────────────────────────────

from ujin.jobs.app import _deep_merge  # noqa: E402


def test_deep_merge_over_wins_on_conflict():
    base = {"a": 1, "b": {"c": 2, "d": 3}}
    over = {"b": {"c": 99}, "e": 5}
    assert _deep_merge(base, over) == {"a": 1, "b": {"c": 99, "d": 3}, "e": 5}


def test_deep_merge_base_only_key_kept():
    assert _deep_merge({"x": 1}, {"y": 2}) == {"x": 1, "y": 2}


def test_deep_merge_list_in_over_replaces_base():
    assert _deep_merge({"a": [1, 2]}, {"a": [3]}) == {"a": [3]}


def test_deep_merge_scalar_over_replaces_dict_base():
    assert _deep_merge({"a": {"nested": True}}, {"a": "scalar"}) == {"a": "scalar"}


def test_defaults_per_job_keys_win(tmp_path):
    """Per-job keys override `defaults:` (defaults is deep-merged *under* each job)."""
    _write(tmp_path / "plan.yaml", {
        "defaults": {"schedule": {"mode": "adaptive", "base": 300}},
        "jobs": [
            {"id": "j1", "source": {"kind": "command", "config": {"argv": ["printf", "a"]}},
             "schedule": {"mode": "once"}},   # per-job mode wins
            {"id": "j2", "source": {"kind": "command", "config": {"argv": ["printf", "b"]}}},
        ],
    })
    specs = _load_ingest_plan(tmp_path / "plan.yaml")
    by_id = {s.id: s for s in specs}
    assert by_id["j1"].schedule.mode == "once"       # overrode defaults
    assert by_id["j2"].schedule.mode == "adaptive"   # inherited
    assert by_id["j2"].schedule.base == 300.0        # inherited


def test_defaults_source_deep_merged_under_job(tmp_path):
    """A `defaults:` source is merged field-by-field under the per-job source."""
    _write(tmp_path / "plan.yaml", {
        "defaults": {
            "source": {"kind": "api", "config": {"method": "GET", "json_path": "items"}}
        },
        "jobs": [
            {"id": "j", "source": {"config": {"url": "https://api.test/v1"}}},
        ],
    })
    (spec,) = _load_ingest_plan(tmp_path / "plan.yaml")
    # kind and method from defaults, url from per-job
    assert spec.source.kind == "api"
    assert spec.source.config["method"] == "GET"
    assert spec.source.config["url"] == "https://api.test/v1"


# ─────────────────────────────────────────────────────────────────────────────
# matrix / for_each fan-out error paths
# ─────────────────────────────────────────────────────────────────────────────

from ujin.jobs.app import _expand_matrix  # noqa: E402


def test_expand_matrix_both_keys_raises():
    """Having both `matrix:` and `for_each:` in one entry raises ValueError."""
    entry = {
        "id": "j",
        "matrix": [{"x": 1}],
        "for_each": [{"x": 2}],
        "source": {"kind": "command", "config": {}},
    }
    with pytest.raises(ValueError, match="use only one"):
        _expand_matrix(entry, "j")


def test_expand_matrix_non_list_rows_raises():
    """`matrix:` scalar (neither list nor mapping) raises ValueError naming both forms."""
    entry = {"id": "j", "matrix": "nope", "source": {"kind": "command", "config": {}}}
    with pytest.raises(ValueError, match="list"):
        _expand_matrix(entry, "j")


def test_expand_matrix_empty_list_raises():
    """`matrix: []` (empty list) raises ValueError."""
    entry = {"id": "j", "matrix": [], "source": {"kind": "command", "config": {}}}
    with pytest.raises(ValueError, match="non-empty list"):
        _expand_matrix(entry, "j")


def test_expand_matrix_non_dict_row_raises():
    """A matrix entry that is not a dict (e.g. an int) raises ValueError."""
    entry = {
        "id": "j-{{ n }}",
        "matrix": [{"n": "a"}, 42],   # second entry is an int, not a dict
        "source": {"kind": "command", "config": {}},
    }
    with pytest.raises(ValueError, match="variable map"):
        _expand_matrix(entry, "j")


def test_expand_matrix_duplicate_generated_id_raises():
    """Two matrix rows that produce the same expanded id raise ValueError."""
    entry = {
        "id": "job-{{ slug }}",
        "matrix": [{"slug": "same"}, {"slug": "same"}],
        "source": {"kind": "command", "config": {"argv": ["printf", "x"]}},
    }
    with pytest.raises(ValueError, match="duplicate id"):
        _expand_matrix(entry, "job")


def test_matrix_non_dict_row_fails_job_others_load(tmp_path):
    """A non-dict matrix row fails only that job; other valid jobs still load."""
    _write(tmp_path / "plan.yaml", [
        {"id": "good", "schedule": {"mode": "once"},
         "source": {"kind": "command", "config": {"argv": ["printf", "ok"]}}},
        {"id": "bad", "matrix": [{"x": "a"}, 99],
         "source": {"kind": "command", "config": {"argv": ["printf", "{{ x }}"]}}},
    ])
    failed: list = []
    specs = _load_ingest_plan(tmp_path / "plan.yaml", failed=failed)
    assert [s.id for s in specs] == ["good"]
    assert [f["id"] for f in failed] == ["bad"]
    assert "variable map" in failed[0]["error"]


def test_matrix_both_keys_fails_job_others_load(tmp_path):
    """Both matrix: and for_each: in one plan job fails only that entry."""
    _write(tmp_path / "plan.yaml", [
        {"id": "good", "schedule": {"mode": "once"},
         "source": {"kind": "command", "config": {"argv": ["printf", "ok"]}}},
        {"id": "bad",
         "matrix": [{"x": 1}], "for_each": [{"x": 2}],
         "source": {"kind": "command", "config": {"argv": ["printf", "x"]}}},
    ])
    failed: list = []
    specs = _load_ingest_plan(tmp_path / "plan.yaml", failed=failed)
    assert [s.id for s in specs] == ["good"]
    assert failed and "bad" in failed[0]["id"]


def test_for_each_alias_fans_out_identically(tmp_path):
    """`for_each:` is a synonym for `matrix:` and produces the same fan-out."""
    _write(tmp_path / "plan.yaml", [
        {
            "id": "feed-{{ env }}",
            "for_each": [{"env": "prod"}, {"env": "staging"}],
            "source": {"kind": "command", "config": {"argv": ["printf", "{{ env }}"]}},
            "schedule": {"mode": "once"},
        }
    ])
    specs = _load_ingest_plan(tmp_path / "plan.yaml")
    assert [s.id for s in specs] == ["feed-prod", "feed-staging"]


def test_matrix_duplicate_generated_id_in_plan_fails_job(tmp_path):
    """Two matrix rows producing the same id fail only that plan job."""
    _write(tmp_path / "plan.yaml", [
        {"id": "good", "schedule": {"mode": "once"},
         "source": {"kind": "command", "config": {"argv": ["printf", "ok"]}}},
        {"id": "dup-{{ slug }}",
         "matrix": [{"slug": "same"}, {"slug": "same"}],
         "source": {"kind": "command", "config": {"argv": ["printf", "x"]}}},
    ])
    failed: list = []
    specs = _load_ingest_plan(tmp_path / "plan.yaml", failed=failed)
    assert [s.id for s in specs] == ["good"]
    assert failed


# ─────────────────────────────────────────────────────────────────────────────
# _select_jobs — job-id selector edge cases (covers line 415 early-return)
# ─────────────────────────────────────────────────────────────────────────────

from ujin.jobs.app import _select_jobs  # noqa: E402
from ujin.jobs.model import JobSpec  # noqa: E402


def _make_specs(*ids):
    return [
        JobSpec.from_dict(
            {"id": jid, "name": jid,
             "source": {"kind": "command", "config": {"argv": ["true"]}}}
        )
        for jid in ids
    ]


def test_select_jobs_no_filter_returns_original_list():
    """No only/exclude -> original list returned unchanged (identity, not a copy)."""
    specs = _make_specs("a", "b", "c")
    assert _select_jobs(specs) is specs
    assert _select_jobs(specs, only=None, exclude=None) is specs


def test_select_jobs_empty_lists_treated_as_no_filter():
    """only=[] and exclude=[] are treated as no filter — covers the early return."""
    specs = _make_specs("a", "b")
    assert _select_jobs(specs, only=[], exclude=[]) is specs


def test_select_jobs_only_exact_match():
    specs = _make_specs("alpha", "beta", "gamma")
    result = _select_jobs(specs, only=["alpha", "gamma"])
    assert [s.id for s in result] == ["alpha", "gamma"]


def test_select_jobs_only_wildcard():
    specs = _make_specs("feed-tech", "feed-bio", "crawler")
    result = _select_jobs(specs, only=["feed-*"])
    assert [s.id for s in result] == ["feed-tech", "feed-bio"]


def test_select_jobs_only_no_match_raises():
    specs = _make_specs("a", "b")
    with pytest.raises(ValueError, match="no job ids matched"):
        _select_jobs(specs, only=["nomatch"])


def test_select_jobs_exclude_only_no_only():
    """exclude without only starts from the full list, then subtracts."""
    specs = _make_specs("a", "b", "c")
    result = _select_jobs(specs, exclude=["b"])
    assert [s.id for s in result] == ["a", "c"]


def test_select_jobs_only_then_exclude():
    specs = _make_specs("feed-tech", "feed-bio", "crawler")
    result = _select_jobs(specs, only=["feed-*"], exclude=["feed-bio"])
    assert [s.id for s in result] == ["feed-tech"]


def test_select_jobs_exclude_all_leaves_empty():
    specs = _make_specs("a", "b")
    result = _select_jobs(specs, exclude=["*"])
    assert result == []


def test_select_jobs_only_empty_spec_list_raises():
    """--only with no specs at all still raises the no-match error."""
    with pytest.raises(ValueError, match="no job ids matched"):
        _select_jobs([], only=["anything"])


# ─────────────────────────────────────────────────────────────────────────────
# CLI — ujin plan validate with --only / --exclude / --json / --strict
# ─────────────────────────────────────────────────────────────────────────────

from ujin.cli import main as cli_main  # noqa: E402


def _run_cli(argv):
    try:
        return cli_main(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1


def _two_job_plan(tmp_path) -> Path:
    plan = tmp_path / "plan.yaml"
    _write(plan, [
        {"id": "alpha", **_minimal_job()},
        {"id": "beta", **_minimal_job()},
    ])
    return plan


def test_cli_plan_validate_all_pass(tmp_path, capsys):
    rc = _run_cli(["plan", "validate", str(_two_job_plan(tmp_path))])
    out = capsys.readouterr().out
    assert rc == 0
    assert "alpha" in out and "beta" in out


def test_cli_plan_validate_only_filter(tmp_path, capsys):
    rc = _run_cli(["plan", "validate", str(_two_job_plan(tmp_path)), "--only", "alpha"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "alpha" in out
    assert "beta" not in out


def test_cli_plan_validate_exclude_filter(tmp_path, capsys):
    rc = _run_cli(["plan", "validate", str(_two_job_plan(tmp_path)), "--exclude", "beta"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "alpha" in out
    assert "beta" not in out


def test_cli_plan_validate_only_no_match_exits_1(tmp_path):
    rc = _run_cli(["plan", "validate", str(_two_job_plan(tmp_path)), "--only", "nomatch"])
    assert rc == 1


def test_cli_plan_validate_json_ok(tmp_path, capsys):
    import json

    rc = _run_cli(["plan", "validate", str(_two_job_plan(tmp_path)), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["ok"] is True
    assert set(data["resolved"]) == {"alpha", "beta"}
    assert data["failed"] == []


def test_cli_plan_validate_json_only_filter(tmp_path, capsys):
    import json

    rc = _run_cli(
        ["plan", "validate", str(_two_job_plan(tmp_path)), "--json", "--only", "alpha"]
    )
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["resolved"] == ["alpha"]


def test_cli_plan_validate_json_no_match_exits_1(tmp_path, capsys):
    import json

    rc = _run_cli(
        ["plan", "validate", str(_two_job_plan(tmp_path)), "--json", "--only", "nomatch"]
    )
    data = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert data["ok"] is False


def test_cli_plan_validate_strict_promotes_warnings(tmp_path, capsys):
    """--strict turns unrecognized-key warnings into failures (non-zero exit)."""
    plan = tmp_path / "plan.yaml"
    _write(plan, [{"id": "j", "defaluts": {}, **_minimal_job()}])  # typo key -> warning
    # Without --strict: advisory only, still exits 0
    rc_ok = _run_cli(["plan", "validate", str(plan)])
    capsys.readouterr()   # consume output
    # With --strict: exit 1 because warnings exist
    rc_fail = _run_cli(["plan", "validate", str(plan), "--strict"])
    assert rc_ok == 0
    assert rc_fail == 1


def test_cli_plan_validate_missing_path_exits_1(tmp_path, capsys):
    rc = _run_cli(["plan", "validate", str(tmp_path / "nonexistent.yaml")])
    assert rc == 1
    capsys.readouterr()


def test_cli_plan_validate_json_missing_path(tmp_path, capsys):
    import json

    rc = _run_cli(
        ["plan", "validate", str(tmp_path / "nonexistent.yaml"), "--json"]
    )
    data = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert data["ok"] is False


def test_cli_plan_validate_directory_of_workflows(tmp_path, capsys):
    """plan validate works on a workflows directory too."""
    wf = tmp_path / "wf"
    wf.mkdir()
    _write(wf / "alpha.yaml", _minimal_job(id="alpha", name="alpha"))
    rc = _run_cli(["plan", "validate", str(wf)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "alpha" in out


# ─────────────────────────────────────────────────────────────────────────────
# CLI — ujin plan render with --only / --exclude / --json
# ─────────────────────────────────────────────────────────────────────────────


def test_cli_plan_render_basic_yaml(tmp_path, capsys):
    rc = _run_cli(["plan", "render", str(_two_job_plan(tmp_path))])
    out = capsys.readouterr().out
    assert rc == 0
    assert "alpha" in out and "beta" in out


def test_cli_plan_render_json(tmp_path, capsys):
    import json

    rc = _run_cli(["plan", "render", str(_two_job_plan(tmp_path)), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert isinstance(data, list)
    ids = {d["id"] for d in data}
    assert ids == {"alpha", "beta"}


def test_cli_plan_render_only_filter(tmp_path, capsys):
    rc = _run_cli(["plan", "render", str(_two_job_plan(tmp_path)), "--only", "alpha"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "alpha" in out and "beta" not in out


def test_cli_plan_render_exclude_filter(tmp_path, capsys):
    rc = _run_cli(["plan", "render", str(_two_job_plan(tmp_path)), "--exclude", "beta"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "alpha" in out and "beta" not in out


def test_cli_plan_render_only_no_match_exits_1(tmp_path):
    rc = _run_cli(["plan", "render", str(_two_job_plan(tmp_path)), "--only", "nomatch"])
    assert rc == 1


def test_cli_plan_render_missing_path_exits_1(tmp_path, capsys):
    rc = _run_cli(["plan", "render", str(tmp_path / "nonexistent.yaml")])
    assert rc == 1
    capsys.readouterr()


def test_cli_plan_render_failed_job_exits_1(tmp_path, capsys):
    """A plan with a YAML-level failure causes render to exit non-zero."""
    bad = tmp_path / "bad.yaml"
    bad.write_text("[unclosed yaml\n", encoding="utf-8")  # invalid YAML
    rc = _run_cli(["plan", "render", str(bad)])
    assert rc == 1
    capsys.readouterr()


# ─────────────────────────────────────────────────────────────────────────────
# POST /plan/reload — add / update / remove (upsert-only) behavior
# ─────────────────────────────────────────────────────────────────────────────


def _tc():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    return TestClient


def test_reload_adds_new_job(tmp_path, monkeypatch):
    TestClient = _tc()
    from ujin.jobs.app import create_jobs_app

    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    plan = tmp_path / "plan.yaml"
    _write(plan, [{"id": "j1", **_minimal_job()}])

    with TestClient(create_jobs_app(run_engine=False, plan_path=str(plan))) as c:
        assert {j["id"] for j in c.get("/jobs").json()} == {"j1"}
        _write(plan, [{"id": "j1", **_minimal_job()}, {"id": "j2", **_minimal_job()}])
        body = c.post("/plan/reload").json()
        assert body["reloaded"] is True
        assert "j2" in body["loaded"]
        assert {j["id"] for j in c.get("/jobs").json()} >= {"j1", "j2"}


def test_reload_updates_existing_job_spec(tmp_path, monkeypatch):
    TestClient = _tc()
    from ujin.jobs.app import create_jobs_app

    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    plan = tmp_path / "plan.yaml"
    _write(plan, [{"id": "myjob", "schedule": {"mode": "once"},
                   "source": {"kind": "command", "config": {"argv": ["printf", "v1"]}}}])

    with TestClient(create_jobs_app(run_engine=False, plan_path=str(plan))) as c:
        orig = c.get("/jobs/myjob").json()["spec"]
        assert orig["source"]["config"]["argv"] == ["printf", "v1"]

        _write(plan, [{"id": "myjob", "schedule": {"mode": "once"},
                       "source": {"kind": "command", "config": {"argv": ["printf", "v2"]}}}])
        body = c.post("/plan/reload").json()
        assert body["reloaded"] is True and "myjob" in body["loaded"]

        updated = c.get("/jobs/myjob").json()["spec"]
        assert updated["source"]["config"]["argv"] == ["printf", "v2"]


def test_reload_does_not_remove_jobs_dropped_from_plan(tmp_path, monkeypatch):
    """Reload is upsert-only: jobs removed from the plan file are NOT deleted."""
    TestClient = _tc()
    from ujin.jobs.app import create_jobs_app

    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    plan = tmp_path / "plan.yaml"
    _write(plan, [{"id": "keep", **_minimal_job()}])

    with TestClient(create_jobs_app(run_engine=False, plan_path=str(plan))) as c:
        assert "keep" in {j["id"] for j in c.get("/jobs").json()}
        plan.write_text("jobs: []\n", encoding="utf-8")
        body = c.post("/plan/reload").json()
        assert body["reloaded"] is True and body["loaded"] == []
        # "keep" is still present — upsert semantics, not sync/delete
        assert "keep" in {j["id"] for j in c.get("/jobs").json()}


def test_reload_health_plan_block_refreshed(tmp_path, monkeypatch):
    """/health `plan` block is updated after a successful reload."""
    TestClient = _tc()
    from ujin.jobs.app import create_jobs_app

    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    plan = tmp_path / "plan.yaml"
    _write(plan, [{"id": "j1", **_minimal_job()}])

    with TestClient(create_jobs_app(run_engine=False, plan_path=str(plan))) as c:
        assert c.get("/health").json()["plan"]["loaded"] == ["j1"]
        _write(plan, [{"id": "j1", **_minimal_job()}, {"id": "j2", **_minimal_job()}])
        c.post("/plan/reload")
        assert sorted(c.get("/health").json()["plan"]["loaded"]) == ["j1", "j2"]
