"""External ingest-plan loader: defaults+matrix expansion, ${VAR} substitution,
missing/empty/malformed plan handling, and the /health "plan" reporting.
"""
from __future__ import annotations

import pytest

from ujin.jobs.plan import PLAN_ENV, PlanError, load_plan, parse_plan


# --------------------------------------------------------------------------- #
# parse_plan: defaults + matrix expansion (pure, no filesystem)
# --------------------------------------------------------------------------- #

def test_empty_plan_yields_no_jobs():
    assert parse_plan(None) == []
    assert parse_plan({}) == []


def test_plain_job_no_matrix():
    data = {"jobs": [{"id": "homepage", "source": {"kind": "http", "config": {"url": "https://x"}}}]}
    specs = parse_plan(data)
    assert [s["id"] for s in specs] == ["homepage"]
    assert specs[0]["name"] == "homepage"


def test_defaults_deep_merge_lists_replace():
    data = {
        "defaults": {
            "sinks": [{"kind": "sqlite"}],
            "schedule": {"mode": "adaptive", "base": 300, "min": 60, "max": 3600},
        },
        "jobs": [
            {"id": "a", "source": {"kind": "http", "config": {"url": "https://a"}}},
            {
                "id": "b",
                "source": {"kind": "http", "config": {"url": "https://b"}},
                "schedule": {"mode": "adaptive", "base": 600, "min": 60, "max": 3600},
                "sinks": [{"kind": "webhook", "config": {"url": "https://hook"}}],
            },
        ],
    }
    specs = parse_plan(data)
    a, b = specs
    assert a["sinks"] == [{"kind": "sqlite"}]
    assert a["schedule"]["base"] == 300
    # job b overrides the default schedule.base and replaces sinks wholesale
    assert b["schedule"]["base"] == 600
    assert b["sinks"] == [{"kind": "webhook", "config": {"url": "https://hook"}}]


def test_matrix_mapping_cartesian_product():
    data = {
        "jobs": [
            {
                "id": "status-${name}",
                "matrix": {"name": ["github", "npm"]},
                "source": {"kind": "http", "config": {"url": "https://status.${name}/"}},
            }
        ]
    }
    specs = parse_plan(data)
    ids = {s["id"] for s in specs}
    assert ids == {"status-github", "status-npm"}
    urls = {s["id"]: s["source"]["config"]["url"] for s in specs}
    assert urls["status-github"] == "https://status.github/"
    assert urls["status-npm"] == "https://status.npm/"


def test_matrix_mapping_multi_axis_cartesian_product():
    data = {
        "jobs": [
            {
                "id": "combo-${a}-${b}",
                "matrix": {"a": ["x", "y"], "b": ["1", "2"]},
                "source": {"kind": "http", "config": {"url": "https://x"}},
            }
        ]
    }
    specs = parse_plan(data)
    ids = {s["id"] for s in specs}
    assert ids == {"combo-x-1", "combo-x-2", "combo-y-1", "combo-y-2"}


def test_matrix_list_rows_explicit():
    data = {
        "jobs": [
            {
                "id": "feed-${name}",
                "matrix": [
                    {"name": "releases", "path": "/releases.atom"},
                    {"name": "tags", "path": "/tags.atom"},
                ],
                "source": {"kind": "rss", "config": {"url": "https://x${path}"}},
            }
        ]
    }
    specs = parse_plan(data)
    by_id = {s["id"]: s for s in specs}
    assert set(by_id) == {"feed-releases", "feed-tags"}
    assert by_id["feed-releases"]["source"]["config"]["url"] == "https://x/releases.atom"
    assert by_id["feed-tags"]["source"]["config"]["url"] == "https://x/tags.atom"


def test_matrix_type_preserving_whole_scalar():
    # a numeric matrix axis substituted into a whole scalar keeps its type
    data = {
        "jobs": [
            {
                "id": "status-${name}",
                "matrix": [{"name": "a", "base": 300}, {"name": "b", "base": 600}],
                "source": {"kind": "http", "config": {"url": "https://x"}},
                "schedule": {"mode": "adaptive", "base": "${base}", "min": 60, "max": 3600},
            }
        ]
    }
    specs = parse_plan(data)
    by_id = {s["id"]: s for s in specs}
    assert by_id["status-a"]["schedule"]["base"] == 300
    assert isinstance(by_id["status-a"]["schedule"]["base"], int)
    assert by_id["status-b"]["schedule"]["base"] == 600


