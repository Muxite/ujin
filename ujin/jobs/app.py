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
import itertools
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

# A value that is *exactly* one ``${VAR}`` reference and nothing else. When such a
# whole-scalar reference resolves to a typed matrix variable (an int/float/bool),
# the native type is preserved instead of being stringified — so ``base: ${IVL}``
# with ``IVL: 300`` stays the integer ``300``. Partial refs (``.../${NAME}/x``)
# still interpolate as strings via :data:`_ENV_REF`.
_WHOLE_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}$")


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


class WorkflowMatrixError(ValueError):
    """A workflow's ``matrix:`` / ``for_each:`` block is malformed.

    Raised (naming the offending template) when the block is neither a list nor a
    mapping of axes, an entry is not a mapping of variable names to values, or an
    axis value is not a list. Callers that load a directory catch it and surface
    it in ``GET /health`` ``workflows.failed``.
    """


# The keys that turn a single workflow template into a fan-out over var maps.
_MATRIX_KEYS = ("matrix", "for_each")


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
    the job body / ``jobs:`` list. A non-mapping ``defaults:`` is a clear, named
    error routed through ``workflows.failed`` rather than failing deep inside the
    merge.
    """
    if not isinstance(data, dict):
        return {}, [], data
    data = dict(data)
    defaults = data.pop("defaults", None) or {}
    if not isinstance(defaults, dict):
        raise WorkflowIncludeError(
            f"defaults: must be a mapping of shared job settings, "
            f"got {type(defaults).__name__}"
        )
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


def _resolve_ref(name: str, default: str | None, vars: dict[str, Any]) -> str:
    """Resolve one ``${VAR}`` reference against matrix *vars*, then os.environ.

    With an empty *vars* map this reproduces :func:`_expand_env` exactly (env,
    then ``:-default``, then empty-with-a-warning), so routing a file through the
    tree substituter keeps non-matrix ``${VAR}`` semantics byte-for-byte.
    """
    if name in vars:
        return str(vars[name])
    val = os.environ.get(name)
    if val is not None:
        return val
    if default is not None:
        return default
    log.warning("workflow env var %s unset; expanding to empty", name)
    return ""


def _subst_tree(node: Any, vars: dict[str, Any]) -> Any:
    """Recursively substitute ``${VAR}``/``${VAR:-default}`` in a parsed tree.

    Strings are expanded from *vars* (falling back to the environment); dicts and
    lists are walked; every other scalar is returned unchanged.
    """
    if isinstance(node, str):
        whole = _WHOLE_REF.match(node)
        if whole is not None and whole.group(1) in vars:
            # Whole-scalar reference to a matrix var: keep its native type so a
            # numeric value stays int/float rather than becoming a string.
            return vars[whole.group(1)]
        return _ENV_REF.sub(
            lambda m: _resolve_ref(m.group(1), m.group(2), vars), node
        )
    if isinstance(node, dict):
        return {k: _subst_tree(v, vars) for k, v in node.items()}
    if isinstance(node, list):
        return [_subst_tree(v, vars) for v in node]
    return node


def _slugify(value: str) -> str:
    """Lowercase, collapse non-alphanumerics to single dashes, trim dashes."""
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _entry_slug(entry: dict[str, Any], index: int) -> str:
    """A deterministic, readable token for a matrix entry.

    Prefers a conventional identity variable (``id``/``name``, case-insensitive)
    so the common case reads cleanly (``<template>-<name>``); otherwise falls back
    to a slug of all the entry's values, then the positional index. Collisions are
    disambiguated by the caller so ids stay distinct.
    """
    lowered = {k.lower(): v for k, v in entry.items()}
    for key in ("id", "name"):
        if key in lowered:
            slug = _slugify(str(lowered[key]))
            if slug:
                return slug
    parts = [_slugify(str(v)) for v in entry.values()]
    slug = "-".join(p for p in parts if p)
    return slug or str(index)


def _cartesian(axes: dict[str, Any], tid: str, key: str) -> list[dict[str, Any]]:
    """Expand a mapping of axes into the cartesian product of variable maps.

    ``{REGION: [us, eu], ENV: [prod, dev]}`` becomes four rows
    (``{REGION: us, ENV: prod}``, ``{REGION: us, ENV: dev}``, ...) in a stable
    axis-declaration order. Each axis value must be a list.
    """
    names = list(axes)
    value_lists = []
    for name in names:
        values = axes[name]
        if not isinstance(values, list):
            raise WorkflowMatrixError(
                f"workflow {tid!r}: {key} axis {name!r} must be a list of "
                f"values, got {type(values).__name__}"
            )
        value_lists.append(values)
    return [dict(zip(names, combo)) for combo in itertools.product(*value_lists)]


def _has_matrix(peek: Any) -> bool:
    """Whether the parsed file contains any ``matrix:``/``for_each:`` template."""
    def _m(d: Any) -> bool:
        return isinstance(d, dict) and any(k in d for k in _MATRIX_KEYS)

    if isinstance(peek, dict) and "jobs" not in peek:
        return _m(peek)
    raw = peek.get("jobs", peek) if isinstance(peek, dict) else peek
    return any(_m(e) for e in (raw or []) if isinstance(e, dict))


def _expand_matrix(template: dict, stem: str, index: int | None) -> list:
    """Expand one resolved workflow *template* dict into a list of job dicts.

    *template* has already gone through ``defaults:``/``include:``/``use:``
    resolution (:func:`_resolve_job`), so shared settings are already merged in
    before the matrix fans out. Without a ``matrix:``/``for_each:`` key this is a
    single job (``${VAR}`` substituted from the environment). With one, the value
    is either a list of variable maps (taken verbatim) or a mapping of axes
    (expanded to the cartesian product); each resulting map produces one job with
    its vars substituted into the whole tree (source/transforms/sinks/schedule)
    and a distinct, deterministic id derived from the template id + the entry.
    """
    base_id = stem if index is None else f"{stem}-{index}"
    matrix = None
    key = None
    for k in _MATRIX_KEYS:
        if k in template:
            matrix, key = template[k], k
            break

    if matrix is None:
        d = _subst_tree(template, {})
        d.setdefault("id", base_id)
        d.setdefault("name", d["id"])
        return [d]

    tid = template.get("id") or base_id
    if isinstance(matrix, dict):
        # Mapping-of-axes form: fan out over the cartesian product of the axes.
        matrix = _cartesian(matrix, tid, key)
    if not isinstance(matrix, list):
        raise WorkflowMatrixError(
            f"workflow {tid!r}: {key}: must be a list of variable maps or a "
            f"mapping of axes, got {type(matrix).__name__}"
        )

    id_tmpl = template.get("id")
    var_driven = id_tmpl is not None and bool(_ENV_REF.search(id_tmpl))
    body = {k: v for k, v in template.items() if k not in _MATRIX_KEYS}
    jobs: list = []
    seen: set[str] = set()
    seen_identity: set[str] = set()
    for i, entry in enumerate(matrix):
        if not isinstance(entry, dict) or not all(
            isinstance(k, str) for k in entry
        ):
            raise WorkflowMatrixError(
                f"workflow {tid!r}: {key}[{i}] must be a variable map "
                f"(mapping of names to values), got {type(entry).__name__}"
            )
        d = _subst_tree(body, entry)
        # Distinguish a genuine author duplicate (same explicit identity twice)
        # from a slugification artifact (distinct values collapsing to one slug).
        # The former is a config error worth surfacing; the latter is disambiguated
        # silently so distinct entries always yield distinct ids.
        if var_driven:
            # The template opted into a var-driven id (e.g. ``id: watch-${NAME}``).
            jid = _subst_tree(id_tmpl, entry)
            identity = jid
        else:
            jid = f"{tid}-{_entry_slug(entry, i)}"
            lowered = {k.lower(): v for k, v in entry.items()}
            identity = next(
                (str(lowered[k]) for k in ("id", "name") if k in lowered), None
            )
        if identity is not None and identity in seen_identity:
            raise WorkflowMatrixError(
                f"workflow {tid!r}: {key} produced a duplicate job id {jid!r}"
            )
        if identity is not None:
            seen_identity.add(identity)
        if jid in seen:  # distinct entries slugged the same -> keep ids distinct
            jid = f"{jid}-{i}"
        seen.add(jid)
        d["id"] = jid
        d.setdefault("name", jid)
        jobs.append(d)
    return jobs


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
    fall back to ``<stem>-<index>`` to stay deterministic. A template carrying a
    ``matrix:``/``for_each:`` block fans out into one job per entry, composed
    *after* ``defaults:``/``include:``/``use:`` resolution so a matrix template
    inherits shared defaults and fragments.
    """
    import yaml

    from .model import JobSpec

    from pathlib import Path

    path = Path(path)
    stem = path.stem
    raw_text = open(path, encoding="utf-8").read()

    # A matrix template's per-entry `${VAR}` refs must survive to tree-level
    # substitution (which falls back to the environment on its own), so detect
    # matrix on the *un-expanded* text and skip the blanket env-expansion pass
    # below for such files. Env-only files take the byte-for-byte legacy path.
    peek = yaml.safe_load(raw_text) or {}
    has_matrix = _has_matrix(peek)
    data = peek if has_matrix else (yaml.safe_load(_expand_env(raw_text)) or {})

    # File-level `defaults:` + `include:`/`use:` (deep-merged under every job).
    defaults, file_includes, data = _file_directives(data)

    # Single-job mapping (no top-level `jobs:` list) -> the whole file is one job.
    if isinstance(data, dict) and "jobs" not in data:
        d = _resolve_job(data, defaults, file_includes, path)
        if has_matrix:
            return [JobSpec.from_dict(j) for j in _expand_matrix(d, stem, None)]
        d.setdefault("id", stem)
        d.setdefault("name", stem)
        return [JobSpec.from_dict(d)]

    raw = data.get("jobs", data) if isinstance(data, dict) else data
    raw = raw or []
    specs = []
    for i, entry in enumerate(raw):
        d = _resolve_job(entry, defaults, file_includes, path)
        if has_matrix:
            idx = None if len(raw) == 1 else i
            specs.extend(JobSpec.from_dict(j) for j in _expand_matrix(d, stem, idx))
            continue
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


