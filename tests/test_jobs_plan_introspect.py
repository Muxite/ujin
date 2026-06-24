"""``GET /plan`` — read-only introspection of the active plan / workflows dir.

The endpoint re-runs the SAME loaders + provenance plumbing that back
``plan render --explain`` (``_load_ingest_plan`` / ``_load_workflows_dir`` with
``provenance=...``) and returns each resolved job's id plus its provenance — the
originating source file and, for ``matrix:``/``for_each:`` fan-out, the matrix
entry (key/index/vars) that produced it. It only ever *reads*: no job is created,
mutated, or deleted.

All hermetic — a deterministic ``command`` source, ``run_engine=False``, no network.
"""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from ujin.jobs.app import create_jobs_app  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    return tmp_path / "jobs.db"


@pytest.fixture
def empty_wf(tmp_path):
    """An empty workflows directory, so the default ``/workflows`` is never read."""
    d = tmp_path / "wf"
    d.mkdir()
    return d


def _plan_tree(tmp_path):
    """A plan exercising defaults + an include fragment + a matrix fan-out.

    Resolves to three jobs: ``crossref`` (defaults only) and the matrix pair
    ``feed-tech`` / ``feed-science`` (each with a sink pulled from the fragment).
    """
    frag = tmp_path / "fragments" / "jsonl-sink.yaml"
    frag.parent.mkdir(parents=True, exist_ok=True)
    frag.write_text(
        "kind: jsonl\nconfig: {path: " + str(tmp_path / "out.jsonl") + "}\n",
        encoding="utf-8",
    )
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        "defaults:\n"
        "  schedule: {mode: once}\n"
        "jobs:\n"
        "  - id: crossref\n"
        "    source: {kind: command, config: {argv: [printf, hello]}}\n"
        "  - id: 'feed-{{ slug }}'\n"
        "    matrix: [{slug: tech}, {slug: science}]\n"
        "    source: {kind: command, config: {argv: [printf, '{{ slug }}']}}\n"
        "    sinks: [{include: fragments/jsonl-sink.yaml}]\n",
        encoding="utf-8",
    )
    return plan


def test_plan_introspect_defaults_include_matrix(db, tmp_path, empty_wf):
    """A plan with defaults/include/matrix surfaces every job + its provenance."""
    plan = _plan_tree(tmp_path)
    app = create_jobs_app(
        run_engine=False, plan_path=str(plan), workflows_dir=str(empty_wf)
    )
    with TestClient(app) as c:
        body = c.get("/plan").json()

    assert body["plan_path"] == str(plan)
    assert body["workflows_dir"] == str(empty_wf)
    assert body["failed"] == []

    by_id = {j["id"]: j for j in body["jobs"]}
    assert set(by_id) == {"crossref", "feed-tech", "feed-science"}
    # Every job names its originating plan file.
    for job in body["jobs"]:
        assert job["provenance"]["source"] == str(plan)
    # The plain job carries no matrix provenance.
    assert by_id["crossref"]["provenance"]["matrix"] is None
    # Matrix-expanded jobs name the entry index and the variable map.
    assert by_id["feed-tech"]["provenance"]["matrix"] == {
        "key": "matrix", "index": 0, "vars": {"slug": "tech"},
    }
    assert by_id["feed-science"]["provenance"]["matrix"] == {
        "key": "matrix", "index": 1, "vars": {"slug": "science"},
    }


def test_plan_introspect_is_read_only(db, tmp_path, empty_wf):
    """``GET /plan`` resolves the plan but never registers its jobs as running."""
    plan = _plan_tree(tmp_path)
    # No --plan configured: the plan file exists but the app is not pointed at it,
    # so introspecting it must not create any jobs in the manager.
    app = create_jobs_app(run_engine=False, workflows_dir=str(empty_wf))
    with TestClient(app) as c:
        before = c.get("/jobs").json()
        assert before == []
        # Re-resolving twice changes nothing, and /jobs is identical afterwards.
        c.get("/plan")
        c.get("/plan")
        assert c.get("/jobs").json() == before


