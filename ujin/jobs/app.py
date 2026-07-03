"""FastAPI app for the unified job control plane (default port :8902).

One service, one ``Job`` abstraction: source -> transforms -> sinks on a schedule.
Jobs are durable (sqlite :class:`JobStore`), reloaded on startup, and driven by a
single :class:`ujin.engine.PollEngine` (adaptive) plus a cron loop. A ``scrape``
source is backed by the same :class:`ScrapeService` wiring the :8901 app uses.

Endpoints::

  GET    /health
  GET    /jobs                  list job summaries
  POST   /jobs                  create a job (JobCreate) -> {id}
  GET    /jobs/{id}             full spec + runtime state
  DELETE /jobs/{id}
  POST   /jobs/{id}/run         run now (one-shot poll + pipeline)
  POST   /jobs/{id}/pause
  POST   /jobs/{id}/resume
  GET    /jobs/{id}/runs        recent run history
  GET    /jobs/{id}/events      recent persisted events (sqlite sink)
  WS     /jobs/events           live stream of change events

Needs the ``jobs`` extra (fastapi, uvicorn, websockets, pydantic, scrape stack).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager
from typing import Any

# Imported at module level so FastAPI can resolve the handler's `req: JobCreate`
# annotation (this module uses `from __future__ import annotations`, which turns
# annotations into strings resolved against module globals). Guarded so the
# package still imports without the jobs extra.
try:
    from fastapi import WebSocket  # for the WS handler annotation

    from .api_models import JobCreate
except ModuleNotFoundError:  # pragma: no cover - jobs extra missing
    JobCreate = None  # type: ignore[assignment,misc]
    WebSocket = None  # type: ignore[assignment,misc]

log = logging.getLogger("ujin.jobs.app")

# ``${VAR}`` / ``${VAR:-default}`` references in a workflow/job file, expanded
# against the environment at load time. Lets a mounted file reference secrets
# (an ingest token, a backend URL) without committing them. An unset variable
# with no default expands to "" (and logs once), mirroring shell semantics.
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand_env(text: str) -> str:
    """Expand ``${VAR}`` / ``${VAR:-default}`` in *text* from os.environ."""
    def _sub(m: "re.Match[str]") -> str:
        name, default = m.group(1), m.group(2)
        val = os.environ.get(name)
        if val is None:
            if default is None:
                log.warning("workflow env var %s unset; expanding to empty", name)
                return ""
            return default
        return val

    return _ENV_REF.sub(_sub, text)


class WorkflowIncludeError(Exception):
    """A ``defaults:``/``include:``/``use:`` directive could not be resolved.

    Raised on a missing fragment file, a cyclic include chain, or a malformed
    directive. The directory loader catches this (logs + skips the file); the
    explicit ``--config`` preload path lets it propagate (fail fast on a typo).
    """


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base*, returning a new dict.

    Nested mappings merge key-by-key; any non-mapping value (list, scalar) in
    *override* replaces the one in *base* wholesale. Neither input is mutated —
    this is how defaults/fragments layer under a job without clobbering siblings.
    """
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _pop_includes(mapping: dict) -> list:
    """Pop ``include:``/``use:`` from *mapping* → ordered list of fragment refs.

    Both keys are accepted (``use`` is an alias for ``include``); each may be a
    single path string or a list of them. ``include`` entries are applied before
    ``use`` entries when both are present.
    """
    refs: list = []
    for key in ("include", "use"):
        val = mapping.pop(key, None)
        if val is None:
            continue
        if isinstance(val, str):
            refs.append(val)
        elif isinstance(val, (list, tuple)):
            refs.extend(val)
        else:
            raise WorkflowIncludeError(
                f"{key!r} must be a fragment path or list of paths, "
                f"got {type(val).__name__}"
            )
    return refs


