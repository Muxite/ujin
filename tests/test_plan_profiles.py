"""Profile overlays for INGEST-PLAN/workflow files (offline, deterministic).

A top-level ``profiles:`` mapping declares named overlays; selecting one with
``--profile NAME`` (or ``$UJIN_PLAN_PROFILE``) deep-merges it OVER the plan's base
``defaults:`` before the per-job merge — so an overlaid key reaches every job that
does not set it itself, while per-job keys, matrix fan-out, and ``${VAR}``/``{{ var
}}`` substitution behave exactly as before. With no profile selected (and even when a
``profiles:`` block is present) resolution is byte-for-byte unchanged.

These tests exercise the pure loader helpers in :mod:`ujin.jobs.app` and the three
CLI surfaces (``plan validate``/``plan render``/``jobs-serve``) directly. All offline.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from ujin.cli import _resolve_profile, main
from ujin.jobs.app import (
    PlanProfileError,
    _check_plan_profile,
    _load_ingest_plan,
    _load_workflows_dir,
    _profile_overlay,
    _specs_from_workflow_file,
)


# ── helpers ─────────────────────────────────────────────────────────────────── #

def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _effective(spec) -> dict:
    """A spec's content with the volatile/identity fields stripped."""
    d = spec.to_dict()
    for k in ("id", "name", "created_at"):
        d.pop(k, None)
    return d


def _job(url: str) -> dict:
    return {"source": {"kind": "http", "config": {"url": url}}}


def _plan_with_profiles() -> dict:
    """A plan whose base defaults set base=60; a `fast` profile overlays base=5.

    Job ``a`` inherits the schedule; job ``b`` sets its own base=9 so the per-job
    key must keep winning over both the base default and any selected profile.
    """
    return {
        "defaults": {
            "schedule": {"mode": "poll", "base": 60},
            "transforms": [{"kind": "noop"}],
        },
        "profiles": {
            "fast": {"schedule": {"base": 5}},
            "slow": {"schedule": {"base": 600}, "transforms": [{"kind": "lower"}]},
        },
        "jobs": [
            {"id": "a", **_job("https://a/")},
            {"id": "b", "schedule": {"mode": "poll", "base": 9}, **_job("https://b/")},
        ],
    }


def _base(spec) -> float:
    return spec.schedule.base


# ── no-profile parity: presence of `profiles:` changes nothing ──────────────── #

def test_no_profile_selected_is_byte_for_byte_unchanged(tmp_path):
    """A plan with a `profiles:` block, with no profile selected, resolves exactly
    like the same plan stripped of `profiles:`."""
    with_profiles = _plan_with_profiles()
    without = {k: v for k, v in with_profiles.items() if k != "profiles"}

    _write(tmp_path / "withp.yaml", with_profiles)
    _write(tmp_path / "without.yaml", without)

    specs_with = _load_ingest_plan(tmp_path / "withp.yaml")
    specs_without = _load_ingest_plan(tmp_path / "without.yaml")

    assert [s.id for s in specs_with] == [s.id for s in specs_without]
    assert [_effective(s) for s in specs_with] == [_effective(s) for s in specs_without]
    # base default reaches job a; job b keeps its own.
    by_id = {s.id: s for s in specs_with}
    assert _base(by_id["a"]) == 60 and _base(by_id["b"]) == 9


def test_no_profile_no_failures_no_warnings(tmp_path):
    _write(tmp_path / "p.yaml", _plan_with_profiles())
    failed: list = []
    warnings: list = []
    specs = _load_ingest_plan(tmp_path / "p.yaml", failed=failed, warnings=warnings)
    assert failed == [] and warnings == []
    assert len(specs) == 2


# ── selected-profile overlay applied + per-job key still wins ───────────────── #

def test_selected_profile_overlays_base_defaults(tmp_path):
    _write(tmp_path / "p.yaml", _plan_with_profiles())
    specs = _load_ingest_plan(tmp_path / "p.yaml", profile="fast")
    by_id = {s.id: s for s in specs}
    # `fast` overlays base=5 onto the defaults, so job a (no own base) inherits it...
    assert _base(by_id["a"]) == 5
    # ...but job b sets its own base=9, which still wins over the profile overlay.
    assert _base(by_id["b"]) == 9


def test_profile_can_add_and_replace_default_keys(tmp_path):
    _write(tmp_path / "p.yaml", _plan_with_profiles())
    specs = _load_ingest_plan(tmp_path / "p.yaml", profile="slow")
    by_id = {s.id: s for s in specs}
    assert _base(by_id["a"]) == 600
    # `slow` replaces the transforms list wholesale (lists replace, not concat).
    assert [t.kind for t in by_id["a"].transforms] == ["lower"]


