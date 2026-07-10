"""External ingest-plan loader (ujin/jobs/plan.py) + its wiring into the app.

A plan is one mounted file declaring many jobs, sharing config through
``defaults`` (deep-merged into every job), named ``templates`` (opted into with
``use:``), and a per-entry ``matrix`` (cartesian expansion). Loading is
non-fatal: a bad or missing plan is logged and surfaced on ``/health`` under the
``plan`` block, never blocking startup. All offline and deterministic.
"""
from __future__ import annotations

import pytest

from ujin.jobs.plan import PLAN_ENV, PlanError, load_plan, parse_plan


# ---- pure parse_plan -------------------------------------------------------- #

def test_bare_list_of_jobs():
    specs = parse_plan([
        {"id": "a", "source": {"kind": "command", "config": {"argv": ["true"]}}},
        {"id": "b", "source": {"kind": "http", "config": {"url": "http://x"}}},
    ])
    assert [s.id for s in specs] == ["a", "b"]
    assert specs[1].source.kind == "http"


def test_defaults_deep_merge_and_job_wins():
    specs = parse_plan({
        "defaults": {
            "schedule": {"mode": "adaptive", "base": 300},
            "sinks": [{"kind": "sqlite", "config": {}}],
        },
        "jobs": [
            {"id": "a", "source": {"kind": "http", "config": {"url": "http://a"}}},
            {"id": "b", "source": {"kind": "http", "config": {"url": "http://b"}},
             "schedule": {"base": 999}},  # overrides only base; mode stays merged
        ],
    })
    a, b = specs
    assert a.schedule.base == 300 and a.schedule.mode == "adaptive"
    assert [s.kind for s in a.sinks] == ["sqlite"]
    assert b.schedule.base == 999 and b.schedule.mode == "adaptive"


def test_templates_use_single_and_list():
    specs = parse_plan({
        "templates": {
            "feed": {"source": {"kind": "rss"}, "transforms": [{"kind": "dedupe", "config": {"key": "id"}}]},
            "hourly": {"schedule": {"base": 3600}},
        },
        "jobs": [
            {"id": "one", "use": "feed", "source": {"config": {"url": "http://one/rss"}}},
            {"id": "two", "use": ["feed", "hourly"], "source": {"config": {"url": "http://two/rss"}}},
        ],
    })
    one, two = specs
    assert one.source.kind == "rss"
    assert one.source.config["url"] == "http://one/rss"
    assert [t.kind for t in one.transforms] == ["dedupe"]
    assert two.schedule.base == 3600


def test_matrix_expansion_defaults_and_ids():
    specs = parse_plan({
        "defaults": {"schedule": {"base": 120}},
        "templates": {"feed": {"source": {"kind": "rss"}}},
        "jobs": [{
            "id": "mirror",
            "use": "feed",
            "matrix": {"region": ["us", "eu", "ap"]},
            "source": {"config": {"url": "https://${region}.example.com/feed"}},
        }],
    })
    assert [s.id for s in specs] == ["mirror-0", "mirror-1", "mirror-2"]
    assert [s.source.config["url"] for s in specs] == [
        "https://us.example.com/feed",
        "https://eu.example.com/feed",
        "https://ap.example.com/feed",
    ]
    assert all(s.schedule.base == 120 for s in specs)  # defaults reach matrix jobs


def test_matrix_id_template_controls_naming():
    specs = parse_plan({"jobs": [{
        "id": "feed-${region}",
        "matrix": {"region": ["us", "eu"]},
        "source": {"kind": "rss", "config": {"url": "https://${region}/f"}},
    }]})
    assert [s.id for s in specs] == ["feed-us", "feed-eu"]


def test_var_substitution_env_and_default(monkeypatch):
    monkeypatch.setenv("PLAN_FEED_URL", "https://env.example.com/f")
    specs = parse_plan({"jobs": [
        {"id": "a", "source": {"kind": "rss", "config": {"url": "${PLAN_FEED_URL}"}}},
        {"id": "b", "source": {"kind": "rss", "config": {"url": "${UNSET_VAR:-https://fallback/f}"}}},
    ]})
    assert specs[0].source.config["url"] == "https://env.example.com/f"
    assert specs[1].source.config["url"] == "https://fallback/f"


