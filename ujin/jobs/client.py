"""A thin Python client for the jobs control plane.

A convenience wrapper over the REST surface — handy for scripts and tests that
want to drive a running ``ujin jobs-serve`` instance without hand-rolling httpx
calls. Construction is dependency-light; ``httpx`` is imported lazily so importing
this module never requires it.

    from ujin.jobs.client import JobsClient

    jc = JobsClient("http://localhost:8902", api_key="...")  # api_key optional
    job_id = jc.create({
        "name": "crossref",
        "source": {"kind": "api", "config": {
            "url": "https://api.crossref.org/works?query=...",
            "json_path": "message.items"}},
        "transforms": [{"kind": "select", "config": {"fields": ["DOI", "title"]}}],
        "sinks": [{"kind": "jsonl", "config": {"path": "/data/crossref.jsonl"}}],
        "schedule": {"mode": "adaptive", "base": 3600, "min": 600, "max": 86400},
    })
    jc.run(job_id)
    print(jc.runs(job_id))
"""
from __future__ import annotations

from typing import Any


class JobsClient:
    def __init__(self, base_url: str = "http://localhost:8902",
                 *, api_key: str | None = None, timeout: float = 30.0):
        import httpx

        headers = {"X-API-Key": api_key} if api_key else {}
        self._http = httpx.Client(base_url=base_url.rstrip("/"),
                                  headers=headers, timeout=timeout)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "JobsClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- jobs -------------------------------------------------------------- #
    def create(self, spec: dict) -> str:
        r = self._http.post("/jobs", json=spec)
        r.raise_for_status()
        return r.json()["id"]

    def list(self) -> list[dict]:
        return self._get("/jobs")

    def get(self, job_id: str) -> dict:
        return self._get(f"/jobs/{job_id}")

    def delete(self, job_id: str) -> dict:
        r = self._http.delete(f"/jobs/{job_id}")
        r.raise_for_status()
        return r.json()

    def run(self, job_id: str) -> dict:
        return self._post(f"/jobs/{job_id}/run")

    def pause(self, job_id: str) -> dict:
        return self._post(f"/jobs/{job_id}/pause")

    def resume(self, job_id: str) -> dict:
        return self._post(f"/jobs/{job_id}/resume")

    def runs(self, job_id: str, limit: int = 50) -> list[dict]:
        return self._get(f"/jobs/{job_id}/runs", params={"limit": limit})

    def events(self, job_id: str, limit: int = 50) -> list[dict]:
        return self._get(f"/jobs/{job_id}/events", params={"limit": limit})

    # -- plane ------------------------------------------------------------- #
    def health(self) -> dict:
        return self._get("/health")

    def metrics(self) -> dict:
        return self._get("/metrics")

    def kinds(self) -> dict:
        return self._get("/kinds")

    def reload_plugins(self) -> dict:
        return self._post("/plugins/reload")

    # -- internal ---------------------------------------------------------- #
    def _get(self, path: str, **kw: Any) -> Any:
        r = self._http.get(path, **kw)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, **kw: Any) -> Any:
        r = self._http.post(path, **kw)
        r.raise_for_status()
        return r.json()