def test_matrix_embedded_reference_is_textual():
    data = {
        "jobs": [
            {
                "id": "j-${n}",
                "matrix": {"n": [1, 2]},
                "source": {"kind": "http", "config": {"url": "https://x/page-${n}"}},
            }
        ]
    }
    specs = parse_plan(data)
    urls = sorted(s["source"]["config"]["url"] for s in specs)
    assert urls == ["https://x/page-1", "https://x/page-2"]


def test_matrix_unknown_name_passes_through_for_env(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "secret")
    data = {
        "jobs": [
            {
                "id": "j-${n}",
                "matrix": {"n": [1]},
                "source": {"kind": "http", "config": {"url": "https://x?token=${MY_TOKEN}"}},
            }
        ]
    }
    specs = parse_plan(data)
    assert specs[0]["source"]["config"]["url"] == "https://x?token=secret"


def test_empty_matrix_yields_zero_jobs():
    data = {
        "jobs": [
            {"id": "a", "matrix": {}, "source": {"kind": "http", "config": {"url": "https://a"}}},
            {"id": "b", "source": {"kind": "http", "config": {"url": "https://b"}}},
        ]
    }
    specs = parse_plan(data)
    assert [s["id"] for s in specs] == ["b"]


def test_empty_list_matrix_yields_zero_jobs():
    data = {"jobs": [{"id": "a", "matrix": [], "source": {"kind": "http", "config": {"url": "https://a"}}}]}
    assert parse_plan(data) == []


def test_no_matrix_still_expands_env(monkeypatch):
    monkeypatch.setenv("BASE_URL", "https://real.example.com")
    data = {"jobs": [{"id": "homepage", "source": {"kind": "http", "config": {"url": "${BASE_URL}/"}}}]}
    specs = parse_plan(data)
    assert specs[0]["source"]["config"]["url"] == "https://real.example.com/"


def test_env_var_unset_expands_empty():
    data = {"jobs": [{"id": "homepage", "source": {"kind": "http", "config": {"url": "${UJIN_TEST_UNSET_VAR}x"}}}]}
    specs = parse_plan(data)
    assert specs[0]["source"]["config"]["url"] == "x"


def test_env_var_default_fallback():
    data = {"jobs": [{"id": "homepage", "source": {"kind": "http", "config": {"url": "${UJIN_TEST_UNSET_VAR:-fallback}"}}}]}
    specs = parse_plan(data)
    assert specs[0]["source"]["config"]["url"] == "fallback"


# --------------------------------------------------------------------------- #
# Job id assignment: distinct-else-<stem>-<i>
# --------------------------------------------------------------------------- #

def test_id_fallback_when_template_does_not_vary():
    data = {
        "jobs": [
            {
                "id": "static-id",
                "matrix": {"n": [1, 2, 3]},
                "source": {"kind": "http", "config": {"url": "https://x/${n}"}},
            }
        ]
    }
    specs = parse_plan(data)
    assert [s["id"] for s in specs] == ["static-id-0", "static-id-1", "static-id-2"]


def test_id_fallback_when_no_id_given():
    data = {
        "jobs": [
            {
                "matrix": {"n": [1, 2]},
                "source": {"kind": "http", "config": {"url": "https://x/${n}"}},
            }
        ]
    }
    specs = parse_plan(data)
    assert [s["id"] for s in specs] == ["job-0-0", "job-0-1"]


def test_id_fallback_single_combo_no_index_suffix():
    data = {
        "jobs": [
            {"matrix": {"n": [1]}, "source": {"kind": "http", "config": {"url": "https://x"}}},
        ]
    }
    specs = parse_plan(data)
    assert [s["id"] for s in specs] == ["job-0"]