def test_plan_introspect_empty_no_active_plan(db, empty_wf):
    """No plan + an empty workflows dir -> an empty job set (no plan_path)."""
    app = create_jobs_app(run_engine=False, workflows_dir=str(empty_wf))
    with TestClient(app) as c:
        body = c.get("/plan").json()
    assert body["plan_path"] is None
    assert body["workflows_dir"] == str(empty_wf)
    assert body["jobs"] == []
    assert body["failed"] == []


def test_plan_introspect_workflows_dir(db, tmp_path):
    """Jobs resolved from the workflows directory name their own file."""
    wf = tmp_path / "wf"
    wf.mkdir()
    (wf / "alpha.yaml").write_text(
        "source: {kind: command, config: {argv: [printf, x]}}\n"
        "schedule: {mode: once}\n",
        encoding="utf-8",
    )
    (wf / "beta.yaml").write_text(
        "id: 'beta-{{ n }}'\n"
        "matrix: [{n: 1}, {n: 2}]\n"
        "source: {kind: command, config: {argv: [printf, '{{ n }}']}}\n"
        "schedule: {mode: once}\n",
        encoding="utf-8",
    )
    app = create_jobs_app(run_engine=False, workflows_dir=str(wf))
    with TestClient(app) as c:
        body = c.get("/plan").json()

    by_id = {j["id"]: j for j in body["jobs"]}
    assert set(by_id) == {"alpha", "beta-1", "beta-2"}
    assert by_id["alpha"]["provenance"]["source"] == str(wf / "alpha.yaml")
    assert by_id["alpha"]["provenance"]["matrix"] is None
    assert by_id["beta-1"]["provenance"]["source"] == str(wf / "beta.yaml")
    assert by_id["beta-2"]["provenance"]["matrix"] == {
        "key": "matrix", "index": 1, "vars": {"n": 2},
    }


def test_plan_introspect_reports_failed(db, tmp_path, empty_wf):
    """A malformed plan job is reported in ``failed`` while valid jobs resolve."""
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        "jobs:\n"
        "  - id: good\n"
        "    source: {kind: command, config: {argv: [printf, ok]}}\n"
        "    schedule: {mode: once}\n"
        "  - id: bad\n"
        "    matrix: not-a-list\n",  # invalid matrix -> per-job failure
        encoding="utf-8",
    )
    app = create_jobs_app(
        run_engine=False, plan_path=str(plan), workflows_dir=str(empty_wf)
    )
    with TestClient(app) as c:
        body = c.get("/plan").json()
    assert [j["id"] for j in body["jobs"]] == ["good"]
    assert len(body["failed"]) == 1
    assert body["failed"][0]["id"] == "bad"
    assert "matrix" in body["failed"][0]["error"]


def test_plan_introspect_honors_only_selection(db, tmp_path, empty_wf):
    """``--only`` filters the introspected plan jobs exactly as startup would."""
    plan = _plan_tree(tmp_path)
    app = create_jobs_app(
        run_engine=False, plan_path=str(plan), workflows_dir=str(empty_wf),
        only=["feed-*"],
    )
    with TestClient(app) as c:
        body = c.get("/plan").json()
    assert {j["id"] for j in body["jobs"]} == {"feed-tech", "feed-science"}


def test_introspect_helper_only_no_match_is_empty_not_fatal(tmp_path):
    """A request-time ``--only`` that now matches nothing yields no jobs, not an error.

    Startup makes a no-match ``--only`` fatal, but the plan file can change after the
    server is up; the read-only helper must degrade to "no jobs selected" instead of
    raising (which would surface as a 500). Exercised directly, offline.
    """
    from ujin.jobs.app import _introspect_active_plan

    plan = _plan_tree(tmp_path)
    out = _introspect_active_plan(
        str(plan), str(tmp_path / "wf"), only=["does-not-exist-*"]
    )
    assert out["plan_path"] == str(plan)
    assert out["jobs"] == []
