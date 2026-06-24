"""Job-id selection: --only and --exclude for plan render, plan validate, jobs-serve.

Covers all required cases per the plan-job-selection spec:
- only by exact id          (render + validate)
- only/exclude by glob      (render + validate)
- exclude subset            (render + validate)
- no-match --only error     (render + validate + jobs-serve pre-flight)
- comma-separated patterns  (render)
- repeatable patterns       (render)
- regression: no flags → output identical to unfiltered output (render)
- jobs-serve: --only/--exclude filters plan.loaded in /health (create_jobs_app)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

from ujin.cli import main


# ── shared helpers ───────────────────────────────────────────────────────────── #

def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _minimal_job(url: str = "https://example.com/") -> dict:
    return {
        "source": {"kind": "http", "config": {"url": url}},
        "schedule": {"mode": "once"},
    }


def _plan_3(tmp_path: Path) -> Path:
    """Write a 3-job plan: alpha, beta, gamma."""
    plan = tmp_path / "plan.yaml"
    _write(plan, [
        {"id": "alpha", **_minimal_job("https://a/")},
        {"id": "beta",  **_minimal_job("https://b/")},
        {"id": "gamma", **_minimal_job("https://c/")},
    ])
    return plan


def _matrix_plan(tmp_path: Path) -> Path:
    """Write a matrix plan that fans into feed-tech, feed-science, feed-business."""
    plan = tmp_path / "feeds.yaml"
    _write(plan, {
        "jobs": [{
            "id": "feed-{{ slug }}",
            "matrix": [{"slug": "tech"}, {"slug": "science"}, {"slug": "business"}],
            **_minimal_job("https://feeds.example.com/{{ slug }}"),
        }]
    })
    return plan


# ══════════════════════════════════════════════════════════════════════════════ #
#  plan render: --only by exact id
# ══════════════════════════════════════════════════════════════════════════════ #

def test_render_only_exact_exits_zero(tmp_path, capsys):
    plan = _plan_3(tmp_path)
    rc = main(["plan", "render", str(plan), "--only", "alpha"])
    assert rc == 0


def test_render_only_exact_shows_matching_job(tmp_path, capsys):
    plan = _plan_3(tmp_path)
    main(["plan", "render", str(plan), "--only", "alpha"])
    out = capsys.readouterr().out
    assert "alpha" in out
    assert "beta" not in out
    assert "gamma" not in out


def test_render_only_exact_json_length_one(tmp_path, capsys):
    plan = _plan_3(tmp_path)
    main(["plan", "render", str(plan), "--only", "alpha", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 1
    assert data[0]["id"] == "alpha"


# ══════════════════════════════════════════════════════════════════════════════ #
#  plan render: --only by glob
# ══════════════════════════════════════════════════════════════════════════════ #

def test_render_only_glob_matches_all_matrix_jobs(tmp_path, capsys):
    plan = _matrix_plan(tmp_path)
    main(["plan", "render", str(plan), "--only", "feed-*", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 3
    assert {d["id"] for d in data} == {"feed-tech", "feed-science", "feed-business"}


def test_render_only_glob_matches_subset(tmp_path, capsys):
    plan = _plan_3(tmp_path)
    main(["plan", "render", str(plan), "--only", "a*", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 1
    assert data[0]["id"] == "alpha"


# ══════════════════════════════════════════════════════════════════════════════ #
#  plan render: --exclude
# ══════════════════════════════════════════════════════════════════════════════ #

def test_render_exclude_removes_exact_job(tmp_path, capsys):
    plan = _plan_3(tmp_path)
    main(["plan", "render", str(plan), "--exclude", "beta", "--json"])
    data = json.loads(capsys.readouterr().out)
    ids = {d["id"] for d in data}
    assert "beta" not in ids
    assert "alpha" in ids
    assert "gamma" in ids


def test_render_exclude_by_glob(tmp_path, capsys):
    plan = _matrix_plan(tmp_path)
    main(["plan", "render", str(plan), "--exclude", "feed-t*", "--json"])
    data = json.loads(capsys.readouterr().out)
    ids = {d["id"] for d in data}
    assert "feed-tech" not in ids
    assert "feed-science" in ids
    assert "feed-business" in ids


# ══════════════════════════════════════════════════════════════════════════════ #
#  plan render: --only + --exclude (exclude subtracts from --only set)
# ══════════════════════════════════════════════════════════════════════════════ #

def test_render_only_and_exclude_combined(tmp_path, capsys):
    plan = _matrix_plan(tmp_path)
    main(["plan", "render", str(plan),
          "--only", "feed-*", "--exclude", "feed-tech", "--json"])
    data = json.loads(capsys.readouterr().out)
    ids = {d["id"] for d in data}
    assert "feed-tech" not in ids
    assert "feed-science" in ids
    assert "feed-business" in ids


# ══════════════════════════════════════════════════════════════════════════════ #
#  plan render: comma-separated and repeatable patterns
# ══════════════════════════════════════════════════════════════════════════════ #

def test_render_only_comma_separated(tmp_path, capsys):
    plan = _plan_3(tmp_path)
    main(["plan", "render", str(plan), "--only", "alpha,beta", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert {d["id"] for d in data} == {"alpha", "beta"}


def test_render_only_repeatable_flags(tmp_path, capsys):
    plan = _plan_3(tmp_path)
    main(["plan", "render", str(plan),
          "--only", "alpha", "--only", "beta", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert {d["id"] for d in data} == {"alpha", "beta"}


def test_render_exclude_comma_separated(tmp_path, capsys):
    plan = _plan_3(tmp_path)
    main(["plan", "render", str(plan), "--exclude", "alpha,beta", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert {d["id"] for d in data} == {"gamma"}


# ══════════════════════════════════════════════════════════════════════════════ #
#  plan render: no-match --only error
# ══════════════════════════════════════════════════════════════════════════════ #

def test_render_only_no_match_exits_nonzero(tmp_path, capsys):
    plan = _plan_3(tmp_path)
    rc = main(["plan", "render", str(plan), "--only", "no-such-job"])
    assert rc != 0


def test_render_only_no_match_clean_ujin_message(tmp_path, capsys):
    plan = _plan_3(tmp_path)
    main(["plan", "render", str(plan), "--only", "no-such-job"])
    err = capsys.readouterr().err
    assert "ujin:" in err
    assert "no-such-job" in err


def test_render_only_no_match_no_traceback(tmp_path, capsys):
    plan = _plan_3(tmp_path)
    main(["plan", "render", str(plan), "--only", "no-such-job"])
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "Exception" not in err


# ══════════════════════════════════════════════════════════════════════════════ #
#  plan render: regression — no flags → identical to unfiltered output
# ══════════════════════════════════════════════════════════════════════════════ #

def test_render_no_flags_is_unfiltered(tmp_path, capsys):
    """With no --only/--exclude the output is identical to before this change."""
    plan = _plan_3(tmp_path)
    main(["plan", "render", str(plan), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert {d["id"] for d in data} == {"alpha", "beta", "gamma"}


def test_render_no_flags_count_matches_plan(tmp_path, capsys):
    plan = _plan_3(tmp_path)
    main(["plan", "render", str(plan), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 3


# ══════════════════════════════════════════════════════════════════════════════ #
#  plan validate: --only and --exclude
# ══════════════════════════════════════════════════════════════════════════════ #

def test_validate_only_exact_exits_zero(tmp_path, capsys):
    plan = _plan_3(tmp_path)
    rc = main(["plan", "validate", str(plan), "--only", "alpha"])
    assert rc == 0


def test_validate_only_shows_only_selected_job(tmp_path, capsys):
    plan = _plan_3(tmp_path)
    main(["plan", "validate", str(plan), "--only", "alpha"])
    out = capsys.readouterr().out
    assert "ok  alpha" in out
    assert "beta" not in out
    assert "gamma" not in out


def test_validate_only_glob(tmp_path, capsys):
    plan = _matrix_plan(tmp_path)
    main(["plan", "validate", str(plan), "--only", "feed-*", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert set(data["resolved"]) == {"feed-tech", "feed-science", "feed-business"}


def test_validate_exclude_subset(tmp_path, capsys):
    plan = _plan_3(tmp_path)
    main(["plan", "validate", str(plan), "--exclude", "beta", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert "beta" not in data["resolved"]
    assert "alpha" in data["resolved"]
    assert "gamma" in data["resolved"]


def test_validate_only_no_match_exits_nonzero(tmp_path, capsys):
    plan = _plan_3(tmp_path)
    rc = main(["plan", "validate", str(plan), "--only", "no-such-job"])
    assert rc != 0


def test_validate_only_no_match_clean_ujin_message(tmp_path, capsys):
    plan = _plan_3(tmp_path)
    main(["plan", "validate", str(plan), "--only", "no-such-job"])
    err = capsys.readouterr().err
    assert "ujin:" in err
    assert "no-such-job" in err


def test_validate_only_no_match_no_traceback(tmp_path, capsys):
    plan = _plan_3(tmp_path)
    main(["plan", "validate", str(plan), "--only", "no-such-job"])
    err = capsys.readouterr().err
    assert "Traceback" not in err


def test_validate_json_only_resolved_contains_only_match(tmp_path, capsys):
    plan = _plan_3(tmp_path)
    rc = main(["plan", "validate", str(plan), "--only", "alpha", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert data["resolved"] == ["alpha"]


def test_validate_json_only_no_match_error_field(tmp_path, capsys):
    plan = _plan_3(tmp_path)
    rc = main(["plan", "validate", str(plan), "--only", "no-such", "--json"])
    assert rc != 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False
    assert "ujin:" in data["error"]
    assert "no-such" in data["error"]


def test_validate_no_flags_is_unfiltered(tmp_path, capsys):
    """No flags → all jobs resolved (regression for unchanged default behavior)."""
    plan = _plan_3(tmp_path)
    main(["plan", "validate", str(plan), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert set(data["resolved"]) == {"alpha", "beta", "gamma"}


# ══════════════════════════════════════════════════════════════════════════════ #
#  jobs-serve: --only/--exclude filters plan.loaded; /health reflects selection
# ══════════════════════════════════════════════════════════════════════════════ #

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402
from ujin.jobs.app import create_jobs_app  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "jobs.db"
    monkeypatch.setenv("UJIN_JOBS_DB", str(path))
    return path


def test_jobs_serve_only_filters_plan_loaded(tmp_path, db):
    plan = _plan_3(tmp_path)
    app = create_jobs_app(run_engine=False, plan_path=str(plan), only=["alpha"])
    with TestClient(app) as c:
        health = c.get("/health").json()
    assert health["plan"]["loaded"] == ["alpha"]
    assert "beta" not in health["plan"]["loaded"]
    assert "gamma" not in health["plan"]["loaded"]


def test_jobs_serve_only_glob_filters_plan_loaded(tmp_path, db):
    plan = _matrix_plan(tmp_path)
    app = create_jobs_app(run_engine=False, plan_path=str(plan), only=["feed-t*"])
    with TestClient(app) as c:
        health = c.get("/health").json()
    assert health["plan"]["loaded"] == ["feed-tech"]


def test_jobs_serve_exclude_filters_plan_loaded(tmp_path, db):
    plan = _plan_3(tmp_path)
    app = create_jobs_app(run_engine=False, plan_path=str(plan), exclude=["beta"])
    with TestClient(app) as c:
        health = c.get("/health").json()
    loaded = health["plan"]["loaded"]
    assert "beta" not in loaded
    assert "alpha" in loaded
    assert "gamma" in loaded


def test_jobs_serve_no_flags_health_unchanged(tmp_path, db):
    """No selector flags → plan.loaded identical to unfiltered load (regression)."""
    plan = _plan_3(tmp_path)
    app = create_jobs_app(run_engine=False, plan_path=str(plan))
    with TestClient(app) as c:
        health = c.get("/health").json()
    assert set(health["plan"]["loaded"]) == {"alpha", "beta", "gamma"}


def test_jobs_serve_only_no_match_exits(tmp_path, db, capsys):
    """--only matching nothing aborts server startup and prints a clean ujin: message.

    anyio wraps sys.exit(1) in a BaseExceptionGroup / CancelledError when
    called from an async lifespan, so we only assert that the startup fails
    (any exception) and that the clean message reached stderr.
    """
    plan = _plan_3(tmp_path)
    app = create_jobs_app(run_engine=False, plan_path=str(plan), only=["no-match"])
    with pytest.raises(Exception):
        with TestClient(app):
            pass
    err = capsys.readouterr().err
    assert "ujin:" in err
    assert "no-match" in err
    assert "Traceback" not in err


# ══════════════════════════════════════════════════════════════════════════════ #
#  jobs-serve CLI pre-flight: --only no-match exits before launching uvicorn
# ══════════════════════════════════════════════════════════════════════════════ #

def test_jobs_serve_cli_only_no_match_exits_nonzero(tmp_path, monkeypatch, capsys):
    """_cmd_jobs_serve pre-flight exits 1 before starting the server."""
    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    plan = _plan_3(tmp_path)
    rc = main(["jobs-serve", "--plan", str(plan), "--only", "no-match"])
    assert rc != 0


def test_jobs_serve_cli_only_no_match_clean_message(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    plan = _plan_3(tmp_path)
    main(["jobs-serve", "--plan", str(plan), "--only", "no-match"])
    err = capsys.readouterr().err
    assert "ujin:" in err
    assert "no-match" in err


def test_jobs_serve_cli_only_no_match_no_traceback(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    plan = _plan_3(tmp_path)
    main(["jobs-serve", "--plan", str(plan), "--only", "no-match"])
    err = capsys.readouterr().err
    assert "Traceback" not in err