def _load_ingest_plan(path: str) -> tuple[list, str | None]:
    """Load an ingest-plan file, returning ``(specs, error)``.

    A bad or missing plan is non-fatal: the error string is returned (not raised)
    so startup can log it and surface it on ``/health``, mirroring how the
    workflows-dir loader reports a bad file.
    """
    from .plan import PlanError, load_plan

    try:
        return load_plan(path), None
    except PlanError as exc:
        return [], str(exc)
    except Exception as exc:  # noqa: BLE001 -- never let a plan block startup
        return [], f"{type(exc).__name__}: {exc}"


def create_jobs_app(
    config_path: str | None = None,
    *,
    workflows_dir: str | None = None,
    plan_path: str | None = None,
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

    from .build import build_job_manager
    from .manager import JobManager, UnknownKind
    from .store import JobStore

    from .plan import PLAN_ENV

    db_path = os.environ.get("UJIN_JOBS_DB", "./ujin-jobs.db")
    wf_dir = workflows_dir or os.environ.get("UJIN_WORKFLOWS_DIR", "/workflows")
    plan_src = plan_path or os.environ.get(PLAN_ENV)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        hub = _Hub()

        # Build the store/engine/manager (+ shared scrape stack for `scrape`
        # sources) via the same factory a headless in-process consumer uses —
        # see ujin.jobs.build for the wiring. Env-based config resolution is
        # this app's own choice (the generic factory takes a plain config, it
        # doesn't hardcode from_env()); a failure resolving *that* config
        # (e.g. a malformed env var) degrades exactly like a scrape-backend
        # build failure would: no scrape stack, a warning, nothing fatal.
        try:
            from ujin.scrape.config import ScrapeConfig

            cfg = scrape_config or ScrapeConfig.from_env()
            manager, store, _scrape_service, aclose_stack = await build_job_manager(
                db_path, scrape_config=cfg, hub=hub
            )
            engine = manager.engine
        except Exception as exc:  # noqa: BLE001
            log.warning("scrape source backend unavailable: %s", exc)
            store = JobStore(db_path)
            engine = PollEngine()
            manager = JobManager(engine, store, hub=hub, scrape_service=None)

            async def aclose_stack() -> None:
                try:
                    store.close()
                except Exception as close_exc:  # noqa: BLE001
                    log.warning("job store close failed: %s", close_exc)

        # Load operator plugins BEFORE reloading jobs, so persisted jobs that
        # reference plugin:* kinds resolve.
        from ujin.plugins import load_plugins

        plugin_status = load_plugins()
        app.state.plugins = plugin_status

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

        # External ingest plan: one mounted file declaring many jobs (defaults +
        # matrix expansion). Same non-fatal contract as the workflows dir — a bad
        # or missing plan is logged and reported on /health, never fatal.
        plan_status: dict[str, Any] = {"path": plan_src, "loaded": [], "failed": []}
        if plan_src:
            specs, plan_error = _load_ingest_plan(plan_src)
            if plan_error is not None:
                log.warning("ingest plan %s not loaded: %s", plan_src, plan_error)
                plan_status["error"] = plan_error
            for spec in specs:
                try:
                    manager.create(spec)
                    plan_status["loaded"].append(spec.id)
                except Exception as exc:  # noqa: BLE001
                    log.warning("ingest plan job %s failed: %s", spec.id, exc)
                    plan_status["failed"].append({"id": spec.id, "error": str(exc)})
        app.state.plan = plan_status

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
            await aclose_stack()  # closes the scrape stack (if any) + the store

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
                                     {"dir": wf_dir, "loaded": [], "failed": []}),
                "plan": getattr(app.state, "plan",
                                {"path": plan_src, "loaded": [], "failed": []})}

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
    plan_path: str | None = None,
) -> None:
    import uvicorn

    uvicorn.run(
        create_jobs_app(config_path, workflows_dir=workflows_dir, plan_path=plan_path),
        host=host, port=port,
    )
