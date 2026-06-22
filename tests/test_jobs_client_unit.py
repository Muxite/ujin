"""Unit tests for JobsClient covering methods not exercised by test_examples_and_sdk.

Uses a minimal synchronous httpx-client stub (no running server required).
"""
from __future__ import annotations

import pytest

from ujin.jobs.client import JobsClient


# --------------------------------------------------------------------------- #
# Minimal synchronous httpx stub
# --------------------------------------------------------------------------- #
class _FakeHttp:
    """Minimal synchronous drop-in for httpx.Client used by JobsClient._http.

    Pre-load responses via push(); they are consumed FIFO for every HTTP call
    (get / post / delete).  Tracks all requests in .calls for assertions.
    """

    def __init__(self):
        self._q: list = []
        self.calls: list[tuple[str, str]] = []
        self.closed = False

    def push(self, status: int, body, *, method: str = "GET", url: str = "http://fake/"):
        import httpx
        req = httpx.Request(method, url)
        resp = httpx.Response(status, json=body, request=req)
        self._q.append(resp)
        return self

    def _pop(self, method: str, path: str) -> "httpx.Response":
        self.calls.append((method, path))
        return self._q.pop(0)

    def get(self, path, **kw):
        return self._pop("GET", path)

    def post(self, path, **kw):
        return self._pop("POST", path)

    def delete(self, path, **kw):
        return self._pop("DELETE", path)

    def close(self):
        self.closed = True


@pytest.fixture
def jc() -> JobsClient:
    """JobsClient wired to an in-process fake HTTP transport."""
    client = JobsClient.__new__(JobsClient)
    client._http = _FakeHttp()
    return client


def http(jc: JobsClient) -> _FakeHttp:
    return jc._http  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# __init__ (exercises the real httpx.Client construction)
# --------------------------------------------------------------------------- #

def test_init_with_api_key():
    """__init__ wires X-API-Key header when api_key is provided."""
    jc = JobsClient("http://localhost:9999", api_key="secret", timeout=5.0)
    assert jc._http.headers.get("x-api-key") == "secret"
    jc.close()


def test_init_without_api_key():
    """__init__ omits X-API-Key header when no api_key."""
    jc = JobsClient("http://localhost:9999")
    assert "x-api-key" not in jc._http.headers
    jc.close()


# --------------------------------------------------------------------------- #
# Context-manager / lifecycle
# --------------------------------------------------------------------------- #

def test_context_manager(jc):
    with jc as c:
        assert c is jc
    assert http(jc).closed


def test_close(jc):
    jc.close()
    assert http(jc).closed


# --------------------------------------------------------------------------- #
# Job CRUD
# --------------------------------------------------------------------------- #

def test_get(jc):
    http(jc).push(200, {"id": "job-1", "name": "test"})
    result = jc.get("job-1")
    assert result["id"] == "job-1"
    assert http(jc).calls[-1] == ("GET", "/jobs/job-1")


def test_delete(jc):
    http(jc).push(200, {"ok": True})
    result = jc.delete("job-1")
    assert result["ok"] is True
    assert http(jc).calls[-1] == ("DELETE", "/jobs/job-1")


def test_create(jc):
    http(jc).push(201, {"id": "new-job"})
    job_id = jc.create({"name": "test", "source": {}, "sinks": []})
    assert job_id == "new-job"


def test_list(jc):
    http(jc).push(200, [{"id": "job-1"}, {"id": "job-2"}])
    jobs = jc.list()
    assert len(jobs) == 2


# --------------------------------------------------------------------------- #
# Job lifecycle
# --------------------------------------------------------------------------- #

def test_run(jc):
    http(jc).push(200, {"ok": True})
    result = jc.run("job-1")
    assert result["ok"] is True
    assert http(jc).calls[-1] == ("POST", "/jobs/job-1/run")


def test_pause(jc):
    http(jc).push(200, {"ok": True})
    result = jc.pause("job-1")
    assert result["ok"] is True
    assert http(jc).calls[-1] == ("POST", "/jobs/job-1/pause")


def test_resume(jc):
    http(jc).push(200, {"ok": True})
    result = jc.resume("job-1")
    assert result["ok"] is True
    assert http(jc).calls[-1] == ("POST", "/jobs/job-1/resume")


# --------------------------------------------------------------------------- #
# Runs / events
# --------------------------------------------------------------------------- #

def test_runs(jc):
    payload = [{"id": "run-1", "status": "done"}]
    http(jc).push(200, payload)
    runs = jc.runs("job-1")
    assert runs == payload
    assert http(jc).calls[-1] == ("GET", "/jobs/job-1/runs")


def test_runs_custom_limit(jc):
    http(jc).push(200, [])
    jc.runs("job-1", limit=10)
    assert http(jc).calls[-1] == ("GET", "/jobs/job-1/runs")


def test_events(jc):
    payload = [{"type": "started", "ts": 1}]
    http(jc).push(200, payload)
    events = jc.events("job-1")
    assert events == payload
    assert http(jc).calls[-1] == ("GET", "/jobs/job-1/events")


def test_events_custom_limit(jc):
    http(jc).push(200, [])
    jc.events("job-1", limit=5)
    assert http(jc).calls[-1] == ("GET", "/jobs/job-1/events")


# --------------------------------------------------------------------------- #
# Control-plane endpoints
# --------------------------------------------------------------------------- #

def test_health(jc):
    http(jc).push(200, {"status": "ok"})
    result = jc.health()
    assert result["status"] == "ok"
    assert http(jc).calls[-1] == ("GET", "/health")


def test_metrics(jc):
    http(jc).push(200, {"jobs_total": 3})
    result = jc.metrics()
    assert result["jobs_total"] == 3
    assert http(jc).calls[-1] == ("GET", "/metrics")


def test_kinds(jc):
    http(jc).push(200, {"sources": ["api", "command"]})
    result = jc.kinds()
    assert "sources" in result
    assert http(jc).calls[-1] == ("GET", "/kinds")


def test_reload_plugins(jc):
    http(jc).push(200, {"ok": True, "loaded": 2})
    result = jc.reload_plugins()
    assert result["ok"] is True
    assert http(jc).calls[-1] == ("POST", "/plugins/reload")


# --------------------------------------------------------------------------- #
# Error propagation
# --------------------------------------------------------------------------- #

def test_get_raises_on_404(jc):
    import httpx
    http(jc).push(404, {"error": "not found"})
    with pytest.raises(httpx.HTTPStatusError):
        jc.get("nonexistent")


def test_delete_raises_on_404(jc):
    import httpx
    http(jc).push(404, {"error": "not found"})
    with pytest.raises(httpx.HTTPStatusError):
        jc.delete("nonexistent")