def test_profile_overlay_preserves_var_and_matrix(tmp_path):
    """A profile that overlays a `{{ var }}`-bearing default still substitutes vars
    and matrix entries, with the established precedence (matrix > env > vars)."""
    _write(tmp_path / "p.yaml", {
        "vars": {"path": "default"},
        "defaults": {"schedule": {"mode": "poll", "base": 60}},
        "profiles": {"q": {"source": {"kind": "http",
                                      "config": {"url": "https://h/{{ path }}"}}}},
        "jobs": [
            {"id": "m-{{ path }}", "matrix": {"path": ["x", "y"]}},
        ],
    })
    specs = _load_ingest_plan(tmp_path / "p.yaml", profile="q")
    urls = sorted(s.source.config["url"] for s in specs)
    assert urls == ["https://h/x", "https://h/y"]


# ── unknown profile: clean failure, no traceback, no jobs ───────────────────── #

def test_unknown_profile_fails_whole_plan(tmp_path):
    _write(tmp_path / "p.yaml", _plan_with_profiles())
    failed: list = []
    specs = _load_ingest_plan(tmp_path / "p.yaml", failed=failed, profile="nope")
    assert specs == []
    assert len(failed) == 1
    msg = failed[0]["error"]
    assert msg.startswith("ujin: ")
    assert "profile 'nope' not found" in msg
    # the available names are listed to make the message actionable
    assert "fast" in msg and "slow" in msg


def test_selected_profile_without_profiles_block_fails(tmp_path):
    _write(tmp_path / "p.yaml", {
        "defaults": {"schedule": {"mode": "once"}},
        "jobs": [{"id": "a", **_job("https://a/")}],
    })
    failed: list = []
    specs = _load_ingest_plan(tmp_path / "p.yaml", failed=failed, profile="fast")
    assert specs == []
    assert len(failed) == 1
    assert "no `profiles:` block" in failed[0]["error"]


# ── _profile_overlay unit coverage ──────────────────────────────────────────── #

def test_profile_overlay_no_profile_is_noop():
    assert _profile_overlay({"profiles": {"a": {"x": 1}}}, None, "p.yaml") == {}


def test_profile_overlay_returns_selected_mapping():
    data = {"profiles": {"a": {"x": 1}, "b": {"y": 2}}}
    assert _profile_overlay(data, "b", "p.yaml") == {"y": 2}


def test_profile_overlay_none_overlay_is_empty():
    assert _profile_overlay({"profiles": {"a": None}}, "a", "p.yaml") == {}


def test_profile_overlay_unknown_lists_available():
    with pytest.raises(PlanProfileError) as exc:
        _profile_overlay({"profiles": {"a": {}, "b": {}}}, "z", "p.yaml")
    assert "available profiles: a, b" in str(exc.value)


def test_profile_overlay_non_mapping_profiles_block():
    with pytest.raises(PlanProfileError) as exc:
        _profile_overlay({"profiles": ["a", "b"]}, "a", "p.yaml")
    assert "must be a mapping" in str(exc.value)


def test_profile_overlay_non_mapping_overlay():
    with pytest.raises(PlanProfileError) as exc:
        _profile_overlay({"profiles": {"a": [1, 2]}}, "a", "p.yaml")
    assert "must be a mapping" in str(exc.value)


def test_profile_overlay_lenient_missing_block_is_noop():
    # workflow-file mode: a file with no `profiles:` key is left unchanged.
    assert _profile_overlay({"jobs": []}, "fast", "wf.yaml", strict=False) == {}


def test_profile_overlay_strict_missing_block_raises():
    with pytest.raises(PlanProfileError):
        _profile_overlay({"jobs": []}, "fast", "p.yaml", strict=True)


# ── _check_plan_profile (jobs-serve pre-flight) ─────────────────────────────── #

def test_check_plan_profile_ok(tmp_path):
    p = tmp_path / "p.yaml"
    _write(p, _plan_with_profiles())
    # a valid profile is a no-op (no raise)
    _check_plan_profile(str(p), "fast")


def test_check_plan_profile_bad_raises(tmp_path):
    p = tmp_path / "p.yaml"
    _write(p, _plan_with_profiles())
    with pytest.raises(PlanProfileError):
        _check_plan_profile(str(p), "nope")


def test_check_plan_profile_unparseable_is_noop(tmp_path):
    p = tmp_path / "broken.yaml"
    p.write_text("::: not yaml :::\n  - [", encoding="utf-8")
    # a file-level parse error is reported at load time, not here.
    _check_plan_profile(str(p), "fast")


def test_check_plan_profile_noop_when_unset(tmp_path):
    _check_plan_profile(None, "fast")
    _check_plan_profile("missing.yaml", None)


# ── workflow directory: lenient overlay ─────────────────────────────────────── #

def test_workflow_dir_profile_lenient(tmp_path):
    """A `--profile` over a workflows dir overlays files that declare it and leaves
    files with no `profiles:` key unchanged (only a present-but-unknown name fails)."""
    _write(tmp_path / "has.yaml", {
        "defaults": {"schedule": {"mode": "poll", "base": 60}},
        "profiles": {"fast": {"schedule": {"base": 5}}},
        "jobs": [{**_job("https://a/")}],
    })
    _write(tmp_path / "plain.yaml", {
        "schedule": {"mode": "poll", "base": 30}, **_job("https://b/"),
    })
    failed: list = []
    specs = _load_workflows_dir(str(tmp_path), failed=failed, profile="fast")
    assert failed == []
    by_id = {s.id: s for s in specs}
    assert _base(by_id["has"]) == 5      # overlay applied
    assert _base(by_id["plain"]) == 30   # untouched (no profiles: key)


