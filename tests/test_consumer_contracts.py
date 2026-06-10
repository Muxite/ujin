"""Consumer contract tripwires.

ujin is a git submodule of awork, hct-site, and wordle-max. Each test here pins
an exact surface one of those projects consumes (see docs/CONSUMERS.md). If a
refactor breaks one of these, a downstream submodule bump will break too —
change the consumer in lockstep or don't make the change.

  awork      — `from ujin import CallablePollable, PollEngine`;
               `ujin.fetch.obscura.{obscura_available, ObscuraFetcher}`
  hct-site   — POST :8901/scrape {url, mode, force_refresh} and the response
               fields its UjinClient parses; GET /health `status`;
               `from ujin import register` (+ @register.source/@register.sink)
  wordle-max — jobs-serve workflow YAML (source/transforms/sinks/schedule.cron),
               UJIN_WORKFLOWS_DIR / UJIN_JOBS_DB env vars
"""
from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from conftest import FakeBrowser, FakeHttp, FakeObscura  # noqa: E402


# ── awork: library imports ──────────────────────────────────────────────────

def test_awork_top_level_imports():
    from ujin import CallablePollable, PollEngine  # noqa: F401


def test_awork_obscura_imports():
    from ujin.fetch.obscura import ObscuraFetcher, obscura_available

    assert callable(obscura_available)
    fetcher = ObscuraFetcher(timeout_secs=5)
    assert hasattr(fetcher, "render_html")


def test_awork_pollengine_add_signature():
    """awork/backend/awork/watch.py: engine.add(CallablePollable(fn, key=...),
    base=..., on_change=cb) then engine.run()/sweep()."""
    from ujin import CallablePollable, PollEngine

    engine = PollEngine()
    pollable = CallablePollable(lambda: 1, key="watch")
    engine.add(pollable, base=30, on_change=lambda *a, **k: None)
    assert hasattr(engine, "run") and hasattr(engine, "sweep")


# ── hct-site: plugin registry ───────────────────────────────────────────────

def test_hct_register_decorators():
    """hct-site/backend/ujin_plugins/hct_publications.py uses
    @register.source(...) and @register.sink(...)."""
    from ujin import register

    @register.source("_contract_probe_source")
    def _src(cfg, ctx=None):  # pragma: no cover - factory body unused
        return None

    @register.sink("_contract_probe_sink")
    def _snk(cfg, ctx=None):  # pragma: no cover
        return None

    try:
        assert register.has("source", "_contract_probe_source")
        assert register.has("sink", "_contract_probe_sink")
    finally:
        register.clear_plugins()


def test_builtin_transforms_buildable_through_registry():
    """Regression: builtin transforms were registered via a default-arg lambda
    that swallowed the BuildContext, so every workflow transform failed with
    "'BuildContext' object is not callable" when built through the registry
    (the path JobManager actually uses)."""
    from ujin import register
    from ujin.registry import BuildContext

    for kind in ("select", "dedupe"):
        t = register.build_transform(kind, {"key": "url", "fields": ["url"]},
                                     BuildContext())
        assert t is not None


# ── hct-site: :8901 wire format ─────────────────────────────────────────────

class _ContractService:
    """Produces a fully-populated ScrapeResult, mirroring a real links scrape."""

    async def scrape(self, url, *, mode="links", force_refresh=False,
                     enrich_html_top_n=0, render="auto", actions=None):
        from ujin.extract.links import NormalizedLink
        from ujin.scrape.service import ScrapeResult

        return ScrapeResult(
            url=url, kind="links", fingerprint="fp123", fetched_at=1.0,
            cached=False, age_secs=0.0, used_renderer=False,
            strategy_used="http",
            links=[NormalizedLink(url="https://x.test/a",
                                  text="A sufficiently long headline")],
        )


@pytest.fixture
def scrape_client():
    from ujin.scrape.app import create_scrape_app
    from ujin.scrape.config import ScrapeConfig

    app = create_scrape_app(ScrapeConfig())
    client = TestClient(app)
    client.__enter__()
    app.state.service = _ContractService()
    yield client
    client.__exit__(None, None, None)


def test_hct_scrape_request_and_response_fields(scrape_client):
    """UjinClient.scrape() sends {url, mode, force_refresh} and reads:
    url, kind, fingerprint, used_renderer, strategy_used, article, links,
    structured. None of these may be renamed or removed."""
    r = scrape_client.post("/scrape", json={
        "url": "https://news.example.com/", "mode": "links",
        "force_refresh": False,
    })
    assert r.status_code == 200
    body = r.json()
    for field in ("url", "kind", "fingerprint", "used_renderer",
                  "strategy_used", "article", "links", "structured"):
        assert field in body, f"hct-site reads ScrapeResponse.{field}"
    link = body["links"][0]
    for field in ("url", "text"):
        assert field in link


def test_hct_health_has_status_ok(scrape_client):
    """UjinClient.health() expects GET /health with status == 'ok'."""
    r = scrape_client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# ── wordle-max: workflow file format + env vars ─────────────────────────────

WORDLE_MAX_STYLE_WORKFLOW = """\
source:
  kind: api
  config:
    url: "https://api.example.com/items"
    json_path: items
transforms:
  - kind: select
    config: { fields: [name, url] }
  - kind: dedupe
    config: { key: url }
sinks:
  - kind: webhook
    config: { url: "http://backend.test/api/ingest" }
schedule:
  cron: "0 */4 * * *"
"""


def test_wordlemax_workflow_yaml_loads(tmp_path, monkeypatch):
    """jobs-serve must keep accepting the workflow shape wordle-max ships
    (ingest/workflows/amazon-categories.yaml): source/transforms/sinks +
    schedule.cron, driven by UJIN_WORKFLOWS_DIR and UJIN_JOBS_DB."""
    from ujin.jobs.app import create_jobs_app

    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    (wf_dir / "amazon-categories.yaml").write_text(WORDLE_MAX_STYLE_WORKFLOW)
    monkeypatch.setenv("UJIN_WORKFLOWS_DIR", str(wf_dir))
    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))

    app = create_jobs_app(run_engine=False)
    with TestClient(app) as client:
        health = client.get("/health").json()
        assert health["workflows"]["loaded"] == ["amazon-categories"]
        assert health["workflows"]["failed"] == []

        jobs = client.get("/jobs").json()
        ids = [j["id"] for j in jobs]
        assert "amazon-categories" in ids

        spec = client.get("/jobs/amazon-categories").json()["spec"]
        assert spec["schedule"]["cron"] == "0 */4 * * *"
        assert [t["kind"] for t in spec["transforms"]] == ["select", "dedupe"]
        assert [s["kind"] for s in spec["sinks"]] == ["webhook"]


def test_wordlemax_jobs_surface_routes_exist(tmp_path, monkeypatch):
    """The :8902 routes wordle-max relies on must keep existing."""
    from ujin.jobs.app import create_jobs_app

    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    monkeypatch.delenv("UJIN_WORKFLOWS_DIR", raising=False)
    app = create_jobs_app(run_engine=False)
    paths = {r.path for r in app.routes}
    for p in ("/health", "/jobs", "/jobs/{job_id}", "/jobs/{job_id}/run",
              "/jobs/{job_id}/pause", "/jobs/{job_id}/resume",
              "/jobs/{job_id}/runs", "/jobs/{job_id}/events",
              "/jobs/{job_id}/content", "/jobs/{job_id}/results",
              "/metrics"):
        assert p in paths, f"wordle-max depends on {p}"