def test_whole_token_preserves_type():
    # a lone ${n} referencing a numeric matrix axis stays an int in the config
    specs = parse_plan({"jobs": [{
        "id": "j",
        "matrix": {"rows": [25, 50]},
        "source": {"kind": "api", "config": {"url": "http://x", "rows": "${rows}"}},
    }]})
    assert specs[0].source.config["rows"] == 25
    assert isinstance(specs[0].source.config["rows"], int)


def test_unset_var_expands_to_empty(monkeypatch):
    monkeypatch.delenv("DEFINITELY_UNSET_PLAN_VAR", raising=False)
    specs = parse_plan({"jobs": [{
        "id": "j",
        "source": {"kind": "rss", "config": {
            "url": "${DEFINITELY_UNSET_PLAN_VAR}",              # whole-token -> ""
            "note": "x-${DEFINITELY_UNSET_PLAN_VAR}-y",         # embedded -> "x--y"
        }},
    }]})
    assert specs[0].source.config["url"] == ""
    assert specs[0].source.config["note"] == "x--y"


def test_matrix_var_overrides_env(monkeypatch):
    monkeypatch.setenv("region", "from-env")
    specs = parse_plan({"jobs": [{
        "id": "m", "matrix": {"region": ["us"]},
        "source": {"kind": "rss", "config": {"url": "https://${region}/f"}},
    }]})
    assert specs[0].source.config["url"] == "https://us/f"


# ---- actionable validation errors ------------------------------------------ #

@pytest.mark.parametrize("data, needle", [
    ({"defaults": {}}, "must define a 'jobs' list"),
    (42, "must be a mapping or a list"),
    ({"jobs": {"a": 1}}, "'jobs' must be a list"),
    ({"jobs": [["nope"]]}, "job #0 must be a mapping"),
    ({"defaults": [1], "jobs": []}, "'defaults' must be a mapping"),
    ({"templates": [1], "jobs": []}, "'templates' must be a mapping"),
    ({"jobs": [{"id": "x", "use": "ghost", "source": {"kind": "http"}}]},
     "unknown template 'ghost'"),
    ({"jobs": [{"id": "x", "matrix": {"r": []}, "source": {"kind": "http"}}]},
     "matrix axis 'r' must be a non-empty list"),
    ({"jobs": [{"id": "x", "matrix": [], "source": {"kind": "http"}}]},
     "'matrix' must be a non-empty mapping"),
    ({"jobs": [{"id": "x"}]}, "needs a 'source' with a 'kind'"),
    ({"jobs": [{"id": "x", "source": {"config": {}}}]}, "needs a 'source' with a 'kind'"),
])
def test_actionable_errors(data, needle):
    with pytest.raises(PlanError) as ei:
        parse_plan(data, source="t.yaml")
    assert needle in str(ei.value)


def test_invalid_jobspec_wrapped_as_planerror():
    # an out-of-shape schedule value trips JobSpec.from_dict -> wrapped, not raw
    with pytest.raises(PlanError) as ei:
        parse_plan({"jobs": [{"id": "x", "source": {"kind": "http"},
                              "schedule": {"bogus_field": 1}}]})
    assert "is invalid" in str(ei.value)


# ---- load_plan (file I/O) --------------------------------------------------- #

def _write(p, text: str):
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_load_plan_missing_path(tmp_path):
    with pytest.raises(PlanError) as ei:
        load_plan(tmp_path / "nope.yaml")
    assert "ingest plan not found" in str(ei.value)


def test_load_plan_empty_file_is_empty(tmp_path):
    path = _write(tmp_path / "empty.yaml", "\n")
    assert load_plan(path) == []


def test_load_plan_bad_yaml(tmp_path):
    path = _write(tmp_path / "bad.yaml", "jobs: [\n  - id: x\n :::")
    with pytest.raises(PlanError) as ei:
        load_plan(path)
    assert "not valid YAML/JSON" in str(ei.value)


def test_load_plan_no_source_is_empty(monkeypatch):
    monkeypatch.delenv(PLAN_ENV, raising=False)
    assert load_plan() == []


def test_unknown_top_level_key_rejected():
    with pytest.raises(PlanError) as ei:
        parse_plan({"jobs": [], "bogus": 1})
    assert "unknown top-level key" in str(ei.value)


def test_job_without_id_falls_back_to_stem_index():
    specs = parse_plan(
        {"jobs": [{"source": {"kind": "http", "config": {"url": "http://x"}}}]},
        source="/plans/site.yaml",
    )
    assert specs[0].id == "site-0"
    assert specs[0].name == "site-0"


