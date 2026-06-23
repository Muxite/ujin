"""INGEST-PLAN loader — many jobs from one mountable YAML/JSON file (offline).

A plan reuses the same additive layers as a workflow file (``defaults:`` deep-merge,
``include:``/``use:`` fragments, ``matrix:``/``for_each:`` fan-out) and derives a
stable id per job so re-loading upserts. These tests exercise the pure loader
helpers in :mod:`ujin.jobs.app` directly plus a few end-to-end startup checks via
the FastAPI app. All offline and deterministic — no network.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ujin.jobs.app import _load_ingest_plan, _plan_entries

_ROOT = Path(__file__).resolve().parents[1]


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _effective(spec) -> dict:
    """A spec's content with the volatile/identity fields stripped."""
    d = spec.to_dict()
    for k in ("id", "name", "created_at"):
        d.pop(k, None)
    return d


# ── shape: list form, mapping form, empty ──────────────────────────────────── #
def test_plan_entries_list_form():
    defaults, entries = _plan_entries([{"id": "a"}, {"id": "b"}], "p.yaml")
    assert defaults == {} and entries == [{"id": "a"}, {"id": "b"}]


def test_plan_entries_mapping_form():
    defaults, entries = _plan_entries(
        {"defaults": {"schedule": {"mode": "once"}}, "jobs": [{"id": "a"}]}, "p.yaml"
    )
    assert defaults == {"schedule": {"mode": "once"}} and entries == [{"id": "a"}]


def test_empty_plan_loads_nothing(tmp_path):
    f = tmp_path / "empty.yaml"
    f.write_text("", encoding="utf-8")
    failed: list = []
    assert _load_ingest_plan(f, failed=failed) == []
    assert failed == []


# ── additive no-op: a plain list == the equivalent workflow jobs list ───────── #
def test_plain_plan_matches_equivalent_jobs_list(tmp_path):
    from ujin.jobs.app import _specs_from_workflow_file

    jobs = [
        {"source": {"kind": "http", "config": {"url": "https://a/"}},
         "sinks": [{"kind": "sqlite", "config": {}}], "schedule": {"mode": "once"}},
        {"source": {"kind": "http", "config": {"url": "https://b/"}},
         "schedule": {"mode": "once"}},
    ]
    # same content, same stem -> same ids and same effective specs through both loaders
    _write(tmp_path / "noop.yaml", {"jobs": jobs})
    plan_specs = _load_ingest_plan(tmp_path / "noop.yaml")
    wf_specs = _specs_from_workflow_file(tmp_path / "noop.yaml")

    assert [s.id for s in plan_specs] == ["noop-0", "noop-1"]
    assert [s.id for s in plan_specs] == [s.id for s in wf_specs]
    assert [_effective(s) for s in plan_specs] == [_effective(s) for s in wf_specs]


def test_explicit_ids_win_over_stem_index(tmp_path):
    _write(tmp_path / "p.yaml", [
        {"id": "papers", "source": {"kind": "http", "config": {"url": "https://a/"}}},
        {"source": {"kind": "http", "config": {"url": "https://b/"}}},
    ])
    specs = _load_ingest_plan(tmp_path / "p.yaml")
    # explicit id kept; the one without falls back to <stem>-<index>
    assert [s.id for s in specs] == ["papers", "p-1"]


# ── defaults + matrix + include applied across multiple jobs ────────────────── #
def test_defaults_matrix_include_across_jobs(tmp_path):
    # a shared sink fragment, referenced from defaults
    _write(tmp_path / "fragments" / "sink.yaml",
           {"kind": "webhook", "config": {"url": "https://ingest.test/in"}})
    _write(tmp_path / "plan.yaml", {
        "defaults": {
            "source": {"kind": "api", "config": {"method": "GET", "json_path": "items"}},
            "sinks": [{"include": "fragments/sink.yaml"}],
            "schedule": {"mode": "adaptive", "base": 1800},
        },
        "jobs": [
            {"id": "crossref",
             "source": {"config": {"url": "https://api.test/works"}}},
            {"id": "feed-{{ slug }}",
             "matrix": [{"slug": "tech"}, {"slug": "bio"}],
             "source": {"config": {"url": "https://feeds.test/{{ slug }}"}},
             "sinks": [{"kind": "jsonl", "config": {"path": "/data/{{ slug }}.jsonl"}}]},
        ],
    })
    specs = _load_ingest_plan(tmp_path / "plan.yaml")
    by_id = {s.id: s for s in specs}
    assert set(by_id) == {"crossref", "feed-tech", "feed-bio"}

    # defaults deep-merged under the plain job: source kind/method from defaults,
    # url from the job; the shared include resolved into its sink.
    cr = by_id["crossref"]
    assert cr.source.kind == "api"
    assert cr.source.config == {"method": "GET", "json_path": "items",
                                "url": "https://api.test/works"}
    assert cr.sinks[0].kind == "webhook"
    assert cr.sinks[0].config["url"] == "https://ingest.test/in"
    assert cr.schedule.base == 1800.0  # inherited from defaults

    # matrix fanned out, per-entry vars substituted into source + sink path
    assert by_id["feed-tech"].source.config["url"] == "https://feeds.test/tech"
    assert by_id["feed-bio"].sinks[0].config["path"] == "/data/bio.jsonl"
    # defaults' schedule still inherited by the matrix jobs
    assert by_id["feed-tech"].schedule.base == 1800.0