def test_name_defaults_to_id():
    data = {"jobs": [{"id": "homepage", "source": {"kind": "http", "config": {"url": "https://x"}}}]}
    specs = parse_plan(data)
    assert specs[0]["name"] == "homepage"


def test_explicit_name_kept():
    data = {"jobs": [{"id": "homepage", "name": "Homepage!", "source": {"kind": "http", "config": {"url": "https://x"}}}]}
    specs = parse_plan(data)
    assert specs[0]["name"] == "Homepage!"


# --------------------------------------------------------------------------- #
# Malformed plans -> actionable PlanError
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("data,msg", [
    ("not-a-mapping", "must be a mapping"),
    ({"unexpected": 1}, "unknown top-level key"),
    ({"defaults": [1, 2]}, "`defaults:` must be a mapping"),
    ({"jobs": {"not": "a list"}}, "`jobs:` must be a list"),
    ({"jobs": ["not-a-mapping"]}, "must be a mapping"),
    ({"jobs": [{"matrix": "not-a-mapping-or-list"}]}, "matrix must be a mapping"),
    ({"jobs": [{"matrix": {"n": "not-a-list"}}]}, "matrix axis"),
    ({"jobs": [{"matrix": ["not-a-mapping"]}]}, "matrix list entries"),
])
def test_malformed_plan_raises_actionable_error(data, msg):
    with pytest.raises(PlanError, match=msg):
        parse_plan(data)


# --------------------------------------------------------------------------- #
# load_plan: file + env resolution
# --------------------------------------------------------------------------- #

def test_load_plan_none_and_unset_env_yields_no_jobs(monkeypatch):
    monkeypatch.delenv(PLAN_ENV, raising=False)
    assert load_plan(None) == []
    assert load_plan("") == []


def test_load_plan_missing_file_raises(tmp_path):
    missing = tmp_path / "nope.yaml"
    with pytest.raises(PlanError, match="not found"):
        load_plan(str(missing))


def test_load_plan_unreadable_file_raises(tmp_path):
    import os

    f = tmp_path / "secret.yaml"
    f.write_text("jobs: []\n")
    f.chmod(0)
    try:
        with pytest.raises(PlanError, match="could not be read"):
            load_plan(str(f))
    finally:
        f.chmod(0o644)  # restore so tmp_path cleanup can remove it
    assert os.access(str(f), os.R_OK)