def test_example_plan_loads(tmp_path):
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    specs = load_plan(root / "examples" / "ingest-plan.yaml")
    ids = [s.id for s in specs]
    assert "status-page" in ids
    assert {"mirror-0", "mirror-1", "mirror-2"} <= set(ids)


# ---- app wiring: /health plan block ---------------------------------------- #

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from ujin.jobs.app import create_jobs_app  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    return tmp_path


_VALID_PLAN = """
defaults:
  schedule: {mode: once}
  sinks: [{kind: sqlite, config: {}}]
templates:
  feed: {source: {kind: command}}
jobs:
  - id: solo
    source: {kind: command, config: {argv: ["printf", "hi"]}}
  - id: mirror
    use: feed
    matrix: {n: ["a", "b"]}
    source: {config: {argv: ["printf", "${n}"]}}
"""


def test_plan_registers_all_jobs_via_arg(db, tmp_path):
    path = _write(tmp_path / "plan.yaml", _VALID_PLAN)
    with TestClient(create_jobs_app(run_engine=False, plan_path=path)) as c:
        health = c.get("/health").json()
        assert health["plan"]["path"] == path
        assert set(health["plan"]["loaded"]) == {"solo", "mirror-0", "mirror-1"}
        assert health["plan"]["failed"] == []
        ids = {j["id"] for j in c.get("/jobs").json()}
        assert {"solo", "mirror-0", "mirror-1"} <= ids


def test_plan_registers_all_jobs_via_env(db, tmp_path, monkeypatch):
    path = _write(tmp_path / "plan.yaml", _VALID_PLAN)
    monkeypatch.setenv(PLAN_ENV, path)
    with TestClient(create_jobs_app(run_engine=False)) as c:
        loaded = c.get("/health").json()["plan"]["loaded"]
        assert set(loaded) == {"solo", "mirror-0", "mirror-1"}


def test_no_plan_configured_is_empty_block(db):
    with TestClient(create_jobs_app(run_engine=False)) as c:
        plan = c.get("/health").json()["plan"]
        assert plan == {"path": None, "loaded": [], "failed": []}


def test_missing_plan_is_non_fatal(db, tmp_path):
    missing = str(tmp_path / "gone.yaml")
    with TestClient(create_jobs_app(run_engine=False, plan_path=missing)) as c:
        health = c.get("/health").json()
        assert health["ok"] is True                    # startup survived
        assert "ingest plan not found" in health["plan"]["error"]
        assert health["plan"]["loaded"] == []


def test_malformed_plan_is_non_fatal_with_actionable_error(db, tmp_path):
    path = _write(tmp_path / "bad.yaml",
                  "jobs:\n  - id: broken\n    use: ghost\n    source: {kind: command}\n")
    with TestClient(create_jobs_app(run_engine=False, plan_path=path)) as c:
        health = c.get("/health").json()
        assert health["ok"] is True                    # not fatal
        assert "unknown template 'ghost'" in health["plan"]["error"]
        assert c.get("/jobs").json() == []             # nothing registered, no crash


def test_plan_per_job_failure_recorded_not_fatal(db, tmp_path):
    # a job with an unregistered kind fails at create() -> recorded in `failed`,
    # while the valid sibling still registers.
    path = _write(tmp_path / "mixed.yaml",
                  "jobs:\n"
                  "  - id: good\n    source: {kind: command, config: {argv: [\"true\"]}}\n"
                  "  - id: bad\n    source: {kind: no_such_kind}\n")
    with TestClient(create_jobs_app(run_engine=False, plan_path=path)) as c:
        plan = c.get("/health").json()["plan"]
        assert plan["loaded"] == ["good"]
        assert [f["id"] for f in plan["failed"]] == ["bad"]


def test_workflows_and_plan_coexist(db, tmp_path):
    import yaml

    wf = tmp_path / "workflows"
    wf.mkdir()
    (wf / "alpha.yaml").write_text(yaml.safe_dump({
        "source": {"kind": "command", "config": {"argv": ["printf", "a"]}},
        "schedule": {"mode": "once"},
    }))
    path = _write(tmp_path / "plan.yaml", _VALID_PLAN)
    with TestClient(create_jobs_app(run_engine=False, workflows_dir=str(wf), plan_path=path)) as c:
        health = c.get("/health").json()
        assert health["workflows"]["loaded"] == ["alpha"]   # unchanged path
        assert set(health["plan"]["loaded"]) == {"solo", "mirror-0", "mirror-1"}