def _resolve_fragment_path(ref: str, including_file):
    """Resolve a fragment *ref* to a path, *workflows dir first, then sibling*.

    Resolution order for a relative *ref*: ``$UJIN_WORKFLOWS_DIR/<ref>`` (if the
    env var is set), then ``<including_file>.parent/<ref>``. The first existing
    candidate wins; if neither exists the sibling candidate is returned so the
    "not found" error names a sensible location. An absolute *ref* is used as-is.
    """
    from pathlib import Path

    p = Path(ref)
    if p.is_absolute():
        # Normalise so a ``..``-crossing include collapses to a stable key and
        # the cycle guard cannot recurse into an OS path-length failure.
        return Path(os.path.normpath(str(p)))
    candidates = []
    wf_dir = os.environ.get("UJIN_WORKFLOWS_DIR")
    if wf_dir:
        candidates.append(Path(os.path.normpath(str(Path(wf_dir) / ref))))
    candidates.append(
        Path(os.path.normpath(str(Path(including_file).parent / ref)))
    )
    for c in candidates:
        if c.is_file():
            return c
    return candidates[-1]


def _load_fragment(ref: str, including_file, chain: list) -> dict:
    """Load fragment *ref* into a dict, resolving its own nested includes.

    *chain* holds the resolved paths currently being loaded so a fragment that
    (transitively) includes itself raises instead of recursing forever. A
    fragment is a partial job mapping; its own ``include:``/``use:`` are merged
    *beneath* its inline keys (more-specific wins), matching whole-job precedence.
    """
    import yaml

    resolved = _resolve_fragment_path(ref, including_file)
    key = str(resolved)
    if key in chain:
        cycle = " -> ".join([*chain, key])
        raise WorkflowIncludeError(f"cyclic include detected: {cycle}")
    if not resolved.is_file():
        raise WorkflowIncludeError(
            f"include {ref!r} not found (looked in $UJIN_WORKFLOWS_DIR and "
            f"{resolved.parent})"
        )
    frag = yaml.safe_load(_expand_env(resolved.read_text(encoding="utf-8"))) or {}
    if not isinstance(frag, dict):
        raise WorkflowIncludeError(
            f"include {ref!r} must be a mapping, got {type(frag).__name__}"
        )
    frag = dict(frag)
    nested = _pop_includes(frag)
    merged: dict = {}
    for nref in nested:
        merged = _deep_merge(merged, _load_fragment(nref, resolved, [*chain, key]))
    return _deep_merge(merged, frag)


def _file_directives(data):
    """Split file-level ``defaults:`` + ``include:``/``use:`` off *data*.

    Returns ``(defaults, includes, remaining)``. A non-mapping file (a bare list
    of jobs) carries no file-level directives. The remaining mapping still holds
    the job body / ``jobs:`` list.
    """
    if not isinstance(data, dict):
        return {}, [], data
    data = dict(data)
    defaults = data.pop("defaults", None) or {}
    includes = _pop_includes(data)
    return defaults, includes, data


def _resolve_job(raw, defaults: dict, file_includes: list, including_file) -> dict:
    """Layer ``defaults < fragments < inline job keys`` into one job dict.

    *file_includes* apply to every job in the file; the job's own
    ``include:``/``use:`` apply on top of them (closer to the job), and the
    inline keys win over everything. Returns a fresh dict ready for
    :meth:`JobSpec.from_dict`.
    """
    job = dict(raw)
    job_includes = _pop_includes(job)
    merged: dict = dict(defaults)
    for ref in (*file_includes, *job_includes):
        merged = _deep_merge(merged, _load_fragment(ref, including_file, []))
    return _deep_merge(merged, job)


def _preload_specs(path: str) -> list:
    """Parse a jobs.yaml (top-level list, or {jobs: [...]}) into JobSpecs.

    Honours the same file-level ``defaults:`` / ``include:`` resolution as the
    workflows-dir loader so both entry points behave consistently.
    """
    import yaml

    from pathlib import Path

    from .model import JobSpec

    text = _expand_env(open(path, encoding="utf-8").read())
    data = yaml.safe_load(text) or {}
    defaults, file_includes, data = _file_directives(data)
    raw = data.get("jobs", data) if isinstance(data, dict) else data
    file = Path(path)
    return [
        JobSpec.from_dict(_resolve_job(d, defaults, file_includes, file))
        for d in (raw or [])
    ]


