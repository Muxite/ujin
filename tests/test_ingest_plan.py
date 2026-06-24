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
    assert failed[0]["error"].startswith("ujin: plan job bad in")


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


# ── required-variable substitution: ${VAR:?message} ─────────────────────────── #

def test_required_var_present_substitutes_value(tmp_path, monkeypatch):
    """${VAR:?msg} expands to the env value when the variable is set and non-empty."""
    monkeypatch.setenv("TEST_REQUIRED_TOKEN", "mytoken")
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        "- id: j\n"
        "  schedule: {mode: once}\n"
        '  source: {kind: command, config: {argv: ["printf", "${TEST_REQUIRED_TOKEN:?must be set}"]}}\n',
        encoding="utf-8",
    )
    failed: list = []
    specs = _load_ingest_plan(plan, failed=failed)
    assert failed == []
    assert len(specs) == 1
    assert specs[0].source.config["argv"][-1] == "mytoken"


def test_required_var_unset_raises_actionable_error(tmp_path, monkeypatch):
    """${VAR:?msg} fails with an error naming the variable when it is unset."""
    monkeypatch.delenv("TEST_MISSING_VAR", raising=False)
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        "- id: j\n"
        "  schedule: {mode: once}\n"
        '  source: {kind: command, config: {argv: ["printf", "${TEST_MISSING_VAR:?set the API token}"]}}\n',
        encoding="utf-8",
    )
    failed: list = []
    specs = _load_ingest_plan(plan, failed=failed)
    assert specs == []
    assert len(failed) == 1
    assert "TEST_MISSING_VAR" in failed[0]["error"]
    assert failed[0]["error"].startswith("ujin:")


def test_required_var_empty_raises_actionable_error(tmp_path, monkeypatch):
    """${VAR:?msg} fails with an error when the variable is set but empty."""
    monkeypatch.setenv("TEST_EMPTY_VAR", "")
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        "- id: j\n"
        "  schedule: {mode: once}\n"
        '  source: {kind: command, config: {argv: ["printf", "${TEST_EMPTY_VAR:?cannot be empty}"]}}\n',
        encoding="utf-8",
    )
    failed: list = []
    specs = _load_ingest_plan(plan, failed=failed)
    assert specs == []
    assert len(failed) == 1
    assert "TEST_EMPTY_VAR" in failed[0]["error"]
    assert failed[0]["error"].startswith("ujin:")


def test_required_var_error_includes_custom_message(tmp_path, monkeypatch):
    """The optional message from ${VAR:?message} appears in the error text."""
    monkeypatch.delenv("UJIN_API_KEY", raising=False)
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        "- id: j\n"
        "  schedule: {mode: once}\n"
        '  source: {kind: command, config: {argv: ["printf", "${UJIN_API_KEY:?set your API key in .env}"]}}\n',
        encoding="utf-8",
    )
    failed: list = []
    _load_ingest_plan(plan, failed=failed)
    assert len(failed) == 1
    assert "set your API key in .env" in failed[0]["error"]


def test_required_var_no_message_still_names_variable(tmp_path, monkeypatch):
    """${VAR:?} with no message still names the variable in the error."""
    monkeypatch.delenv("UNNAMED_REQUIRED", raising=False)
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        "- id: j\n"
        "  schedule: {mode: once}\n"
        '  source: {kind: command, config: {argv: ["printf", "${UNNAMED_REQUIRED:?}"]}}\n',
        encoding="utf-8",
    )
    failed: list = []
    _load_ingest_plan(plan, failed=failed)
    assert len(failed) == 1
    assert "UNNAMED_REQUIRED" in failed[0]["error"]


def test_plain_var_substitution_unchanged(tmp_path, monkeypatch):
    """${VAR} (no operator) still expands to the value or empty — regression guard."""
    monkeypatch.setenv("PLAIN_SET", "hello")
    monkeypatch.delenv("PLAIN_UNSET", raising=False)
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        "- id: set\n"
        "  schedule: {mode: once}\n"
        '  source: {kind: command, config: {argv: ["printf", "${PLAIN_SET}"]}}\n'
        "- id: unset\n"
        "  schedule: {mode: once}\n"
        '  source: {kind: command, config: {argv: ["printf", "${PLAIN_UNSET}"]}}\n',
        encoding="utf-8",
    )
    failed: list = []
    specs = _load_ingest_plan(plan, failed=failed)
    assert failed == []
    assert len(specs) == 2
    by_id = {s.id: s for s in specs}
    assert by_id["set"].source.config["argv"][-1] == "hello"
    assert by_id["unset"].source.config["argv"][-1] == ""


