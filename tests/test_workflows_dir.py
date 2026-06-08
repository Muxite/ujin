"""Workflow directory loading (setup) + the collect/hand-out endpoints.

A "workflow" is a job whose id is derived from its filename stem, loaded from a
mounted directory. After it runs, the obtained data is handed back via
/jobs/{id}/content (latest) and /jobs/{id}/results (recent buffer).
"""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
import yaml  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from ujin.jobs.app import create_jobs_app  # noqa: E402
from ujin.jobs.store import RESULTS_CAP, JobStore  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    return tmp_path


def _write_workflow(d, stem: str, msg: str) -> None:
    spec = {
        "source": {"kind": "command", "config": {"argv": ["printf", msg]}},
        "sinks": [{"kind": "sqlite", "config": {}}],
        "schedule": {"mode": "once"},
    }
    (d / f"{stem}.yaml").write_text(yaml.safe_dump(spec))


def test_dir_loads_with_filename_ids(db, tmp_path):
    wf = tmp_path / "workflows"
    wf.mkdir()
    _write_workflow(wf, "alpha", "a")
    _write_workflow(wf, "beta", "b")

    app = create_jobs_app(run_engine=False, workflows_dir=str(wf))
    with TestClient(app) as c:
        health = c.get("/health").json()
        assert set(health["workflows"]["loaded"]) == {"alpha", "beta"}
        ids = {j["id"] for j in c.get("/jobs").json()}
        assert {"alpha", "beta"} <= ids
        # the workflow id == the filename stem
        assert c.get("/jobs/alpha").json()["name"] == "alpha"


def test_reload_is_idempotent(db, tmp_path):
    wf = tmp_path / "workflows"
    wf.mkdir()
    _write_workflow(wf, "alpha", "a")

    # first "process"
    with TestClient(create_jobs_app(run_engine=False, workflows_dir=str(wf))) as c:
        first = [j["id"] for j in c.get("/jobs").json()]
    # second "process": same db + same files -> same single workflow, no dupes
    with TestClient(create_jobs_app(run_engine=False, workflows_dir=str(wf))) as c:
        second = [j["id"] for j in c.get("/jobs").json()]
    assert first == second == ["alpha"]


def test_content_and_results_handout(db, tmp_path):
    wf = tmp_path / "workflows"
    wf.mkdir()
    _write_workflow(wf, "alpha", "hello")

    app = create_jobs_app(run_engine=False, workflows_dir=str(wf))
    with TestClient(app) as c:
        # before any run: known job, but nothing obtained yet
        pre = c.get("/jobs/alpha/content").json()
        assert pre["id"] == "alpha" and pre["payload"] is None

        run = c.post("/jobs/alpha/run").json()
        assert run["ok"] and run["changed"]

        content = c.get("/jobs/alpha/content").json()
        assert content["ok"] is True and content["payload"] is not None

        results = c.get("/jobs/alpha/results").json()
        assert len(results) == 1
        assert results[0]["fingerprint"] == content["fingerprint"]

        # unknown workflow -> 404 on both surfaces
        assert c.get("/jobs/nope/content").status_code == 404
        assert c.get("/jobs/nope/results").status_code == 404


def test_results_buffer_is_capped(tmp_path):
    store = JobStore(tmp_path / "s.db")
    try:
        for i in range(RESULTS_CAP + 10):
            store.record_result("j", ts=float(i), fingerprint=f"fp{i}",
                                 payload={"i": i})
        rows = store.results("j", limit=1000)
        assert len(rows) == RESULTS_CAP
        # newest first; oldest 10 pruned
        assert rows[0]["payload"] == {"i": RESULTS_CAP + 9}
        assert rows[-1]["payload"] == {"i": 10}
    finally:
        store.close()