def _specs_from_workflow_file(path) -> list:
    """Parse one workflow file into JobSpecs, deriving stable ids from the stem.

    A workflow file is the same declarative shape as a job. The filename stem is
    the **workflow id** (and default name) so the same file maps to the same
    workflow across restarts/redeploys — unless the file sets an explicit ``id``.
    A file may also hold a list / ``{jobs: [...]}``; entries without an ``id``
    fall back to ``<stem>-<index>`` to stay deterministic.
    """
    import yaml

    from .model import JobSpec

    from pathlib import Path

    path = Path(path)
    stem = path.stem
    data = yaml.safe_load(_expand_env(open(path, encoding="utf-8").read())) or {}

    # File-level `defaults:` + `include:`/`use:` (deep-merged under every job).
    defaults, file_includes, data = _file_directives(data)

    # Single-job mapping (no top-level `jobs:` list) -> the whole file is one job.
    if isinstance(data, dict) and "jobs" not in data:
        d = _resolve_job(data, defaults, file_includes, path)
        d.setdefault("id", stem)
        d.setdefault("name", stem)
        return [JobSpec.from_dict(d)]

    raw = data.get("jobs", data) if isinstance(data, dict) else data
    raw = raw or []
    specs = []
    for i, entry in enumerate(raw):
        d = _resolve_job(entry, defaults, file_includes, path)
        d.setdefault("id", stem if len(raw) == 1 else f"{stem}-{i}")
        d.setdefault("name", d["id"])
        specs.append(JobSpec.from_dict(d))
    return specs


def _load_workflows_dir(path: str, *, failed: list | None = None) -> list:
    """Load every ``*.yaml``/``*.yml`` workflow file in *path* (sorted).

    A file that fails to parse or resolve (bad YAML, missing/cyclic
    ``include:``) is logged and skipped rather than crashing the server; pass a
    ``failed`` list to also collect ``{"file", "error"}`` records for ``/health``.
    Fragment files live in subdirectories, so the top-level glob never loads them.
    """
    from pathlib import Path

    root = Path(path)
    if not root.is_dir():
        return []
    specs = []
    for fp in sorted(root.glob("*.y*ml")):
        try:
            specs.extend(_specs_from_workflow_file(fp))
        except Exception as exc:  # noqa: BLE001
            log.warning("skipping workflow file %s: %s", fp, exc)
            if failed is not None:
                failed.append({"file": str(fp), "error": str(exc)})
    return specs


