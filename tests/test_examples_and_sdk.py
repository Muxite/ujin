"""M13: the shipped example plugin loads, the Crossref jobs.yaml parses, and the
SDK client drives a live app (over an in-process ASGI transport)."""
from __future__ import annotations

from pathlib import Path

import pytest

from ujin.plugins import load_plugins
from ujin.registry import register

_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean_registry():
    register.clear_plugins()
    yield
    register.clear_plugins()


def test_example_plugin_loads():
    status = load_plugins(str(_ROOT / "examples" / "plugins"))
    assert "hello_sink" in status["loaded"]
    assert register.has("sink", "hello")
    assert register.has("source", "ticker")


def test_crossref_yaml_parses_into_specs():
    pytest.importorskip("yaml")
    from ujin.jobs.app import _preload_specs

    specs = _preload_specs(str(_ROOT / "examples" / "jobs.crossref.yaml"))
    assert len(specs) == 1
    spec = specs[0]
    assert spec.source.kind == "api"
    assert spec.source.config["json_path"] == "message.items"
    assert [t.kind for t in spec.transforms] == ["select", "select", "dedupe"]
    assert spec.schedule.mode == "adaptive" and spec.schedule.base == 3600


def test_sdk_client_drives_live_app(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))

    from fastapi.testclient import TestClient

    from ujin.jobs.app import create_jobs_app
    from ujin.jobs.client import JobsClient

    app = create_jobs_app(run_engine=False)
    out = tmp_path / "o.jsonl"
    job = {
        "name": "via-sdk",
        "source": {"kind": "command", "config": {"argv": ["printf", "hi"]}},
        "sinks": [{"kind": "jsonl", "config": {"path": str(out)}}],
        "schedule": {"mode": "once"},
    }

    # TestClient is a sync httpx client that runs the app + its lifespan in-process;
    # inject it as the SDK's underlying client to exercise the real request paths.
    with TestClient(app) as tc:
        jc = JobsClient.__new__(JobsClient)
        jc._http = tc
        jid = jc.create(job)
        assert jc.run(jid)["ok"] is True
        assert any(j["id"] == jid for j in jc.list())
        assert len(jc.runs(jid)) == 1
        assert out.read_text().strip()