def test_matrix_id_suffix_when_no_explicit_template(tmp_path):
    # a matrix entry without an `id:` template gets <base>-<n> suffixes
    _write(tmp_path / "plan.yaml", {"jobs": [
        {"source": {"kind": "http", "config": {"url": "https://x/"}}},
        {"matrix": [{"n": "a"}, {"n": "b"}],
         "source": {"kind": "http", "config": {"url": "https://m/{{ n }}"}}},
    ]})
    specs = _load_ingest_plan(tmp_path / "plan.yaml")
    assert [s.id for s in specs] == ["plan-0", "plan-1-0", "plan-1-1"]
    assert [s.source.config["url"] for s in specs] == [
        "https://x/", "https://m/a", "https://m/b"]


def test_include_resolves_via_workflows_dir(tmp_path, monkeypatch):
    # a fragment NOT next to the plan is found via $UJIN_WORKFLOWS_DIR
    wf = tmp_path / "wf"
    _write(wf / "sched.yaml", {"mode": "once"})
    monkeypatch.setenv("UJIN_WORKFLOWS_DIR", str(wf))
    plan_dir = tmp_path / "plans"
    _write(plan_dir / "p.yaml", [
        {"id": "j", "source": {"kind": "http", "config": {"url": "https://a/"}},
         "schedule": {"include": "sched.yaml"}},
    ])
    (spec,) = _load_ingest_plan(plan_dir / "p.yaml")
    assert spec.schedule.mode == "once"


# ── malformed plans: actionable error, no crash, valid jobs still load ──────── #
def test_non_mapping_non_list_is_file_level_error(tmp_path):
    f = tmp_path / "bad.yaml"
    f.write_text("just a scalar string", encoding="utf-8")
    failed: list = []
    assert _load_ingest_plan(f, failed=failed) == []
    assert len(failed) == 1
    assert failed[0]["id"] == "bad"
    assert failed[0]["error"].startswith("ujin:")
    assert "must be a list" in failed[0]["error"]
    assert "bad.yaml" in failed[0]["error"]


def test_mapping_without_jobs_is_error(tmp_path):
    _write(tmp_path / "bad.yaml", {"defaults": {"schedule": {"mode": "once"}}})
    failed: list = []
    assert _load_ingest_plan(tmp_path / "bad.yaml", failed=failed) == []
    assert "`jobs:` list" in failed[0]["error"]


def test_defaults_must_be_mapping(tmp_path):
    with pytest.raises(ValueError, match="defaults:"):
        _plan_entries({"defaults": [1, 2], "jobs": []}, "p.yaml")


def test_jobs_must_be_list(tmp_path):
    with pytest.raises(ValueError, match="jobs:"):
        _plan_entries({"jobs": "nope"}, "p.yaml")


def test_bad_matrix_fails_one_job_others_load(tmp_path):
    _write(tmp_path / "plan.yaml", [
        {"id": "good", "source": {"kind": "http", "config": {"url": "https://a/"}}},
        {"id": "bad", "matrix": "nope",
         "source": {"kind": "http", "config": {"url": "https://b/"}}},
    ])
    failed: list = []
    specs = _load_ingest_plan(tmp_path / "plan.yaml", failed=failed)
    assert [s.id for s in specs] == ["good"]            # valid job still loaded
    assert [f["id"] for f in failed] == ["bad"]         # bad one reported, named
    assert failed[0]["error"].startswith("ujin: plan job bad:")


def test_colliding_ids_fail_the_duplicate(tmp_path):
    _write(tmp_path / "plan.yaml", [
        {"id": "dup", "source": {"kind": "http", "config": {"url": "https://a/"}}},
        {"id": "dup", "source": {"kind": "http", "config": {"url": "https://b/"}}},
    ])
    failed: list = []
    specs = _load_ingest_plan(tmp_path / "plan.yaml", failed=failed)
    assert [s.id for s in specs] == ["dup"]
    assert failed[0]["id"] == "dup"
    assert "duplicate job id" in failed[0]["error"]


def test_missing_include_is_file_level_error(tmp_path):
    _write(tmp_path / "plan.yaml", {"jobs": [
        {"id": "j", "include": "nope.yaml"},
    ]})
    failed: list = []
    assert _load_ingest_plan(tmp_path / "plan.yaml", failed=failed) == []
    assert failed[0]["id"] == "plan"
    assert "include fragment not found" in failed[0]["error"]
    assert failed[0]["error"].startswith("ujin:")


def test_missing_file_is_reported_not_raised(tmp_path):
    failed: list = []
    assert _load_ingest_plan(tmp_path / "nope.yaml", failed=failed) == []
    assert failed[0]["id"] == "nope"
    assert failed[0]["error"].startswith("ujin:")