def test_default_var_substitution_unchanged(tmp_path, monkeypatch):
    """${VAR:-default} still returns value-or-default — regression guard."""
    monkeypatch.setenv("DEF_SET", "val")
    monkeypatch.delenv("DEF_UNSET", raising=False)
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        "- id: set\n"
        "  schedule: {mode: once}\n"
        '  source: {kind: command, config: {argv: ["printf", "${DEF_SET:-fallback}"]}}\n'
        "- id: unset\n"
        "  schedule: {mode: once}\n"
        '  source: {kind: command, config: {argv: ["printf", "${DEF_UNSET:-fallback}"]}}\n',
        encoding="utf-8",
    )
    failed: list = []
    specs = _load_ingest_plan(plan, failed=failed)
    assert failed == []
    assert len(specs) == 2
    by_id = {s.id: s for s in specs}
    assert by_id["set"].source.config["argv"][-1] == "val"
    assert by_id["unset"].source.config["argv"][-1] == "fallback"


def test_required_var_health_reports_as_failed(tmp_path, monkeypatch):
    """An unsatisfied ${VAR:?} in a plan appears in /health failed; other jobs load."""
    TestClient = _client()
    from ujin.jobs.app import create_jobs_app

    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    monkeypatch.delenv("MISSING_HEALTH_VAR", raising=False)
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        "jobs:\n"
        "  - id: good\n"
        "    schedule: {mode: once}\n"
        "    source: {kind: command, config: {argv: [printf, ok]}}\n"
        "  - id: bad\n"
        "    schedule: {mode: once}\n"
        "    source: {kind: command, config: {argv: [printf, '${MISSING_HEALTH_VAR:?must be set}']}}\n",
        encoding="utf-8",
    )
    # A required var failure is file-level: the whole plan fails, not one job.
    with TestClient(create_jobs_app(run_engine=False, plan_path=str(plan))) as c:
        health = c.get("/health").json()
        assert health["plan"]["failed"]
        assert all(f["error"].startswith("ujin:") for f in health["plan"]["failed"])
        assert "MISSING_HEALTH_VAR" in health["plan"]["failed"][0]["error"]


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


# ── POST /plan/reload: pick up file changes without a restart ──────────────── #
def test_reload_endpoint_picks_up_added_and_changed_jobs(tmp_path, monkeypatch):
    TestClient = _client()
    from ujin.jobs.app import create_jobs_app

    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    plan = tmp_path / "plan.yaml"
    _write(plan, [{"id": "alpha", "schedule": {"mode": "once"},
                   "source": {"kind": "command", "config": {"argv": ["printf", "a"]}}}])

    with TestClient(create_jobs_app(run_engine=False, plan_path=str(plan))) as c:
        assert {j["id"] for j in c.get("/jobs").json()} == {"alpha"}

        # Add a job and change the existing one, then reload in-process.
        _write(plan, [
            {"id": "alpha", "schedule": {"mode": "once"},
             "source": {"kind": "command", "config": {"argv": ["printf", "AA"]}}},
            {"id": "beta", "schedule": {"mode": "once"},
             "source": {"kind": "command", "config": {"argv": ["printf", "b"]}}},
        ])
        body = c.post("/plan/reload").json()
        assert body["reloaded"] is True
        assert sorted(body["loaded"]) == ["alpha", "beta"] and body["failed"] == []
        assert body["path"] == str(plan)

        # New job is present, existing one upserted (no duplicate), /health refreshed.
        assert {j["id"] for j in c.get("/jobs").json()} == {"alpha", "beta"}
        assert sorted(c.get("/health").json()["plan"]["loaded"]) == ["alpha", "beta"]
        spec = c.get("/jobs/alpha").json()["spec"]
        assert spec["source"]["config"]["argv"] == ["printf", "AA"]


def test_reload_endpoint_reports_malformed_job_without_aborting(tmp_path, monkeypatch):
    TestClient = _client()
    from ujin.jobs.app import create_jobs_app

    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    plan = tmp_path / "plan.yaml"
    _write(plan, [{"id": "ok", "schedule": {"mode": "once"},
                   "source": {"kind": "command", "config": {"argv": ["printf", "x"]}}}])

    with TestClient(create_jobs_app(run_engine=False, plan_path=str(plan))) as c:
        _write(plan, [
            {"id": "ok", "schedule": {"mode": "once"},
             "source": {"kind": "command", "config": {"argv": ["printf", "x"]}}},
            {"id": "bad", "matrix": "nope",
             "source": {"kind": "command", "config": {"argv": ["printf", "y"]}}},
        ])
        body = c.post("/plan/reload").json()
        assert body["reloaded"] is True
        assert body["loaded"] == ["ok"]
        assert [f["id"] for f in body["failed"]] == ["bad"]
        assert body["failed"][0]["error"].startswith("ujin:")
        # The valid job loaded and /health mirrors the reload summary.
        assert "ok" in {j["id"] for j in c.get("/jobs").json()}
        assert c.get("/health").json()["plan"]["failed"][0]["id"] == "bad"