def test_load_plan_malformed_yaml_raises(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("jobs: [unterminated\n")
    with pytest.raises(PlanError, match="not valid YAML"):
        load_plan(str(bad))


def test_load_plan_from_explicit_path(tmp_path):
    f = tmp_path / "plan.yaml"
    f.write_text("jobs:\n  - id: homepage\n    source: {kind: http, config: {url: https://x}}\n")
    specs = load_plan(str(f))
    assert [s["id"] for s in specs] == ["homepage"]


def test_load_plan_from_env_var(tmp_path, monkeypatch):
    f = tmp_path / "plan.yaml"
    f.write_text("jobs:\n  - id: homepage\n    source: {kind: http, config: {url: https://x}}\n")
    monkeypatch.setenv(PLAN_ENV, str(f))
    specs = load_plan(None)
    assert [s["id"] for s in specs] == ["homepage"]


def test_load_plan_explicit_path_overrides_env(tmp_path, monkeypatch):
    env_f = tmp_path / "env.yaml"
    env_f.write_text("jobs:\n  - id: from-env\n    source: {kind: http, config: {url: https://x}}\n")
    arg_f = tmp_path / "arg.yaml"
    arg_f.write_text("jobs:\n  - id: from-arg\n    source: {kind: http, config: {url: https://x}}\n")
    monkeypatch.setenv(PLAN_ENV, str(env_f))
    specs = load_plan(str(arg_f))
    assert [s["id"] for s in specs] == ["from-arg"]


def test_load_plan_example_file_has_six_jobs():
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / "examples" / "ingest-plan.yaml"
    specs = load_plan(str(example))
    ids = {s["id"] for s in specs}
    assert ids == {
        "homepage",
        "status-github", "status-npm", "status-pypi",
        "feed-releases", "feed-tags",
    }
    assert len(specs) == 6


# --------------------------------------------------------------------------- #
# create_jobs_app wiring + /health "plan" reporting
# --------------------------------------------------------------------------- #

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from ujin.jobs.app import create_jobs_app  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    monkeypatch.delenv(PLAN_ENV, raising=False)
    return tmp_path


def _write_plan(tmp_path, text: str):
    p = tmp_path / "plan.yaml"
    p.write_text(text)
    return p


def test_no_plan_is_a_noop(db):
    app = create_jobs_app(run_engine=False)
    with TestClient(app) as c:
        health = c.get("/health").json()
        assert health["plan"] == {"source": None, "loaded": 0, "failed": 0, "error": None}


def test_plan_path_loads_jobs_into_jobs_serve(db, tmp_path):
    plan = _write_plan(tmp_path, """
defaults:
  sinks: [{kind: sqlite}]
  schedule: {mode: adaptive, base: 300, min: 60, max: 3600}
jobs:
  - id: status-${name}
    matrix: {name: [a, b]}
    source: {kind: http, config: {url: "https://x/${name}"}}
""")
    app = create_jobs_app(run_engine=False, plan_path=str(plan))
    with TestClient(app) as c:
        health = c.get("/health").json()
        assert health["plan"]["source"] == str(plan)
        assert health["plan"]["loaded"] == 2
        assert health["plan"]["failed"] == 0
        assert health["plan"]["error"] is None
        ids = {j["id"] for j in c.get("/jobs").json()}
        assert {"status-a", "status-b"} <= ids


def test_plan_env_var_loads_jobs(db, tmp_path, monkeypatch):
    plan = _write_plan(tmp_path, """
jobs:
  - id: homepage
    source: {kind: http, config: {url: "https://x"}}
""")
    monkeypatch.setenv(PLAN_ENV, str(plan))
    app = create_jobs_app(run_engine=False)
    with TestClient(app) as c:
        health = c.get("/health").json()
        assert health["plan"]["loaded"] == 1
        ids = {j["id"] for j in c.get("/jobs").json()}
        assert "homepage" in ids


def test_missing_plan_file_is_non_fatal(db, tmp_path):
    missing = tmp_path / "nope.yaml"
    app = create_jobs_app(run_engine=False, plan_path=str(missing))
    with TestClient(app) as c:
        health = c.get("/health").json()
        assert health["ok"] is True
        assert health["plan"]["error"] is not None
        assert "not found" in health["plan"]["error"]
        assert health["plan"]["loaded"] == 0


def test_malformed_plan_is_non_fatal(db, tmp_path):
    plan = _write_plan(tmp_path, "not_defaults_or_jobs: true\n")
    app = create_jobs_app(run_engine=False, plan_path=str(plan))
    with TestClient(app) as c:
        health = c.get("/health").json()
        assert health["ok"] is True
        assert health["plan"]["error"] is not None
        assert health["plan"]["loaded"] == 0


def test_plan_job_creation_failure_is_reported_not_fatal(db, tmp_path):
    plan = _write_plan(tmp_path, """
jobs:
  - id: good
    source: {kind: http, config: {url: "https://x"}}
  - id: bad
    source: {kind: no_such_kind, config: {}}
""")
    app = create_jobs_app(run_engine=False, plan_path=str(plan))
    with TestClient(app) as c:
        health = c.get("/health").json()
        assert health["plan"]["loaded"] == 1
        assert health["plan"]["failed"] == 1
        assert health["plan"]["error"] is None
        ids = {j["id"] for j in c.get("/jobs").json()}
        assert ids == {"good"}


def test_example_plan_file_loads_via_create_jobs_app(db):
    from pathlib import Path

    example = str(Path(__file__).resolve().parents[1] / "examples" / "ingest-plan.yaml")
    app = create_jobs_app(run_engine=False, plan_path=example)
    with TestClient(app) as c:
        health = c.get("/health").json()
        assert health["plan"]["loaded"] == 6
        assert health["plan"]["failed"] == 0
