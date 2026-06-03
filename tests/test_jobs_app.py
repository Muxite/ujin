"""Unified jobs app: CRUD, run-now, pause/resume, persistence reload, sinks.

Uses run_engine=False (no background loop) and a deterministic `command` source,
so the pipeline is driven explicitly via /jobs/{id}/run.
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from ujin.jobs.app import create_jobs_app  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "jobs.db"
    monkeypatch.setenv("UJIN_JOBS_DB", str(path))
    return path


def _job(out_path) -> dict:
    return {
        "name": "printer",
        "source": {"kind": "command", "config": {"argv": ["printf", "hello"]}},
        "sinks": [
            {"kind": "jsonl", "config": {"path": str(out_path)}},
            {"kind": "sqlite", "config": {}},
        ],
        "schedule": {"mode": "once"},
    }


def test_create_run_and_sinks(db, tmp_path):
    out = tmp_path / "events.jsonl"
    app = create_jobs_app(run_engine=False)
    with TestClient(app) as c:
        assert c.get("/health").json()["ok"] is True

        job_id = c.post("/jobs", json=_job(out)).json()["id"]
        assert any(j["id"] == job_id for j in c.get("/jobs").json())

        run = c.post(f"/jobs/{job_id}/run").json()
        assert run["ok"] is True and run["changed"] is True

        # jsonl sink wrote a line; sqlite sink persisted the event
        lines = out.read_text().strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["job_id"] == job_id

        runs = c.get(f"/jobs/{job_id}/runs").json()
        assert len(runs) == 1 and runs[0]["changed"] is True
        events = c.get(f"/jobs/{job_id}/events").json()
        assert len(events) == 1 and events[0]["job_id"] == job_id


def test_bad_kind_returns_400(db):
    app = create_jobs_app(run_engine=False)
    with TestClient(app) as c:
        r = c.post("/jobs", json={"name": "x", "source": {"kind": "bogus"}})
        assert r.status_code == 400


def test_pause_resume_delete(db, tmp_path):
    out = tmp_path / "e.jsonl"
    app = create_jobs_app(run_engine=False)
    with TestClient(app) as c:
        job_id = c.post("/jobs", json=_job(out)).json()["id"]
        assert c.post(f"/jobs/{job_id}/pause").status_code == 200
        assert c.get(f"/jobs/{job_id}").json()["enabled"] is False
        assert c.post(f"/jobs/{job_id}/resume").status_code == 200
        assert c.get(f"/jobs/{job_id}").json()["enabled"] is True
        assert c.delete(f"/jobs/{job_id}").status_code == 200
        assert c.get(f"/jobs/{job_id}").status_code == 404


def test_jobs_persist_across_restart(db, tmp_path):
    out = tmp_path / "e.jsonl"
    # first "process": create a job, then tear the app down
    app1 = create_jobs_app(run_engine=False)
    with TestClient(app1) as c:
        job_id = c.post("/jobs", json=_job(out)).json()["id"]
    # second "process": same UJIN_JOBS_DB -> job reloaded from store
    app2 = create_jobs_app(run_engine=False)
    with TestClient(app2) as c:
        ids = [j["id"] for j in c.get("/jobs").json()]
        assert job_id in ids


_PLUGIN = '''
from ujin import register
from ujin.poll.base import PollResult

@register.source("counter")
def make(cfg):
    class _C:
        key = "counter"
        async def poll(self, prev):
            return PollResult(ok=True, changed=True, fingerprint="c1",
                              payload={"v": cfg.get("v", 1)})
    return _C()
'''


def test_plugin_source_via_api(db, tmp_path, monkeypatch):
    from ujin.registry import register as _reg

    plugdir = tmp_path / "plugins"
    plugdir.mkdir()
    (plugdir / "counter.py").write_text(_PLUGIN)
    monkeypatch.setenv("UJIN_PLUGINS_DIR", str(plugdir))
    _reg.clear_plugins()
    try:
        out = tmp_path / "p.jsonl"
        app = create_jobs_app(run_engine=False)
        with TestClient(app) as c:
            assert "counter" in c.get("/health").json()["plugins"]["loaded"]
            assert "counter" in c.get("/kinds").json()["source"]
            job = {
                "name": "plug",
                "source": {"kind": "plugin:counter", "config": {"v": 42}},
                "sinks": [{"kind": "jsonl", "config": {"path": str(out)}}],
                "schedule": {"mode": "once"},
            }
            jid = c.post("/jobs", json=job).json()["id"]
            run = c.post(f"/jobs/{jid}/run").json()
            assert run["ok"] and run["changed"]
            assert json.loads(out.read_text().splitlines()[0])["payload"] == {"v": 42}
    finally:
        _reg.clear_plugins()


def test_global_ws_streams_change(db, tmp_path):
    out = tmp_path / "e.jsonl"
    app = create_jobs_app(run_engine=False)
    with TestClient(app) as c:
        job_id = c.post("/jobs", json=_job(out)).json()["id"]
        with c.websocket_connect("/jobs/events") as ws:
            c.post(f"/jobs/{job_id}/run")
            event = ws.receive_json()
            assert event["event"] == "change"
            assert event["job_id"] == job_id