def test_reload_endpoint_noop_when_no_plan_configured(tmp_path, monkeypatch):
    TestClient = _client()
    from ujin.jobs.app import create_jobs_app

    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    monkeypatch.delenv("UJIN_INGEST_PLAN", raising=False)
    with TestClient(create_jobs_app(run_engine=False)) as c:
        r = c.post("/plan/reload")
        assert r.status_code == 200
        body = r.json()
        assert body == {"reloaded": False, "detail": "no plan configured",
                        "loaded": [], "failed": []}
        # No plan key materializes on /health -> no-plan deploys stay unchanged.
        assert "plan" not in c.get("/health").json()


def test_reload_endpoint_requires_auth(tmp_path, monkeypatch):
    TestClient = _client()
    from ujin.jobs.app import create_jobs_app

    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("UJIN_API_KEY", "s3cret")
    plan = tmp_path / "plan.yaml"
    _write(plan, [{"id": "alpha", "schedule": {"mode": "once"},
                   "source": {"kind": "command", "config": {"argv": ["printf", "a"]}}}])
    with TestClient(create_jobs_app(run_engine=False, plan_path=str(plan))) as c:
        assert c.post("/plan/reload").status_code == 401
        ok = c.post("/plan/reload", headers={"X-API-Key": "s3cret"})
        assert ok.status_code == 200 and ok.json()["reloaded"] is True


def test_reload_endpoint_honors_startup_only_filter(tmp_path, monkeypatch):
    # A server started with --only applies the SAME filter on reload, so a job
    # excluded at startup never sneaks in via /plan/reload.
    TestClient = _client()
    from ujin.jobs.app import create_jobs_app

    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    plan = tmp_path / "plan.yaml"
    _write(plan, [{"id": "alpha", "schedule": {"mode": "once"},
                   "source": {"kind": "command", "config": {"argv": ["printf", "a"]}}}])

    app = create_jobs_app(run_engine=False, plan_path=str(plan), only=["alpha"])
    with TestClient(app) as c:
        assert {j["id"] for j in c.get("/jobs").json()} == {"alpha"}
        # Add an out-of-filter job; reload must keep filtering it out.
        _write(plan, [
            {"id": "alpha", "schedule": {"mode": "once"},
             "source": {"kind": "command", "config": {"argv": ["printf", "a"]}}},
            {"id": "beta", "schedule": {"mode": "once"},
             "source": {"kind": "command", "config": {"argv": ["printf", "b"]}}},
        ])
        body = c.post("/plan/reload").json()
        assert body["reloaded"] is True and body["loaded"] == ["alpha"]
        assert {j["id"] for j in c.get("/jobs").json()} == {"alpha"}


def test_reload_endpoint_reports_only_no_match_without_killing_server(tmp_path, monkeypatch):
    # Unlike startup (which exits), a reload whose --only now matches nothing is
    # reported as not-reloaded and the server keeps running.
    TestClient = _client()
    from ujin.jobs.app import create_jobs_app

    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    plan = tmp_path / "plan.yaml"
    _write(plan, [{"id": "alpha", "schedule": {"mode": "once"},
                   "source": {"kind": "command", "config": {"argv": ["printf", "a"]}}}])

    app = create_jobs_app(run_engine=False, plan_path=str(plan), only=["alpha"])
    with TestClient(app) as c:
        # Drop the only matching job from the file; --only alpha now matches nothing.
        _write(plan, [{"id": "beta", "schedule": {"mode": "once"},
                       "source": {"kind": "command", "config": {"argv": ["printf", "b"]}}}])
        body = c.post("/plan/reload").json()
        assert body["reloaded"] is False
        assert "no job ids matched" in body["detail"]
        assert body["loaded"] == [] and body["failed"] == []
        # Server still serving; the originally-loaded job is untouched.
        assert c.get("/health").json()["status"] == "ok"
        assert {j["id"] for j in c.get("/jobs").json()} == {"alpha"}


# ── POST /plan/reload?prune=true: drop plan-owned jobs gone from the file ───── #
def _cmd_job(job_id, arg="x"):
    # `name` is explicit so the same dict works for the positional jobs.yaml
    # preload too (its loader doesn't default a name the way the plan loader does).
    return {"id": job_id, "name": job_id, "schedule": {"mode": "once"},
            "source": {"kind": "command", "config": {"argv": ["printf", arg]}}}


