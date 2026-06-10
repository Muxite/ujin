"""FastMCP server over ujin's scrape + jobs stacks.

Tools mirror the HTTP surface but call the services in-process:

    scrape_url, scrape_feed, discover_site, get_capabilities, get_metrics
    list_jobs, get_job, create_job, run_job, pause_job, resume_job,
    get_job_results

Configuration comes from the same env vars as the services
(UJIN_JOBS_DB, UJIN_WORKFLOWS_DIR is NOT read here — workflows belong to
jobs-serve; OBSCURA_BIN/OBSCURA_URL, UJIN_* scrape config).
"""
from __future__ import annotations

import dataclasses
import os
from typing import Any, Optional

try:
    from mcp.server.fastmcp import Context, FastMCP
except ModuleNotFoundError as exc:  # pragma: no cover
    raise RuntimeError(
        "ujin mcp server needs the 'mcp' extra: pip install 'ujin[mcp]'"
    ) from exc


def _json_safe(obj: Any) -> Any:
    """Dataclasses → dicts, recursively; leave plain JSON types alone."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _json_safe(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, bytes):
        return f"<{len(obj)} bytes>"
    return obj


class _Backend:
    """Owns the ScrapeService + JobManager for the server's lifetime."""

    def __init__(self) -> None:
        self.scrape_service: Any = None
        self._scrape_close: Any = None
        self.manager: Any = None
        self._store: Any = None

    async def start(self) -> None:
        from ujin.scrape.build import build_scrape_service
        from ujin.scrape.config import ScrapeConfig

        self.scrape_service, _comps, self._scrape_close = (
            await build_scrape_service(ScrapeConfig.from_env())
        )

        # Same persistence default as jobs-serve so the MCP server sees (and
        # can drive) the same job set.
        from ujin.engine import PollEngine
        from ujin.jobs.manager import JobManager
        from ujin.jobs.store import JobStore

        self._store = JobStore(os.environ.get("UJIN_JOBS_DB", "./ujin-jobs.db"))
        self.manager = JobManager(PollEngine(), self._store,
                                  scrape_service=self.scrape_service)
        self.manager.load_from_store()

    async def stop(self) -> None:
        if self._scrape_close is not None:
            await self._scrape_close()
        if self._store is not None:
            self._store.close()


def create_mcp_server(backend: Optional[_Backend] = None) -> FastMCP:
    """Build the FastMCP instance. A pre-started ``backend`` can be injected
    (tests); otherwise one is created and started on first use."""
    owned = backend is None
    state = backend or _Backend()

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(_server: FastMCP):
        if owned:
            await state.start()
        try:
            yield state
        finally:
            if owned:
                await state.stop()

    mcp = FastMCP(
        "ujin",
        instructions=(
            "ujin is a scraper-poller. Use scrape_url for one-shot page "
            "extraction (links/article/structured); check get_capabilities "
            "to see which render backends are available before pinning "
            "render='obscura' or 'browser'. Jobs are persistent polling "
            "pipelines (source -> transforms -> sinks) — create_job + "
            "run_job + get_job_results."
        ),
        lifespan=lifespan,
    )

    # ── scrape tools ─────────────────────────────────────────────────────────

    @mcp.tool()
    async def scrape_url(
        url: str,
        mode: str = "links",
        render: str = "auto",
        force_refresh: bool = False,
    ) -> dict:
        """Scrape one page and extract content.

        mode: 'links' (headline links), 'article' (body text), 'structured'
        (opengraph/json-ld), 'combined' (RSS+HTML merged), 'auto'.
        render: 'auto' (HTTP, escalate to obscura when thin), 'http',
        'obscura', or 'browser' (needs playwright/selenium installed).
        Results are cached; force_refresh bypasses the cache.
        """
        result = await state.scrape_service.scrape(
            url, mode=mode, render=render, force_refresh=force_refresh
        )
        return _json_safe(result)

    @mcp.tool()
    async def scrape_feed(url: str) -> dict:
        """Parse an RSS/Atom feed into items (url, title, summary, published)."""
        from ujin.sources.rss import parse_feed

        items = await parse_feed(url)
        return {"items": [_json_safe(i) for i in items]}

    @mcp.tool()
    async def discover_site(homepage: str) -> dict:
        """Find a site's RSS feeds and sitemaps (link tags, robots.txt,
        well-known paths). Useful before deciding how to poll a site."""
        from ujin.sources.discover import discover_sources

        # ScrapeComponents exposes the shared HttpFetcher on the service
        http = state.scrape_service._http
        found = await discover_sources(http, homepage)
        return _json_safe(found)

    @mcp.tool()
    def get_capabilities() -> dict:
        """The fetch-backend capability matrix (http/obscura/playwright/
        selenium) with live availability — consult before pinning render=."""
        from ujin.fetch.capabilities import capabilities_snapshot

        return {"backends": capabilities_snapshot()}

    @mcp.tool()
    def get_metrics() -> dict:
        """Per-host fetch counters and latency percentiles for this session."""
        return _json_safe(state.scrape_service._metrics.snapshot())

    # ── job tools ────────────────────────────────────────────────────────────

    @mcp.tool()
    def list_jobs() -> list[dict]:
        """All registered jobs with state, schedule, poll/change counters."""
        return state.manager.list()

    @mcp.tool()
    def get_job(job_id: str) -> dict:
        """One job's full spec + runtime summary."""
        handle = state.manager.get(job_id)
        if handle is None:
            return {"error": f"no job {job_id!r}"}
        return {"spec": handle.spec.to_dict(), **handle.summary()}

    @mcp.tool()
    def create_job(spec: dict) -> dict:
        """Create a persistent job. spec: {name, source: {kind, config},
        transforms?: [{kind, config}], sinks?: [{kind, config}],
        schedule?: {mode: adaptive|cron|once, base?, cron?}}.
        Source kinds: http, rss, api, command, site, scrape, browser."""
        from ujin.jobs.model import JobSpec

        job_spec = JobSpec.from_dict(spec)
        handle = state.manager.create(job_spec)
        return handle.summary()

    @mcp.tool()
    async def run_job(job_id: str) -> dict:
        """Poll a job once right now; returns ok/changed/fingerprint."""
        result = await state.manager.run_now(job_id)
        if result is None:
            return {"error": f"no job {job_id!r}"}
        return {"ok": result.ok, "changed": result.changed,
                "fingerprint": result.fingerprint, "error": result.error}

    @mcp.tool()
    def pause_job(job_id: str) -> dict:
        """Disable a job's schedule (its state and counters survive)."""
        return {"paused": state.manager.pause(job_id)}

    @mcp.tool()
    def resume_job(job_id: str) -> dict:
        """Re-enable a paused job."""
        return {"resumed": state.manager.resume(job_id)}

    @mcp.tool()
    def get_job_results(job_id: str, limit: int = 20) -> dict:
        """Recent changed-poll payloads for a job (the collect buffer)."""
        if state.manager.get(job_id) is None:
            return {"error": f"no job {job_id!r}"}
        results = state.manager.store.results(job_id, limit=limit)
        return {"results": _json_safe(results)}

    return mcp


def serve(transport: str = "stdio", *, host: str = "127.0.0.1",
          port: int = 8903) -> None:
    """Run the MCP server. stdio by default; 'http' for streamable HTTP."""
    mcp = create_mcp_server()
    if transport == "http":
        mcp.settings.host = host
        mcp.settings.port = port
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")
