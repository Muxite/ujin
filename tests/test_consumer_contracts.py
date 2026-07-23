"""Consumer contract tripwires.

ujin is a git submodule of awork, hct-site, and wordle-max. Each test here pins
an exact surface one of those projects consumes (see docs/CONSUMERS.md). If a
refactor breaks one of these, a downstream submodule bump will break too —
change the consumer in lockstep or don't make the change.

  awork      — `from ujin import CallablePollable, PollEngine`;
               `ujin.fetch.obscura.{obscura_available, ObscuraFetcher}`;
               `ujin.scrape.config.ScrapeConfig()`, `ujin.scrape.build.build_scrape_service`,
               `ujin.scrape.service.ScrapeService.scrape` (+ `.links`/`.article` on
               `ScrapeResult`); `ujin.sources.discover.discover_sources`;
               `ujin.jobs.build.build_job_manager` (primary entry point: replaces
               hand-wired JobStore+PollEngine+build_scrape_service+JobManager with
               one call) -> `(JobManager, JobStore, ScrapeService | None, aclose)`;
               `ujin.jobs.{JobManager, JobStore, JobSpec, SourceSpec, ScheduleSpec}`
               (backend/awork/apply/ujin_seam.py)
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


# ── awork: ujin_seam.py — scrape stack ──────────────────────────────────────

def test_awork_scrapeconfig_zero_args():
    """apply/ujin_seam.py builds ScrapeConfig() with zero required args (every
    field must keep a default)."""
    from ujin.scrape.config import ScrapeConfig

    cfg = ScrapeConfig()
    assert cfg.fetch_timeout_secs > 0
    assert cfg.http_timeout_secs > 0


async def test_awork_build_scrape_service_returns_service_components_aclose():
    """apply/ujin_seam.py: `service, comps, aclose = await build_scrape_service(cfg)`."""
    from ujin.scrape.build import ScrapeComponents, build_scrape_service
    from ujin.scrape.config import ScrapeConfig
    from ujin.scrape.service import ScrapeService

    service, comps, aclose = await build_scrape_service(ScrapeConfig())
    try:
        assert isinstance(service, ScrapeService)
        assert isinstance(comps, ScrapeComponents)
        assert callable(aclose)
    finally:
        await aclose()


def test_awork_scrapeservice_scrape_signature():
    """apply/ujin_seam.py calls service.scrape(url, mode="links"/"article",
    force_refresh=..., enrich_html_top_n=..., render=..., actions=...) — every
    keyword and its default must stay exactly this shape."""
    import inspect

    from ujin.scrape.service import ScrapeService

    params = inspect.signature(ScrapeService.scrape).parameters
    assert list(params)[:2] == ["self", "url"]
    for name, default in (
        ("mode", "links"),
        ("force_refresh", False),
        ("enrich_html_top_n", 0),
        ("render", "auto"),
        ("actions", None),
    ):
        assert name in params, f"ScrapeService.scrape lost the {name!r} kwarg"
        assert params[name].default == default


async def test_awork_scrapeservice_links_and_article_result_shape(html_corpus):
    """apply/ujin_seam.py reads ScrapeResult.links (list[NormalizedLink] with
    .url/.text) from mode="links" and .article (with .title/.text) from
    mode="article"."""
    from ujin.cache import HostPolicy, ScrapeCache
    from ujin.fetch.http import HttpResponse
    from ujin.scrape.config import ScrapeConfig
    from ujin.scrape.service import ScrapeService

    home = "https://news.example.com/"
    article_url = f"{home}2026/06/08/quantum-error-correction-milestone"
    rows = "".join(
        f'<article><h2><a href="{home}2026/06/09/story-{i}">'
        f"A sufficiently long headline number {i}</a></h2></article>"
        for i in range(8)
    )
    index_html = f"<html><body><main>{rows}</main></body></html>"

    http = FakeHttp({
        home: HttpResponse(url=home, status=200, body=index_html, final_url=home),
        article_url: HttpResponse(url=article_url, status=200,
                                  body=html_corpus["article"], final_url=article_url),
    })
    svc = ScrapeService(http=http, obscura=FakeObscura(), cache=ScrapeCache(),
                       policy=HostPolicy(cooldown_secs=60), config=ScrapeConfig())

    links_result = await svc.scrape(home, mode="links")
    assert links_result.links, "links mode must populate ScrapeResult.links"
    link = links_result.links[0]
    assert hasattr(link, "url") and hasattr(link, "text")

    article_result = await svc.scrape(article_url, mode="article")
    assert article_result.article is not None
    assert hasattr(article_result.article, "title")
    assert hasattr(article_result.article, "text")


# ── awork: ujin_seam.py — source discovery ──────────────────────────────────

async def test_awork_discover_sources_signature_and_shape():
    """apply/ujin_seam.py constructs its own transient HttpFetcher and calls
    discover_sources(http, homepage) -> DiscoveredSources{homepage, rss, sitemap}."""
    from ujin.sources.discover import DiscoveredSources, discover_sources

    home = "https://site.example.com/"
    found = await discover_sources(FakeHttp({}), home)
    assert isinstance(found, DiscoveredSources)
    assert found.homepage == home
    assert isinstance(found.rss, list)
    assert isinstance(found.sitemap, list)


# ── awork: ujin_seam.py — jobs control plane ────────────────────────────────

async def test_awork_build_job_manager_signature_and_shape(tmp_path):
    """apply/ujin_seam.py (post-refactor): `manager, store, service, aclose =
    await build_job_manager(db_path)` — the single call that replaces its old
    hand-wired JobStore + PollEngine + build_scrape_service + JobManager
    sequence. `service` is a ready `ScrapeService | None` (never raises when
    the scrape backend is unavailable); `hub` stays `None` by default (no
    FastAPI/WS layer needed for an in-process consumer); `aclose()` releases
    both the scrape stack and the store."""
    from ujin.jobs.build import build_job_manager
    from ujin.jobs.manager import JobManager
    from ujin.jobs.store import JobStore

    manager, store, service, aclose = await build_job_manager(tmp_path / "jobs.db")
    try:
        assert isinstance(manager, JobManager)
        assert isinstance(store, JobStore)
        assert manager.store is store
        assert manager.scrape_service is service
        assert manager.hub is None
        assert callable(aclose)
    finally:
        await aclose()


def test_awork_jobspec_and_sourcespec_construction():
    """apply/ujin_seam.py builds JobSpec(name=..., source=SourceSpec(kind="scrape",
    config={...})) and relies on transforms/sinks defaulting to [] and schedule
    defaulting to a ScheduleSpec()."""
    from ujin.jobs import JobSpec, ScheduleSpec, SourceSpec

    spec = JobSpec(
        name="awork-scrape-job",
        source=SourceSpec(kind="scrape", config={"url": "https://news.example.com/"}),
    )
    assert spec.transforms == []
    assert spec.sinks == []
    assert isinstance(spec.schedule, ScheduleSpec)
    assert spec.enabled is True


async def test_awork_jobmanager_create_and_run_now_scrape_source(tmp_path):
    """apply/ujin_seam.py: JobManager(engine, store, scrape_service=...).create(spec)
    -> JobHandle, then `await .run_now(job_id) -> PollResult | None`."""
    from ujin.engine import PollEngine
    from ujin.jobs import JobSpec, JobStore, SourceSpec
    from ujin.jobs.manager import JobHandle, JobManager
    from ujin.poll.base import PollResult
    from ujin.scrape.service import ScrapeResult

    class _FakeScrapeService:
        async def scrape(self, url, *, mode="links", force_refresh=False):
            return ScrapeResult(
                url=url, kind="links", fingerprint="fp-1", fetched_at=1.0,
                cached=False, age_secs=0.0, used_renderer=False,
                strategy_used="http",
            )

    engine = PollEngine()
    store = JobStore(tmp_path / "jobs.db")
    manager = JobManager(engine, store, scrape_service=_FakeScrapeService())

    spec = JobSpec(
        name="awork-scrape-job",
        source=SourceSpec(kind="scrape", config={"url": "https://news.example.com/",
                                                 "mode": "links"}),
    )
    handle = manager.create(spec)
    assert isinstance(handle, JobHandle)

    result = await manager.run_now(handle.spec.id)
    assert isinstance(result, PollResult)
    assert result.ok is True

    store.close()


def test_awork_jobstore_results_and_close(tmp_path):
    """apply/ujin_seam.py reads obtained payloads back via
    store.results(job_id, limit) then calls store.close()."""
    from ujin.jobs import JobStore

    store = JobStore(tmp_path / "jobs.db")
    store.record_result("job-1", ts=1.0, fingerprint="fp", payload={"links": []})
    rows = store.results("job-1", limit=50)
    assert rows and rows[0]["payload"] == {"links": []}
    store.close()


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