def test_reload_prune_removes_job_dropped_from_plan(tmp_path, monkeypatch):
    # With prune on, a job edited out of the plan disappears from the running
    # manager (and the /health plan set) and is listed under `pruned`.
    TestClient = _client()
    from ujin.jobs.app import create_jobs_app

    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    plan = tmp_path / "plan.yaml"
    _write(plan, [_cmd_job("keep", "k"), _cmd_job("drop", "d")])

    with TestClient(create_jobs_app(run_engine=False, plan_path=str(plan))) as c:
        assert {j["id"] for j in c.get("/jobs").json()} == {"keep", "drop"}
        # Drop one job from the file, reload with prune via the query param.
        _write(plan, [_cmd_job("keep", "k")])
        body = c.post("/plan/reload?prune=true").json()
        assert body["reloaded"] is True
        assert body["loaded"] == ["keep"]
        assert body["pruned"] == ["drop"]
        # The dropped job is gone from /jobs and the refreshed /health plan set.
        assert {j["id"] for j in c.get("/jobs").json()} == {"keep"}
        assert c.get("/jobs/drop").status_code == 404
        health = c.get("/health").json()["plan"]
        assert health["loaded"] == ["keep"] and health["pruned"] == ["drop"]


def test_reload_prune_accepts_json_body(tmp_path, monkeypatch):
    # The opt-in is also accepted as a {"prune": true} JSON body, not only ?prune.
    TestClient = _client()
    from ujin.jobs.app import create_jobs_app

    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    plan = tmp_path / "plan.yaml"
    _write(plan, [_cmd_job("keep", "k"), _cmd_job("drop", "d")])

    with TestClient(create_jobs_app(run_engine=False, plan_path=str(plan))) as c:
        _write(plan, [_cmd_job("keep", "k")])
        body = c.post("/plan/reload", json={"prune": True}).json()
        assert body["reloaded"] is True and body["pruned"] == ["drop"]
        assert {j["id"] for j in c.get("/jobs").json()} == {"keep"}


def test_reload_without_prune_retains_dropped_job(tmp_path, monkeypatch):
    # Regression: no prune option -> upsert-only, removes nothing, no `pruned`
    # key in either the response or the /health plan block (byte-identical to 0.24).
    TestClient = _client()
    from ujin.jobs.app import create_jobs_app

    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    plan = tmp_path / "plan.yaml"
    _write(plan, [_cmd_job("keep", "k"), _cmd_job("drop", "d")])

    with TestClient(create_jobs_app(run_engine=False, plan_path=str(plan))) as c:
        _write(plan, [_cmd_job("keep", "k")])
        body = c.post("/plan/reload").json()
        assert body["reloaded"] is True
        assert "pruned" not in body
        # The dropped job is retained (current upsert-only behaviour).
        assert {j["id"] for j in c.get("/jobs").json()} == {"keep", "drop"}
        assert "pruned" not in c.get("/health").json()["plan"]


def test_reload_prune_never_removes_api_or_preload_jobs(tmp_path, monkeypatch):
    # The guard: jobs created via the REST API or the positional jobs.yaml preload
    # are never pruned, even when prune is on and their ids are absent from the plan.
    TestClient = _client()
    from ujin.jobs.app import create_jobs_app

    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    preload = tmp_path / "jobs.yaml"
    _write(preload, [_cmd_job("preloaded", "p")])
    plan = tmp_path / "plan.yaml"
    _write(plan, [_cmd_job("planjob", "x")])

    app = create_jobs_app(str(preload), run_engine=False, plan_path=str(plan))
    with TestClient(app) as c:
        api_id = c.post("/jobs", json={
            "name": "apijob", "schedule": {"mode": "once"},
            "source": {"kind": "command", "config": {"argv": ["printf", "a"]}},
        }).json()["id"]
        assert {"preloaded", "planjob", api_id} <= {
            j["id"] for j in c.get("/jobs").json()
        }
        # Empty the plan entirely and prune: only the plan-owned job is removed.
        _write(plan, [])
        body = c.post("/plan/reload?prune=true").json()
        assert body["reloaded"] is True and body["pruned"] == ["planjob"]
        ids = {j["id"] for j in c.get("/jobs").json()}
        assert "planjob" not in ids
        assert "preloaded" in ids and api_id in ids


def test_reload_prune_skips_when_plan_unreadable(tmp_path, monkeypatch):
    # A file-level read failure must not wipe plan jobs even with prune on: the
    # empty id set can't be trusted, so prune is skipped and `pruned` is empty.
    TestClient = _client()
    from ujin.jobs.app import create_jobs_app

    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    plan = tmp_path / "plan.yaml"
    _write(plan, [_cmd_job("alpha", "a")])

    with TestClient(create_jobs_app(run_engine=False, plan_path=str(plan))) as c:
        assert {j["id"] for j in c.get("/jobs").json()} == {"alpha"}
        # A bare scalar is neither a list nor a mapping -> a file-level error.
        plan.write_text("just a string\n", encoding="utf-8")
        body = c.post("/plan/reload?prune=true").json()
        assert body["reloaded"] is True
        assert body["pruned"] == [] and body["failed"]
        # alpha survives the broken reload.
        assert {j["id"] for j in c.get("/jobs").json()} == {"alpha"}