def create_jobs_app(
    config_path: str | None = None,
    *,
    workflows_dir: str | None = None,
    scrape_config: Any = None,
    run_engine: bool = True,
) -> Any:
    try:
        from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise RuntimeError(
            "ujin jobs service needs the 'jobs' extra: pip install 'ujin[jobs]'"
        ) from exc

    from ujin.engine import PollEngine
    from ujin.service import _Hub

    from .manager import JobManager, UnknownKind
    from .store import JobStore

    db_path = os.environ.get("UJIN_JOBS_DB", "./ujin-jobs.db")
    wf_dir = workflows_dir or os.environ.get("UJIN_WORKFLOWS_DIR", "/workflows")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = JobStore(db_path)
        hub = _Hub()
        engine = PollEngine()

        # Shared scrape stack for `scrape` sources (optional — degrade if extras
        # or network stack are unavailable).
        scrape_service = None
        scrape_close = None
        try:
            from ujin.scrape.build import build_scrape_service
            from ujin.scrape.config import ScrapeConfig

            cfg = scrape_config or ScrapeConfig.from_env()
            scrape_service, _comps, scrape_close = await build_scrape_service(cfg)
        except Exception as exc:  # noqa: BLE001
            log.warning("scrape source backend unavailable: %s", exc)

        # Load operator plugins BEFORE reloading jobs, so persisted jobs that
        # reference plugin:* kinds resolve.
        from ujin.plugins import load_plugins

        plugin_status = load_plugins()
        app.state.plugins = plugin_status

        manager = JobManager(engine, store, hub=hub, scrape_service=scrape_service)
        manager.load_from_store()
        if config_path:
            for spec in _preload_specs(config_path):
                try:
                    manager.create(spec)
                except Exception as exc:  # noqa: BLE001
                    log.warning("preload job %s failed: %s", spec.name, exc)

        # "Setup" phase: register every workflow file in the mounted directory.
        # Ids are filename-derived, so re-loading upserts the same workflow rather
        # than duplicating it. A bad file is reported, not fatal.
        wf_status: dict[str, list] = {"dir": wf_dir, "loaded": [], "failed": []}
        file_failures: list = []
        for spec in _load_workflows_dir(wf_dir, failed=file_failures):
            try:
                manager.create(spec)
                wf_status["loaded"].append(spec.id)
            except Exception as exc:  # noqa: BLE001
                log.warning("workflow %s failed: %s", spec.id, exc)
                wf_status["failed"].append({"id": spec.id, "error": str(exc)})
        wf_status["failed"].extend(file_failures)
        app.state.workflows = wf_status

        app.state.engine = engine
        app.state.store = store
        app.state.hub = hub
        app.state.manager = manager

        tasks: list[asyncio.Task] = []
        if run_engine:
            tasks.append(asyncio.create_task(engine.run()))
            tasks.append(asyncio.create_task(manager.cron_loop()))
        log.info("ujin jobs service ready (%d job(s))", len(manager.jobs))
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                try:
                    await t
                except asyncio.CancelledError:
                    pass
            if scrape_close is not None:
                try:
                    await scrape_close()
                except Exception:  # noqa: BLE001
                    pass
            store.close()

    app = FastAPI(title="ujin-jobs", version="0.4.0", lifespan=lifespan)

    def _mgr(app) -> Any:  # pragma: no cover -- defined but unused; kept for external tooling
        return app.state.manager

    @app.get("/health")
    def health() -> dict[str, Any]:
        m = app.state.manager
        return {"ok": True, "status": "ok", "service": "ujin-jobs",
                "jobs": len(m.jobs),
                "plugins": getattr(app.state, "plugins", {"loaded": [], "failed": []}),
                "workflows": getattr(app.state, "workflows",
                                     {"dir": wf_dir, "loaded": [], "failed": []})}

    @app.get("/kinds")
    def kinds() -> dict[str, Any]:
        from ujin.registry import register

        return {c: register.available(c) for c in ("source", "transform", "sink")}

    @app.get("/metrics")
    def metrics() -> dict[str, Any]:
        m = app.state.manager
        handles = list(m.jobs.values())
        per_job = [h.summary() for h in handles]
        return {
            "engine": app.state.engine.stats(),
            "totals": {
                "jobs": len(handles),
                "enabled": sum(1 for h in handles if h.spec.enabled),
                "polls": sum(h.target.polls for h in handles),
                "changes": sum(h.target.changes for h in handles),
                "open_circuits": sum(
                    1 for h in handles if h.target.breaker.state == "open"
                ),
            },
            "plugins": getattr(app.state, "plugins", {"loaded": [], "failed": []}),
            "jobs": per_job,
        }

    @app.post("/plugins/reload")
    def plugins_reload() -> dict[str, Any]:
        from ujin.plugins import load_plugins
        from ujin.registry import register

        register.clear_plugins()
        status = load_plugins()
        app.state.plugins = status
        return status

    @app.get("/jobs")
    def list_jobs() -> list[dict[str, Any]]:
        return app.state.manager.list()

    @app.post("/jobs")
    def create_job(req: JobCreate) -> dict[str, Any]:
        spec = req.to_spec()
        try:
            app.state.manager.create(spec)
        except UnknownKind as exc:
            raise HTTPException(400, str(exc))
        return {"id": spec.id}

    @app.get("/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, Any]:
        handle = app.state.manager.get(job_id)
        if handle is None:
            raise HTTPException(404, f"no job {job_id!r}")
        return {"spec": handle.spec.to_dict(), **handle.summary()}

    @app.delete("/jobs/{job_id}")
    def delete_job(job_id: str) -> dict[str, Any]:
        if not app.state.manager.delete(job_id):
            raise HTTPException(404, f"no job {job_id!r}")
        return {"removed": job_id}

    @app.post("/jobs/{job_id}/run")
    async def run_job(job_id: str) -> dict[str, Any]:
        result = await app.state.manager.run_now(job_id)
        if result is None:
            raise HTTPException(404, f"no job {job_id!r}")
        return {"ok": result.ok, "changed": result.changed,
                "fingerprint": result.fingerprint, "error": result.error}

    @app.post("/jobs/{job_id}/pause")
    def pause_job(job_id: str) -> dict[str, Any]:
        if not app.state.manager.pause(job_id):
            raise HTTPException(404, f"no job {job_id!r}")
        return {"paused": job_id}

    @app.post("/jobs/{job_id}/resume")
    def resume_job(job_id: str) -> dict[str, Any]:
        if not app.state.manager.resume(job_id):
            raise HTTPException(404, f"no job {job_id!r}")
        return {"resumed": job_id}

    @app.get("/jobs/{job_id}/runs")
    def job_runs(job_id: str, limit: int = 50) -> list[dict[str, Any]]:
        if app.state.manager.get(job_id) is None:
            raise HTTPException(404, f"no job {job_id!r}")
        return app.state.store.runs(job_id, limit=limit)

    @app.get("/jobs/{job_id}/events")
    def job_events(job_id: str, limit: int = 50) -> list[dict[str, Any]]:
        if app.state.manager.get(job_id) is None:
            raise HTTPException(404, f"no job {job_id!r}")
        return app.state.store.events(job_id, limit=limit)

    @app.get("/jobs/{job_id}/content")
    def job_content(job_id: str) -> dict[str, Any]:
        """Hand out the information ujin last obtained for this workflow.

        Returns the most recent :class:`PollResult` payload (the body/data the
        source produced on its last poll, changed or not) so a consumer can reuse
        what ujin already fetched. ``payload`` is ``null`` until the first poll.
        """
        handle = app.state.manager.get(job_id)
        if handle is None:
            raise HTTPException(404, f"no job {job_id!r}")
        p = handle.target.prev
        return {
            "id": job_id,
            "name": handle.spec.name,
            "ok": p.ok if p else None,
            "changed": p.changed if p else None,
            "fingerprint": p.fingerprint if p else None,
            "ts": getattr(p, "ts", None) if p else None,
            "status": p.status if p else None,
            "payload": getattr(p, "payload", None) if p else None,
        }

    @app.get("/jobs/{job_id}/results")
    def job_results(job_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Recent buffer of obtained results (one entry per changed poll)."""
        if app.state.manager.get(job_id) is None:
            raise HTTPException(404, f"no job {job_id!r}")
        return app.state.store.results(job_id, limit=limit)

    @app.websocket("/jobs/events")
    async def jobs_events(socket: WebSocket) -> None:
        await socket.accept()
        app.state.hub.add(socket)
        try:
            while True:
                await socket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            app.state.hub.remove(socket)

    # Optional API-key gate (off unless UJIN_API_KEY is set — trust the network
    # by default; guards HTTP + WebSocket; /health stays open).
    from ujin.auth import mount_api_key

    if mount_api_key(app):
        log.info("ujin jobs: API-key auth enabled")

    return app


def serve(  # pragma: no cover -- launches uvicorn; not testable without a live server
    host: str = "0.0.0.0",
    port: int = 8902,
    config_path: str | None = None,
    workflows_dir: str | None = None,
) -> None:
    import uvicorn

    uvicorn.run(
        create_jobs_app(config_path, workflows_dir=workflows_dir), host=host, port=port
    )