def test_workflow_file_present_profile_unknown_name_fails(tmp_path):
    wf = tmp_path / "has.yaml"
    _write(wf, {
        "defaults": {"schedule": {"mode": "poll", "base": 60}},
        "profiles": {"fast": {"schedule": {"base": 5}}},
        "jobs": [{**_job("https://a/")}],
    })
    with pytest.raises(PlanProfileError):
        _specs_from_workflow_file(wf, profile="nope")


# ── _resolve_profile (CLI flag-over-env precedence) ─────────────────────────── #

def test_resolve_profile_flag_wins(monkeypatch):
    monkeypatch.setenv("UJIN_PLAN_PROFILE", "fromenv")
    assert _resolve_profile("fromflag") == "fromflag"


def test_resolve_profile_env_fallback(monkeypatch):
    monkeypatch.setenv("UJIN_PLAN_PROFILE", "fromenv")
    assert _resolve_profile(None) == "fromenv"


def test_resolve_profile_none(monkeypatch):
    monkeypatch.delenv("UJIN_PLAN_PROFILE", raising=False)
    assert _resolve_profile(None) is None


# ── CLI: plan render / validate / jobs-serve ────────────────────────────────── #

def _render_bases(capsys, *argv) -> dict:
    rc = main(["plan", "render", *argv, "--json"])
    out = capsys.readouterr().out
    assert rc == 0, out
    return {j["id"]: j["schedule"]["base"] for j in json.loads(out)}


def test_render_no_profile_baseline(tmp_path, capsys):
    p = tmp_path / "p.yaml"
    _write(p, _plan_with_profiles())
    assert _render_bases(capsys, str(p)) == {"a": 60, "b": 9}


def test_render_with_profile_flag(tmp_path, capsys):
    p = tmp_path / "p.yaml"
    _write(p, _plan_with_profiles())
    assert _render_bases(capsys, str(p), "--profile", "fast") == {"a": 5, "b": 9}


def test_render_env_fallback(tmp_path, capsys, monkeypatch):
    p = tmp_path / "p.yaml"
    _write(p, _plan_with_profiles())
    monkeypatch.setenv("UJIN_PLAN_PROFILE", "slow")
    assert _render_bases(capsys, str(p)) == {"a": 600, "b": 9}


def test_render_flag_beats_env(tmp_path, capsys, monkeypatch):
    p = tmp_path / "p.yaml"
    _write(p, _plan_with_profiles())
    monkeypatch.setenv("UJIN_PLAN_PROFILE", "slow")
    assert _render_bases(capsys, str(p), "--profile", "fast") == {"a": 5, "b": 9}


def test_validate_with_valid_profile_exits_zero(tmp_path, capsys):
    p = tmp_path / "p.yaml"
    _write(p, _plan_with_profiles())
    assert main(["plan", "validate", str(p), "--profile", "fast"]) == 0


def test_validate_unknown_profile_exits_nonzero(tmp_path, capsys):
    p = tmp_path / "p.yaml"
    _write(p, _plan_with_profiles())
    rc = main(["plan", "validate", str(p), "--profile", "nope"])
    out = capsys.readouterr().out + capsys.readouterr().err
    assert rc == 1
    assert "profile 'nope' not found" in out


def test_render_unknown_profile_exits_nonzero(tmp_path, capsys):
    p = tmp_path / "p.yaml"
    _write(p, _plan_with_profiles())
    rc = main(["plan", "render", str(p), "--profile", "nope"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "profile 'nope' not found" in err


def test_jobs_serve_bad_profile_preflight_exits_nonzero(tmp_path, capsys):
    """jobs-serve rejects an unknown --profile BEFORE binding a port (no uvicorn)."""
    p = tmp_path / "p.yaml"
    _write(p, _plan_with_profiles())
    rc = main(["jobs-serve", "--plan", str(p), "--profile", "nope"])
    err = capsys.readouterr().err
    assert rc == 1
    assert err.startswith("ujin: ") and "profile 'nope' not found" in err


# ── shipped example guards drift (examples/profiles.yaml) ───────────────────── #

_EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "profiles.yaml"


def test_example_validates_clean(capsys):
    assert main(["plan", "validate", str(_EXAMPLE)]) == 0
    assert "WARN" not in capsys.readouterr().out


@pytest.mark.parametrize("profile,home", [(None, 60), ("fast", 5), ("slow", 600)])
def test_example_renders_under_each_profile(capsys, profile, home):
    argv = ["plan", "render", str(_EXAMPLE), "--json"]
    if profile:
        argv += ["--profile", profile]
    assert main(argv) == 0
    bases = {j["id"]: j["schedule"]["base"] for j in json.loads(capsys.readouterr().out)}
    assert bases == {"home": home, "status": 9}