# ── shipped example loads through the loader ────────────────────────────────── #
def test_shipped_example_plan_loads():
    pytest.importorskip("yaml")
    failed: list = []
    specs = _load_ingest_plan(_ROOT / "examples" / "ingest-plan.yaml", failed=failed)
    assert failed == []
    assert [s.id for s in specs] == [
        "crossref", "feed-tech", "feed-science", "feed-business"]
    feeds = {s.id: s for s in specs if s.id.startswith("feed-")}
    assert feeds["feed-tech"].source.config["url"] == \
        "https://feeds.example.com/tech?rows=50"
    assert feeds["feed-science"].sinks[-1].config["path"] == "/data/science.jsonl"


# ── end-to-end: env var, --plan override, /health, startup failures ─────────── #
def _client():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    return TestClient


def _basic_plan(tmp_path: Path, name: str, url: str) -> Path:
    f = tmp_path / name
    _write(f, [{"id": Path(name).stem, "schedule": {"mode": "once"},
                "source": {"kind": "command", "config": {"argv": ["printf", url]}}}])
    return f


def test_env_var_resolution(tmp_path, monkeypatch):
    TestClient = _client()
    from ujin.jobs.app import create_jobs_app

    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    plan = _basic_plan(tmp_path, "viaenv.yaml", "hi")
    monkeypatch.setenv("UJIN_INGEST_PLAN", str(plan))

    with TestClient(create_jobs_app(run_engine=False)) as c:
        health = c.get("/health").json()
        assert health["plan"]["path"] == str(plan)
        assert health["plan"]["loaded"] == ["viaenv"]
        assert health["plan"]["failed"] == []
        assert "viaenv" in {j["id"] for j in c.get("/jobs").json()}


def test_plan_flag_overrides_env_var(tmp_path, monkeypatch):
    TestClient = _client()
    from ujin.jobs.app import create_jobs_app

    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    env_plan = _basic_plan(tmp_path, "fromenv.yaml", "e")
    flag_plan = _basic_plan(tmp_path, "fromflag.yaml", "f")
    monkeypatch.setenv("UJIN_INGEST_PLAN", str(env_plan))

    with TestClient(create_jobs_app(run_engine=False, plan_path=str(flag_plan))) as c:
        ids = {j["id"] for j in c.get("/jobs").json()}
        assert "fromflag" in ids and "fromenv" not in ids
        assert c.get("/health").json()["plan"]["loaded"] == ["fromflag"]


def test_no_plan_means_no_health_plan_key(tmp_path, monkeypatch):
    TestClient = _client()
    from ujin.jobs.app import create_jobs_app

    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    monkeypatch.delenv("UJIN_INGEST_PLAN", raising=False)
    with TestClient(create_jobs_app(run_engine=False)) as c:
        assert "plan" not in c.get("/health").json()


def test_health_reports_failed_jobs_while_valid_load(tmp_path, monkeypatch):
    TestClient = _client()
    from ujin.jobs.app import create_jobs_app

    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    # one good job, one with a bad matrix (loader-level fail), and one whose source
    # kind is unknown (manager.create fail at startup) -> both reported, good loads.
    _write(tmp_path / "plan.yaml", [
        {"id": "good", "schedule": {"mode": "once"},
         "source": {"kind": "command", "config": {"argv": ["printf", "ok"]}}},
        {"id": "badmatrix", "matrix": "nope",
         "source": {"kind": "command", "config": {"argv": ["printf", "x"]}}},
        {"id": "badkind", "schedule": {"mode": "once"},
         "source": {"kind": "totally-unregistered", "config": {}}},
    ])
    with TestClient(create_jobs_app(run_engine=False,
                                    plan_path=str(tmp_path / "plan.yaml"))) as c:
        health = c.get("/health").json()
        assert health["plan"]["loaded"] == ["good"]
        failed_ids = {f["id"] for f in health["plan"]["failed"]}
        assert failed_ids == {"badmatrix", "badkind"}
        assert all(f["error"].startswith("ujin:") for f in health["plan"]["failed"])
        assert "good" in {j["id"] for j in c.get("/jobs").json()}


def test_reload_upserts_plan_jobs(tmp_path):
    TestClient = _client()
    from ujin.jobs.app import create_jobs_app

    import os
    os.environ["UJIN_JOBS_DB"] = str(tmp_path / "jobs.db")
    try:
        _write(tmp_path / "plan.yaml", {"jobs": [
            {"id": "feed-{{ slug }}", "matrix": [{"slug": "a"}, {"slug": "b"}],
             "schedule": {"mode": "once"},
             "source": {"kind": "command", "config": {"argv": ["printf", "{{ slug }}"]}}},
        ]})
        plan = str(tmp_path / "plan.yaml")
        with TestClient(create_jobs_app(run_engine=False, plan_path=plan)) as c:
            first = sorted(j["id"] for j in c.get("/jobs").json())
        with TestClient(create_jobs_app(run_engine=False, plan_path=plan)) as c:
            second = sorted(j["id"] for j in c.get("/jobs").json())
        assert first == second == ["feed-a", "feed-b"]
    finally:
        os.environ.pop("UJIN_JOBS_DB", None)
