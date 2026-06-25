"""ujin command line — adaptive scraper-poller.

Run ``ujin <command> --help`` for any command's options and examples.

  ujin doctor                  show which backends/extras are installed
  ujin init                    scaffold a starter targets.yaml in the cwd
  ujin sweep targets.yaml      poll all targets once; print what changed
  ujin serve targets.yaml      run the poll engine as a daemon
  ujin watch URL --selector …  watch one page's regions for change

targets.yaml::

    rate: 10            # global requests/sec (smoothing)
    burst: 10
    concurrency: 8
    defaults: { base: 60, min: 5, max: 3600, jitter: decorrelated }
    targets:
      - http:    { url: https://example.com }
      - rss:     { url: https://example.com/feed.xml, base: 300 }
      - api:     { url: https://api.example.com/v1/x, json_path: data.items }
      - command: { argv: [git, ls-remote, https://github.com/x/y] }
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger("ujin.cli")


def _version() -> str:
    """Best-effort package version (the in-package attr, then installed metadata).

    The hardcoded ``ujin.__version__`` is the release source of truth and tracks
    the working tree; installed ``.dist-info`` metadata can lag behind it under
    an editable install, so prefer the attr and fall back to metadata.
    """
    try:
        import ujin

        v = getattr(ujin, "__version__", None)
        if v:
            return v
    except Exception:  # noqa: BLE001 - never let version lookup break the CLI
        pass
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("ujin")
        except PackageNotFoundError:
            pass
    except Exception:  # noqa: BLE001
        pass
    return "unknown"


def _build_pollable(kind: str, cfg: dict[str, Any]):
    """Resolve a poll source through the plugin registry.

    Built-in kinds (http/rss/api/command/site/scrape/browser) and any
    plugin-registered ``plugin:*`` source kinds resolve identically here, so the
    YAML-driven engine and the jobs control plane share one code path.
    """
    from ujin.registry import register

    if not register.has("source", kind):
        raise ValueError(
            f"unknown source kind {kind!r}; "
            f"available: {', '.join(register.available('source'))}"
        )
    try:
        return register.build_source(kind, cfg or {})
    except KeyError as exc:
        # The kind resolved; the factory hit a missing config key (e.g. `url`).
        raise ValueError(f"missing required config key {exc}") from None


def _read_yaml(path: str) -> dict[str, Any]:
    """Load a YAML file into a dict, with actionable file+line errors."""
    import yaml

    p = Path(path)
    if not p.exists():
        raise SystemExit(
            f"ujin: targets file not found: {path}\n"
            f"  hint: run `ujin init` to scaffold a starter targets.yaml"
        )
    text = p.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        loc = ""
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            loc = f" (line {mark.line + 1}, column {mark.column + 1})"
        raise SystemExit(f"ujin: invalid YAML in {path}{loc}: {exc}") from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise SystemExit(
            f"ujin: {path} must be a YAML mapping with a `targets:` list, "
            f"got {type(data).__name__}"
        )
    return data


def _load(path: str):
    from ujin.adapt.concurrency import TokenBucket
    from ujin.engine import PollEngine

    data = _read_yaml(path)
    defaults = data.get("defaults", {})
    engine = PollEngine(
        token_bucket=TokenBucket(rate=data.get("rate", 10.0), burst=data.get("burst", 10.0)),
        max_concurrency=data.get("concurrency", 8),
    )

    async def _on_change(key: str, result) -> None:
        log.info("CHANGED %s (fp=%s)", key, (result.fingerprint or "")[:12])

    for i, entry in enumerate(data.get("targets", [])):
        if not isinstance(entry, dict) or not entry:
            raise SystemExit(
                f"ujin: {path} targets[{i}] must be a single-key mapping like "
                f"`- http: {{url: ...}}`, got {entry!r}"
            )
        kind, cfg = next(iter(entry.items()))
        try:
            pollable = _build_pollable(kind, cfg or {})
        except ValueError as exc:
            raise SystemExit(f"ujin: {path} targets[{i}] ({kind}): {exc}") from None
        engine.add(
            pollable,
            base=cfg.get("base", defaults.get("base", 60)),
            min_interval=cfg.get("min", defaults.get("min", 5)),
            max_interval=cfg.get("max", defaults.get("max", 3600)),
            jitter=cfg.get("jitter", defaults.get("jitter", "decorrelated")),
            on_change=_on_change,
        )
    return engine


def _cmd_serve(args: argparse.Namespace) -> int:
    engine = _load(args.targets)
    log.info("ujin serve: %d target(s)", len(engine.targets))
    asyncio.run(engine.run())
    return 0


def _cmd_sweep(args: argparse.Namespace) -> int:
    engine = _load(args.targets)
    results = asyncio.run(engine.sweep())
    changed = [t.key for t in engine.targets.values() if t.prev and t.prev.changed]
    print(f"swept {len(results)} target(s); changed: {changed or 'none'}")
    return 0


def _cmd_api(args: argparse.Namespace) -> int:
    from ujin.service import serve

    serve(host=args.host, port=args.port, config_path=args.targets)
    return 0


def _cmd_scrape_serve(args: argparse.Namespace) -> int:
    from ujin.scrape.app import serve
    from ujin.scrape.config import ScrapeConfig

    serve(host=args.host, port=args.port, config=ScrapeConfig.from_env())
    return 0


def _parse_patterns(values: "list[str] | None") -> list:
    """Flatten a list of possibly comma-separated glob-pattern strings."""
    if not values:
        return []
    out: list[str] = []
    for v in values:
        out.extend(p.strip() for p in v.split(",") if p.strip())
    return out


def _apply_env_file(env_file: str) -> int:
    """Load KEY=VALUE pairs from *env_file* and set unset vars in os.environ.

    Process environment variables take precedence: a key already set in
    ``os.environ`` is left unchanged; the file only fills in missing vars.
    Returns 0 on success; prints a clean ``ujin: …`` message to stderr and
    returns 1 on a missing or unparseable file.
    """
    from ujin.jobs.app import _load_env_file

    try:
        env_vars = _load_env_file(Path(env_file))
    except OSError as exc:
        print(f"ujin: env-file not found: {env_file}: {exc.strerror}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"ujin: {exc}", file=sys.stderr)
        return 1
    for k, v in env_vars.items():
        if k not in os.environ:
            os.environ[k] = v
    return 0


def _resolve_profile(flag: "str | None") -> "str | None":
    """Resolve the active plan profile: the ``--profile`` flag wins over
    ``$UJIN_PLAN_PROFILE``; with neither set the result is ``None`` (no overlay)."""
    return flag or os.environ.get("UJIN_PLAN_PROFILE") or None


def _cmd_jobs_serve(args: argparse.Namespace) -> int:
    from ujin.jobs.app import serve

    env_file = getattr(args, "env_file", None)
    if env_file:
        rc = _apply_env_file(env_file)
        if rc:
            return rc

    only = _parse_patterns(getattr(args, "only", None))
    exclude = _parse_patterns(getattr(args, "exclude", None))
    tags = _parse_patterns(getattr(args, "tag", None))
    exclude_tags = _parse_patterns(getattr(args, "exclude_tag", None))
    profile = _resolve_profile(getattr(args, "profile", None))

    # Pre-flight: when --plan is given, validate it before launching uvicorn so
    # any malformed config exits non-zero with a clean ujin: … message rather
    # than starting a server with silently-broken jobs.  File-level errors
    # (missing file, bad YAML, unresolvable include, required ${VAR:?}),
    # per-job errors (malformed matrix, duplicate id), and an empty/zero-job
    # plan all cause an immediate non-zero exit here.  --only/--tag selection is
    # folded into this one load so the plan is read only once in the preflight.
    # Profile errors are NOT raised here (profile=None); _check_plan_profile
    # below handles them via its dedicated, cleanly-formatted path.
    if args.plan:
        from ujin.jobs.app import _load_ingest_plan, _select_jobs
        pre_failed: list = []
        pre_specs = _load_ingest_plan(Path(args.plan), failed=pre_failed)
        if pre_failed:
            for entry in pre_failed:
                print(entry["error"], file=sys.stderr)
            return 1
        if not pre_specs:
            print(f"ujin: {Path(args.plan).name}: no jobs defined", file=sys.stderr)
            return 1
        if only or tags:
            try:
                _select_jobs(pre_specs, only, exclude, tags, exclude_tags)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 1

    # Pre-flight: a selected --profile/$UJIN_PLAN_PROFILE absent from the plan's
    # `profiles:` exits non-zero with a clean message before binding a port, instead
    # of starting the server with the overlay silently un-applied. Resolves the plan
    # from the flag or $UJIN_INGEST_PLAN, matching what the server would load.
    plan_src = args.plan or os.environ.get("UJIN_INGEST_PLAN")
    if profile and plan_src:
        from ujin.jobs.app import PlanProfileError, _check_plan_profile
        try:
            _check_plan_profile(plan_src, profile)
        except PlanProfileError as exc:
            print(f"ujin: {exc}", file=sys.stderr)
            return 1

    serve(host=args.host, port=args.port, config_path=args.jobs,
          workflows_dir=args.workflows, plan_path=args.plan,
          only=only or None, exclude=exclude or None,
          tags=tags or None, exclude_tags=exclude_tags or None,
          watch_plan=getattr(args, "watch_plan", False),
          watch_interval=getattr(args, "watch_interval", None),
          profile=profile)
    return 0


def _collect_schema_violations(path: Path) -> list:
    """Return structural JSON-Schema violations for the UNRESOLVED document(s) at *path*.

    Reads the raw YAML (no env substitution — that only ever touches string values,
    never the document shape) and checks it against the schema ``ujin plan schema``
    emits, via the stdlib-only walker in :func:`ujin.jobs.schema.validate_document`.
    A directory validates every top-level ``*.y*ml`` workflow file — the same scan
    the loader uses — prefixing each message with the file name; a single file
    validates that one document. Each message names the offending JSON location
    (e.g. ``$.jobs[0].source``). Documents that fail to parse as YAML are skipped:
    the loader-level check already reports those, so the schema pass only *adds*
    structural findings rather than duplicating parse errors.
    """
    import yaml

    from ujin.jobs.schema import plan_schema, validate_document

    schema = plan_schema()
    out: list = []

    def _check(doc_path: Path, label: "str | None") -> None:
        try:
            document = yaml.safe_load(doc_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            return  # parse/read failures are the loader's domain, not the schema's
        if document is None:  # empty / comment-only file declares no document
            return
        for msg in validate_document(document, schema):
            out.append(f"{label}: {msg}" if label else msg)

    if path.is_dir():
        for fp in sorted(path.glob("*.y*ml")):
            _check(fp, fp.name)
    else:
        _check(path, path.name)
    return out


def _cmd_plan_validate(args: argparse.Namespace) -> int:
    """Validate an INGEST-PLAN file or a workflows directory without starting the server.

    Reuses the same loaders as ``jobs-serve`` so the resolved job ids and failures
    are identical to what the server would produce. Exits zero when all jobs resolve;
    non-zero when any fail or the path is missing/unreadable.

    Unrecognized/misspelled keys are reported as ``WARN`` lines (or ``warnings`` in
    ``--json``). By default they are advisory and do not affect the exit code — a
    previously-valid file stays valid. ``--strict`` promotes every such warning to a
    failure (non-zero exit) for CI use, leaving genuine load failures unchanged.

    ``--schema`` additionally checks the *unresolved* document(s) against the JSON
    Schema ``ujin plan schema`` emits, reporting structural shape violations (a
    scalar document, a non-list ``jobs:``, a string ``source:``) with the offending
    JSON path. It complements — does not replace — the checks above and is off by
    default, so output and exit codes are byte-for-byte unchanged without the flag.
    """
    import json as _json

    from ujin.jobs.app import _load_ingest_plan, _load_workflows_dir, _select_jobs

    env_file = getattr(args, "env_file", None)
    if env_file:
        rc = _apply_env_file(env_file)
        if rc:
            return rc

    path = Path(args.path)
    strict = getattr(args, "strict", False)
    only = _parse_patterns(getattr(args, "only", None))
    exclude = _parse_patterns(getattr(args, "exclude", None))
    tags = _parse_patterns(getattr(args, "tag", None))
    exclude_tags = _parse_patterns(getattr(args, "exclude_tag", None))
    profile = _resolve_profile(getattr(args, "profile", None))

    if not path.exists():
        msg = f"ujin: path not found: {args.path}"
        if args.json:
            print(_json.dumps({"ok": False, "error": msg, "resolved": [],
                               "failed": [], "warnings": [], "strict": strict}))
        else:
            print(msg, file=sys.stderr)
        return 1

    failed: list = []
    warnings: list = []
    if path.is_dir():
        specs = _load_workflows_dir(str(path), failed=failed, warnings=warnings,
                                    profile=profile)
    else:
        specs = _load_ingest_plan(path, failed=failed, warnings=warnings,
                                  profile=profile)

    # Apply --only/--exclude/--tag/--exclude-tag selection after full resolution
    # so patterns match the final expanded job ids and resolved tags (post
    # defaults-merge, includes, matrix fan-out).
    if only or exclude or tags or exclude_tags:
        try:
            specs = _select_jobs(specs, only or None, exclude or None,
                                 tags or None, exclude_tags or None)
        except ValueError as exc:
            msg = str(exc)
            if args.json:
                print(_json.dumps({"ok": False, "error": msg, "resolved": [],
                                   "failed": [], "warnings": [], "strict": strict}))
            else:
                print(msg, file=sys.stderr)
            return 1

    resolved = [s.id for s in specs]

    # An ingest-plan FILE that resolves to zero jobs is itself an error: an
    # otherwise-valid plan with no declared jobs almost certainly means all entries
    # failed, the file is empty, or a `jobs: []` stub was left in place.
    # An empty workflows DIRECTORY is legitimate (freshly created, not yet populated)
    # so the check is file-only; a directory with all-failed workflows already has
    # non-empty `failed`.
    if not failed and not resolved and path.is_file():
        failed.append({
            "id": path.stem,
            "error": f"ujin: {path.name}: no jobs defined",
        })

    # `--schema` additionally checks the UNRESOLVED document against the JSON Schema
    # `ujin plan schema` emits — a structural shape check that complements (does not
    # replace) the loader-level and --strict checks above. Off by default, so output
    # and exit codes are byte-for-byte unchanged without the flag.
    schema = getattr(args, "schema", False)
    schema_errors = _collect_schema_violations(path) if schema else []
    # Schema warnings only fail the run under --strict; otherwise they are reported
    # but the file stays valid (additive default — no previously-valid file breaks).
    # Structural --schema violations are hard failures, like a load failure.
    ok = not failed and not (strict and warnings) and not schema_errors

    if args.json:
        payload = {"ok": ok, "resolved": resolved, "failed": failed,
                   "warnings": warnings, "strict": strict}
        if schema:
            payload["schema_errors"] = schema_errors
        print(_json.dumps(payload, indent=2))
    else:
        for job_id in resolved:
            print(f"ok  {job_id}")
        for entry in failed:
            print(f"FAIL  {entry['id']}: {entry['error']}")
        label = "FAIL" if strict else "WARN"
        for w in warnings:
            print(f"{label}  {w}")
        for e in schema_errors:
            print(f"FAIL  schema: {e}")
        if not ok:
            n = len(failed) + (len(warnings) if strict else 0) + len(schema_errors)
            print(
                f"ujin: {n} failure(s); {len(resolved)} job(s) resolved",
                file=sys.stderr,
            )
        elif warnings:
            print(
                f"ujin: {len(warnings)} warning(s); {len(resolved)} job(s) resolved "
                "(re-run with --strict to fail on these)",
                file=sys.stderr,
            )

    return 0 if ok else 1


def _render_summary(specs: list) -> str:
    """Return a concise human-readable overview of a resolved plan.

    First line: ``N jobs`` (or ``1 job``).  Then one indented line per job::

        <id>  <source-kind>[:<url>]  <schedule>  <N> sink[s]

    The schedule column is ``adaptive/<base>s``, ``poll/<base>s``,
    ``cron/<expr>``, or ``once``.  ``url`` is omitted when the source config
    carries no ``url`` key (e.g. command sources).
    """
    n = len(specs)
    lines = [f"{n} job{'s' if n != 1 else ''}"]
    for spec in specs:
        src = spec.source
        url = src.config.get("url", "")
        src_col = f"{src.kind}:{url}" if url else src.kind

        sched = spec.schedule
        if sched.mode == "cron":
            sched_col = f"cron/{sched.cron}"
        elif sched.mode == "once":
            sched_col = "once"
        else:
            sched_col = f"{sched.mode}/{sched.base}s"

        ns = len(spec.sinks)
        sink_col = f"{ns} sink{'s' if ns != 1 else ''}"

        lines.append(f"  {spec.id}  {src_col}  {sched_col}  {sink_col}")
    return "\n".join(lines)


def _cmd_plan_render(args: argparse.Namespace) -> int:
    """Resolve and print every fully-expanded JobSpec for a plan or workflows directory.

    Reuses the same loaders as ``jobs-serve`` and ``plan validate`` so resolved ids
    and failures are identical to what the server would produce. Exits zero when all
    jobs resolve; non-zero with a clean ``ujin: …`` message (no traceback) on a
    missing/unreadable path or any failed job.

    With ``--explain`` each rendered job is annotated with its provenance — the
    originating plan/workflow source file and, for ``matrix:``/``for_each:``-expanded
    jobs, the matrix entry index and the variable map that produced it. Provenance is
    threaded through the loaders only when ``--explain`` is set, so the default render
    path is byte-for-byte unchanged. ``--explain`` composes with ``--json`` (the
    machine-readable form), ``--only``/``--exclude``, and ``--env-file``.

    With ``--summary`` a concise human-readable overview is printed instead of the
    full expanded specs: a job count followed by one line per job (id, source
    kind/url, schedule, and number of sinks). ``--summary`` composes with
    ``--only``/``--exclude``, ``--env-file``, and ``--profile`` but is mutually
    exclusive with ``--json`` and ``--explain`` (those flags require the full output).
    """
    import json as _json

    import yaml as _yaml

    from ujin.jobs.app import _load_ingest_plan, _load_workflows_dir, _select_jobs

    env_file = getattr(args, "env_file", None)
    if env_file:
        rc = _apply_env_file(env_file)
        if rc:
            return rc

    path = Path(args.path)
    only = _parse_patterns(getattr(args, "only", None))
    exclude = _parse_patterns(getattr(args, "exclude", None))
    tags = _parse_patterns(getattr(args, "tag", None))
    exclude_tags = _parse_patterns(getattr(args, "exclude_tag", None))
    explain = getattr(args, "explain", False)
    summary = getattr(args, "summary", False)
    profile = _resolve_profile(getattr(args, "profile", None))

    if not path.exists():
        print(f"ujin: path not found: {args.path}", file=sys.stderr)
        return 1

    failed: list = []
    # Provenance is collected only under --explain; without it the loaders run on
    # the existing code path (provenance=None) so the default render is unchanged.
    provenance: list | None = [] if explain else None
    if path.is_dir():
        specs = _load_workflows_dir(str(path), failed=failed, provenance=provenance,
                                    profile=profile)
    else:
        specs = _load_ingest_plan(path, failed=failed, provenance=provenance,
                                  profile=profile)

    # Apply --only/--exclude/--tag/--exclude-tag selection after full resolution
    # so patterns match the final expanded job ids and resolved tags (post
    # defaults-merge, includes, matrix fan-out).
    if only or exclude or tags or exclude_tags:
        try:
            specs = _select_jobs(specs, only or None, exclude or None,
                                 tags or None, exclude_tags or None)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1

    if failed:
        for entry in failed:
            print(entry["error"], file=sys.stderr)
        print(
            f"ujin: {len(failed)} failure(s); {len(specs)} job(s) resolved",
            file=sys.stderr,
        )
        return 1

    # Map each resolved job id to its provenance record (ids are unique post-
    # resolution), so it survives --only/--exclude reordering/filtering of specs.
    prov_by_id = {p["id"]: p for p in (provenance or [])}

    def _prov(spec) -> dict:
        p = prov_by_id.get(spec.id, {})
        return {k: v for k, v in p.items() if k != "id"}

    if summary:
        print(_render_summary(specs))
    elif args.json:
        if explain:
            print(_json.dumps(
                [{"spec": s.to_dict(), "provenance": _prov(s)} for s in specs],
                indent=2,
            ))
        else:
            print(_json.dumps([s.to_dict() for s in specs], indent=2))
    else:
        for i, spec in enumerate(specs):
            if i:
                print()
            print(f"--- # {spec.id}")
            if explain:
                for line in _explain_comment_lines(_prov(spec), _yaml):
                    print(line)
            print(_yaml.dump(spec.to_dict(), default_flow_style=False, sort_keys=False), end="")

    return 0


def _explain_comment_lines(prov: dict, yaml_mod) -> "list[str]":
    """Render a job's provenance as ``# ...`` comment lines for ``--explain`` YAML.

    Emits a ``# source:`` line naming the originating file and, for a
    matrix/for_each-expanded job, a ``# matrix:`` line naming the key, 0-based entry
    index, and the variable map that produced the job. Returns ``[]`` for an empty
    provenance record so the YAML body still prints.
    """
    lines: list[str] = []
    src = prov.get("source")
    if src:
        lines.append(f"# source: {src}")
    matrix = prov.get("matrix")
    if matrix:
        flow = yaml_mod.dump(
            matrix.get("vars", {}), default_flow_style=True, sort_keys=False
        ).strip()
        lines.append(
            f"# {matrix.get('key', 'matrix')}: entry {matrix.get('index')} -> {flow}"
        )
    return lines


def _resolve_diff_input(path_str: str) -> "tuple[dict | None, str | None]":
    """Resolve a ``plan diff`` OLD/NEW input into ``{job_id: comparable_spec}``.

    Reuses the same loaders as ``plan validate``/``plan render`` so the resolved
    job ids and specs are identical to what the server would produce — defaults
    deep-merge, include:/use: fragments, matrix:/for_each: fan-out, and ${VAR}
    substitution are all expanded. A file path loads as an INGEST-PLAN; a
    directory loads as a workflows dir.

    Returns ``(jobs, None)`` on success, where *jobs* maps each resolved job id to
    its spec dict with ``created_at`` stripped — that field is per-load wall-clock
    noise that would otherwise mark every job as changed. Returns ``(None, msg)``
    with a clean ``ujin: …`` message (no traceback) when the path is missing or
    any job fails to load/resolve.
    """
    from ujin.jobs.app import _load_ingest_plan, _load_workflows_dir

    path = Path(path_str)
    if not path.exists():
        return None, f"ujin: path not found: {path_str}"

    failed: list = []
    if path.is_dir():
        specs = _load_workflows_dir(str(path), failed=failed)
    else:
        specs = _load_ingest_plan(path, failed=failed)

    if failed:
        # Loader failure messages are already ``ujin: …``-prefixed and name the
        # offending file/job; surface the first one as the clean error.
        return None, failed[0]["error"]

    jobs: dict = {}
    for spec in specs:
        d = spec.to_dict()
        d.pop("created_at", None)
        jobs[spec.id] = d
    return jobs, None


def _cmd_plan_diff(args: argparse.Namespace) -> int:
    """Diff two resolved plans (or workflows dirs) keyed by final job id.

    Resolves OLD and NEW with the same loaders as ``plan validate``/``plan
    render`` (defaults/include/matrix/${VAR} fully expanded), then reports, by
    job id, the jobs **added** in NEW, **removed** in NEW, and **changed**
    (present in both with a differing resolved spec). Exits zero whenever both
    inputs resolve — including when there is no difference — unless
    ``--exit-code`` (``--check``) is given, in which case it exits non-zero
    when there are any added/removed/changed jobs and zero when they are
    identical. An unreadable or schema-invalid OLD/NEW input exits non-zero with
    a clean ``ujin: …`` message and no traceback.

    ``--json`` emits the same information as a structured object with distinct
    ``added``/``removed``/``changed`` groupings. ``--env-file`` is accepted
    consistently with the other plan subcommands.
    """
    import json as _json

    env_file = getattr(args, "env_file", None)
    if env_file:
        rc = _apply_env_file(env_file)
        if rc:
            return rc

    old_jobs, err = _resolve_diff_input(args.old)
    if err:
        print(err, file=sys.stderr)
        return 1
    new_jobs, err = _resolve_diff_input(args.new)
    if err:
        print(err, file=sys.stderr)
        return 1

    old_ids = set(old_jobs)
    new_ids = set(new_jobs)
    added = sorted(new_ids - old_ids)
    removed = sorted(old_ids - new_ids)
    changed = sorted(i for i in (old_ids & new_ids) if old_jobs[i] != new_jobs[i])

    if args.json:
        print(_json.dumps({
            "added": [{"id": i, "spec": new_jobs[i]} for i in added],
            "removed": [{"id": i, "spec": old_jobs[i]} for i in removed],
            "changed": [{"id": i, "old": old_jobs[i], "new": new_jobs[i]}
                        for i in changed],
        }, indent=2))
    else:
        for i in added:
            print(f"+ {i}")
        for i in removed:
            print(f"- {i}")
        for i in changed:
            print(f"~ {i}")
        if added or removed or changed:
            print(
                f"ujin: {len(added)} added, {len(removed)} removed, "
                f"{len(changed)} changed",
                file=sys.stderr,
            )
        else:
            print("no changes")

    exit_code = getattr(args, "exit_code", False)
    if exit_code and (added or removed or changed):
        return 1
    return 0


def _cmd_plan_schema(args: argparse.Namespace) -> int:
    """Print the JSON Schema (draft 2020-12) for the INGEST-PLAN / workflow format.

    Emits a single deterministic JSON document describing the file format the plan
    and workflow loaders accept — the per-job shape (source/transforms/sinks/
    schedule), the top-level ``defaults:`` block, ``include:``/``use:`` fragment
    references, ``matrix:``/``for_each:`` fan-out, ``vars:`` defaults, and
    ``profiles:`` overlays. With ``--out PATH`` the schema is written to that file
    instead of stdout (a write error exits non-zero with a clean ``ujin: …``
    message). Read-only: it resolves no plan and never touches ``JobSpec.from_dict``
    or the loaders.
    """
    import json as _json

    from ujin.jobs.schema import plan_schema

    text = _json.dumps(plan_schema(), indent=2)

    out = getattr(args, "out", None)
    if out:
        try:
            Path(out).write_text(text + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"ujin: cannot write schema to {out}: {exc}", file=sys.stderr)
            return 1
        print(f"ujin: wrote JSON Schema to {out}", file=sys.stderr)
    else:
        print(text)
    return 0


def _cmd_plan_env(args: argparse.Namespace) -> int:
    """Report the environment variables a plan/workflow file references.

    Reuses the same ``_ENV_REF`` regex and ``include:``/``use:`` fragment-resolution
    machinery as the loaders (via :func:`ujin.jobs.app.collect_plan_env_refs`) to
    walk *path* and every fragment it pulls in, then lists each ``${VAR}`` with the
    default declared by its ``${VAR:-default}`` form, whether any occurrence is
    required (``${VAR:?message}``), and whether it is currently set in the
    environment. Strictly read-only — it never expands or alters resolution.

    ``--env-file`` fills in variables not already in the process environment before
    the set/unset check (process env wins), consistent with the other plan
    subcommands. ``--json`` emits a machine-readable object instead of the table. A
    plan with no ``${VAR}`` references succeeds (exit 0) and reports an empty set; a
    missing/unreadable path exits non-zero with a clean ``ujin: …`` message.
    """
    import json as _json

    from ujin.jobs.app import collect_plan_env_refs

    env_file = getattr(args, "env_file", None)
    if env_file:
        rc = _apply_env_file(env_file)
        if rc:
            return rc

    path = Path(args.path)
    if not path.exists():
        msg = f"ujin: path not found: {args.path}"
        if args.json:
            print(_json.dumps({"path": args.path, "vars": [], "error": msg}))
        else:
            print(msg, file=sys.stderr)
        return 1

    try:
        refs = collect_plan_env_refs(path)
    except OSError as exc:
        print(f"ujin: cannot read {args.path}: {exc}", file=sys.stderr)
        return 1

    rows = [
        {
            "name": name,
            "default": info["default"],
            "required": info["required"],
            "set": name in os.environ,
        }
        for name, info in refs.items()
    ]

    if args.json:
        print(_json.dumps({"path": str(path), "vars": rows}, indent=2))
        return 0

    if not rows:
        print("no environment variables referenced")
        return 0

    n = len(rows)
    print(f"{n} variable{'s' if n != 1 else ''} referenced")
    width = max(len(r["name"]) for r in rows)
    for r in rows:
        req = "required" if r["required"] else "optional"
        state = "set" if r["set"] else "unset"
        line = f"  {r['name']:<{width}}  {req}  {state}"
        if r["default"] is not None:
            line += f"  default={r['default']}"
        print(line)
    return 0


def _cmd_plan_eject(args: argparse.Namespace) -> int:
    """Materialize fully-expanded jobs from a plan or workflows dir to standalone files.

    Resolves the plan through the same loaders as ``jobs-serve`` and ``plan render``
    — ``defaults:`` deep-merge, ``include:``/``use:`` fragment inlining,
    ``matrix:``/``for_each:`` fan-out, ``profiles:`` overlays, and ``${VAR}``
    substitution all applied — then writes one YAML file per resolved job id into the
    ``--out`` directory.  The file is named ``<id>.yaml`` and contains only the
    fully-resolved ``JobSpec`` fields; no ``defaults:``, ``include:``, ``matrix:``,
    or ``${VAR}`` placeholders remain.  Loading any ejected file back through the
    single-file workflow loader (``_specs_from_workflow_file``) yields the same
    ``JobSpec`` it had inside the plan.

    The ``--out`` directory is created (including parents) if it does not yet exist;
    existing files are overwritten.  Exits zero when all jobs resolve; non-zero with a
    clean ``ujin: …`` message (no traceback) on a missing/unreadable path or any
    failed job.
    """
    import yaml as _yaml

    from ujin.jobs.app import _load_ingest_plan, _load_workflows_dir, _select_jobs

    env_file = getattr(args, "env_file", None)
    if env_file:
        rc = _apply_env_file(env_file)
        if rc:
            return rc

    path = Path(args.path)
    only = _parse_patterns(getattr(args, "only", None))
    exclude = _parse_patterns(getattr(args, "exclude", None))
    tags = _parse_patterns(getattr(args, "tag", None))
    exclude_tags = _parse_patterns(getattr(args, "exclude_tag", None))
    profile = _resolve_profile(getattr(args, "profile", None))
    out_dir = Path(args.out)

    if not path.exists():
        print(f"ujin: path not found: {args.path}", file=sys.stderr)
        return 1

    failed: list = []
    if path.is_dir():
        specs = _load_workflows_dir(str(path), failed=failed, profile=profile)
    else:
        specs = _load_ingest_plan(path, failed=failed, profile=profile)

    if only or exclude or tags or exclude_tags:
        try:
            specs = _select_jobs(specs, only or None, exclude or None,
                                 tags or None, exclude_tags or None)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1

    if failed:
        for entry in failed:
            print(entry["error"], file=sys.stderr)
        print(
            f"ujin: {len(failed)} failure(s); {len(specs)} job(s) resolved",
            file=sys.stderr,
        )
        return 1

    out_dir.mkdir(parents=True, exist_ok=True)
    for spec in specs:
        dest = out_dir / f"{spec.id}.yaml"
        dest.write_text(
            _yaml.dump(spec.to_dict(), default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )
        print(f"  {spec.id}  ->  {dest}")

    print(f"ujin: {len(specs)} job(s) ejected to {out_dir}")
    return 0


def _cmd_mcp_serve(args: argparse.Namespace) -> int:
    from ujin.mcp import serve

    serve(transport=("http" if args.http else "stdio"),
          host=args.host, port=args.port)
    return 0


def _cmd_obscura_build(args: argparse.Namespace) -> int:
    """Init the bundled obscura submodule and build the release binary.

    This is the only step that needs the Rust toolchain; it is never run at
    pip-install time. The first build compiles V8 and is slow (~15-20 min).
    """
    import subprocess

    repo_root = Path(__file__).resolve().parents[1].parent
    submodule = repo_root / "ujin" / "obscura"
    log.info("initializing obscura submodule at %s", submodule)
    subprocess.run(
        ["git", "submodule", "update", "--init", "ujin/obscura"],
        cwd=repo_root, check=True,
    )
    if not (submodule / "Cargo.toml").exists():
        log.error("obscura submodule has no Cargo.toml at %s", submodule)
        return 1
    log.info("building obscura (cargo build --release) — first build is slow")
    subprocess.run(["cargo", "build", "--release"], cwd=submodule, check=True)
    binary = submodule / "target" / "release" / "obscura"
    log.info("obscura built: %s", binary)
    return 0


_STARTER_TARGETS = """\
# ujin targets — poll anything on an adaptive, jittered cadence.
#   ujin sweep targets.yaml    one pass; prints what changed
#   ujin serve targets.yaml    run as a daemon
#
# Global smoothing knobs (optional):
rate: 10            # aggregate requests/sec ceiling (token bucket)
burst: 10
concurrency: 8

# Per-target interval defaults (seconds). Each target may override these.
defaults: { base: 60, min: 5, max: 3600, jitter: decorrelated }

targets:
  # An HTTP page — fingerprinted whole-body change detection.
  - http: { url: https://example.com }
  # An RSS/Atom feed, polled less often.
  - rss: { url: https://example.com/feed.xml, base: 300 }
  # A JSON API; json_path narrows to the slice that matters.
  - api: { url: https://api.example.com/v1/items, json_path: data.items }
  # Any shell command — change = different stdout.
  - command: { argv: [git, ls-remote, https://github.com/python/cpython] }
"""


def _cmd_doctor(args: argparse.Namespace) -> int:
    """Report installed backends/extras and what each unlocks."""
    from ujin.fetch.capabilities import BACKENDS

    print(f"ujin {_version()}")
    print()
    print("Fetch backends (what renders/fetches your pages):")
    for cap in BACKENDS.values():
        ok = cap.available()
        mark = "ok " if ok else "-- "
        print(f"  [{mark}] {cap.name:<10} {'available' if ok else 'not installed'}")
        print(f"            {cap.description.splitlines()[0]}")
        if not ok:
            print(f"            enable: {cap.install_weight}")

    # Optional Python extras, probed without importing heavy modules.
    import importlib.util

    extras = [
        ("aiohttp", "web", "HTTP/RSS/API roles + the scrape toolkit"),
        ("selectolax", "scrape", "fast HTML parsing for extraction"),
        ("trafilatura", "scrape", "article/main-content extraction"),
        ("feedparser", "web", "RSS/Atom feed parsing"),
        ("fastapi", "service", "the REST/WebSocket HTTP services (api/scrape/jobs-serve)"),
        ("mcp", "mcp", "the MCP server for agents (ujin mcp-serve)"),
    ]
    print()
    print("Python extras:")
    for mod, extra, what in extras:
        present = importlib.util.find_spec(mod) is not None
        mark = "ok " if present else "-- "
        hint = "" if present else f"   (pip install 'ujin[{extra}]')"
        print(f"  [{mark}] {mod:<13} {what}{hint}")
    print()
    print("Tip: `ujin init` writes a starter targets.yaml; "
          "`ujin sweep targets.yaml` runs one pass.")
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    """Scaffold a starter targets.yaml so a newcomer can run `ujin sweep` at once."""
    dest = Path(args.path)
    if dest.exists() and not args.force:
        print(f"ujin: {dest} already exists (use --force to overwrite)",
              file=sys.stderr)
        return 1
    dest.write_text(_STARTER_TARGETS, encoding="utf-8")
    print(f"wrote {dest}")
    print(f"next: ujin sweep {dest}    # one pass, prints what changed")
    print(f"      ujin serve {dest}    # run as an adaptive daemon")
    return 0


def _open_site_store_ro(path: str | None):
    """Open an *existing* ``SiteStore`` for read-only introspection.

    The store API has no record-creating side effects beyond ``record()`` (which
    we never call here), but ``sqlite3.connect`` happily *creates* a missing
    file — so we check existence first and emit a clean, actionable ``ujin: ...``
    error (no traceback) for a missing or empty path, matching the rest of the
    CLI's error style.
    """
    import sqlite3

    from ujin.adapt import SiteStore

    if not path:
        raise SystemExit(
            "ujin: a site-store database path is required\n"
            "  usage: ujin learned <site_state.db>  "
            "(the path your poller/scraper persists to)"
        )
    if not Path(path).exists():
        raise SystemExit(
            f"ujin: site-store database not found: {path}\n"
            "  hint: pass the SiteStore db path your poller/scraper persists to"
        )
    try:
        return SiteStore(path)
    except sqlite3.DatabaseError:
        raise SystemExit(f"ujin: not a valid ujin site-store database: {path}") from None


def _open_strategy_feedback_ro(path: str):
    """Open an existing ``StrategyFeedback`` db read-only (same error style)."""
    import sqlite3

    from ujin.adapt import StrategyFeedback

    if not Path(path).exists():
        raise SystemExit(
            f"ujin: strategy-feedback database not found: {path}\n"
            "  hint: pass the StrategyFeedback db path your scraper persists to"
        )
    try:
        return StrategyFeedback(path)
    except sqlite3.DatabaseError:
        raise SystemExit(
            f"ujin: not a valid ujin strategy database: {path}"
        ) from None


def _fmt_secs(value: float) -> str:
    """Compact seconds with trailing zeros stripped, e.g. ``10s``/``0.42s``."""
    s = f"{float(value):.3f}".rstrip("0").rstrip(".")
    return f"{s}s"


_LEARNED_COLUMNS = [
    ("host", "host", str),
    ("status", "last_status", str),
    ("latency", "last_latency", _fmt_secs),
    ("p50", "p50_latency", _fmt_secs),
    ("interval", "interval", _fmt_secs),
    ("rec.int", "recommended_interval", _fmt_secs),
    ("conc", "concurrency_factor", lambda v: f"{v:.2f}"),
    ("health", "health", lambda v: f"{v:.2f}"),
    ("cooldown", "cooldown_secs", lambda v: _fmt_secs(v) if v else "-"),
    ("crawl", "crawl_delay", lambda v: _fmt_secs(v) if v else "-"),
    ("err", "error_count", str),
    ("429", "rate_limit_count", str),
]


def _print_learned_table(args: argparse.Namespace, rows: list[dict]) -> None:
    """Render the per-host learned state as an aligned, human-readable table."""
    if not rows:
        if args.host:
            print(f"(host {args.host!r} not found in {args.db})")
        else:
            print(f"(no learned hosts in {args.db})")
        return
    cols = list(_LEARNED_COLUMNS)
    if args.strategy_db:
        cols.append(
            ("best-strategy", "recommended_strategy",
             lambda v: "/".join(v) if v else "-")
        )
    headers = [label for label, _, _ in cols]
    cells = [[fmt(r[key]) for _, key, fmt in cols] for r in rows]
    widths = [
        max(len(headers[i]), *(len(row[i]) for row in cells))
        for i in range(len(headers))
    ]
    fmt_row = lambda values: "  ".join(  # noqa: E731 - tiny local formatter
        v.ljust(w) for v, w in zip(values, widths)
    )
    print(fmt_row(headers))
    print("  ".join("-" * w for w in widths))
    for row in cells:
        print(fmt_row(row))


def _cmd_learned(args: argparse.Namespace) -> int:
    """Inspect the durable per-host learned state in a SiteStore database."""
    import json as _json

    from ujin.adapt import derive_signals

    store = _open_site_store_ro(args.db)
    feedback = _open_strategy_feedback_ro(args.strategy_db) if args.strategy_db else None
    try:
        known = store.hosts()
        if args.host:
            hosts = [args.host] if args.host in known else []
        else:
            hosts = known

        rows: list[dict] = []
        for host in hosts:
            rec = store.get(host)
            # The stored adaptive interval is the base; derive_signals layers any
            # rate-limit slowdown and Crawl-delay floor on top of it.
            sig = derive_signals(rec, base_interval=rec.interval)
            row = {
                "host": host,
                "last_status": rec.last_status,
                "last_latency": round(rec.last_latency, 6),
                "p50_latency": round(rec.p50_latency, 6),
                "error_count": rec.error_count,
                "rate_limit_count": rec.rate_limit_count,
                "crawl_delay": rec.crawl_delay,
                "interval": rec.interval,
                "last_seen": rec.last_seen,
                "recommended_interval": round(sig.recommended_interval, 6),
                "concurrency_factor": round(sig.concurrency_factor, 6),
                "health": round(sig.health, 6),
                "rate_limited": sig.rate_limited,
                "should_cooldown": sig.should_cooldown,
                "cooldown_secs": round(sig.cooldown_secs, 6),
            }
            if feedback is not None:
                best = feedback.recommend(host)
                row["recommended_strategy"] = list(best) if best else None
            rows.append(row)

        if args.json:
            print(_json.dumps({"db": args.db, "hosts": rows}, indent=2))
        else:
            _print_learned_table(args, rows)
        return 0
    finally:
        store.close()
        if feedback is not None:
            feedback.close()


def _cmd_watch(args: argparse.Namespace) -> int:
    """Watch one URL's selected regions; log or webhook on change."""
    from ujin.diff.events import CallbackSink, WebhookSink
    from ujin.engine import PollEngine
    from ujin.poll.site import SitePollable

    pollable = SitePollable(args.url, args.selector or None, render=args.render)
    if args.webhook:
        on_change = WebhookSink(args.webhook)
    else:
        def _log(event) -> None:
            log.info("CHANGED %s regions=%s fp=%s",
                     event.key, event.regions or "(whole-page)",
                     (event.fingerprint or "")[:12])

        on_change = CallbackSink(_log)

    engine = PollEngine()
    engine.add(pollable, base=args.base, min_interval=args.min,
               max_interval=args.max, on_change=on_change)
    log.info("ujin watch: %s (%d selector(s))", args.url, len(args.selector or []))
    asyncio.run(engine.run())
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(
        prog="ujin",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="New here? Try `ujin doctor` then `ujin init`. "
               "Docs: https://github.com/Muxite/ujin (see README + docs/).",
    )
    parser.add_argument("--version", action="version",
                        version=f"ujin {_version()}")
    sub = parser.add_subparsers(dest="cmd", required=True, metavar="<command>")

    p_doctor = sub.add_parser(
        "doctor", help="report installed backends/extras and what each unlocks",
        description="Show which fetch backends (http/obscura/playwright/selenium) "
                    "and optional Python extras are installed, and the pip command "
                    "to enable each missing one. Safe to run any time.",
    )
    p_doctor.set_defaults(func=_cmd_doctor)

    p_init = sub.add_parser(
        "init", help="scaffold a starter targets.yaml in the current directory",
        description="Write a commented starter targets.yaml you can run "
                    "immediately with `ujin sweep`.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="example:\n  ujin init && ujin sweep targets.yaml",
    )
    p_init.add_argument("path", nargs="?", default="targets.yaml",
                        help="output file (default: targets.yaml)")
    p_init.add_argument("-f", "--force", action="store_true",
                        help="overwrite an existing file")
    p_init.set_defaults(func=_cmd_init)

    p_serve = sub.add_parser(
        "serve", help="run the poll engine (daemon)",
        description="Load targets.yaml and run the adaptive poll engine until "
                    "interrupted. Use `sweep` for a one-shot cron-style pass.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="example:\n  ujin serve targets.yaml",
    )
    p_serve.add_argument("targets", help="path to a targets.yaml (see `ujin init`)")
    p_serve.set_defaults(func=_cmd_serve)

    p_sweep = sub.add_parser(
        "sweep", help="poll all targets once and print what changed",
        description="Poll every target once and print which ones changed since "
                    "last run. Cron-friendly; exits when done.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="example:\n  ujin sweep targets.yaml",
    )
    p_sweep.add_argument("targets", help="path to a targets.yaml (see `ujin init`)")
    p_sweep.set_defaults(func=_cmd_sweep)

    p_api = sub.add_parser(
        "api", help="serve the poller control REST + WebSocket API (:8900)",
        description="Run the poller control plane: GET /health /metrics /targets, "
                    "POST /targets /sweep, WS /ws. Set UJIN_API_KEY to require auth.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="example:\n  ujin api targets.yaml --port 8900",
    )
    p_api.add_argument("targets", nargs="?", default=None,
                       help="optional targets.yaml to preload")
    p_api.add_argument("--host", default="0.0.0.0", help="bind address (default: 0.0.0.0)")
    p_api.add_argument("--port", type=int, default=8900, help="port (default: 8900)")
    p_api.set_defaults(func=_cmd_api)

    p_scrape = sub.add_parser(
        "scrape-serve", help="serve the rich scrape HTTP API (/scrape /feed ...) (:8901)",
        description="Run the one-shot scrape service: POST /scrape (modes "
                    "links|article|auto|combined|structured), /feed, /sitemap, "
                    "/discover, /capabilities. Configured via env (see docs/API.md).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="example:\n  ujin scrape-serve --port 8901   "
               "# then: curl localhost:8901/capabilities",
    )
    p_scrape.add_argument("--host", default="0.0.0.0", help="bind address (default: 0.0.0.0)")
    p_scrape.add_argument("--port", type=int, default=8901, help="port (default: 8901)")
    p_scrape.set_defaults(func=_cmd_scrape_serve)

    p_jobs = sub.add_parser(
        "jobs-serve", help="serve the unified job control plane (/jobs ...) (:8902)",
        description="Run the durable job control plane (source->transforms->sinks->"
                    "schedule). Preload jobs from a YAML arg and/or workflow files "
                    "from --workflows. State persists to $UJIN_JOBS_DB.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  ujin jobs-serve                              # empty, durable\n"
               "  ujin jobs-serve examples/jobs.crossref.yaml  # preload jobs\n"
               "  ujin jobs-serve --workflows ./workflows      # file-driven\n"
               "  ujin jobs-serve --plan ./ingest-plan.yaml    # many jobs, one file",
    )
    p_jobs.add_argument("jobs", nargs="?", default=None,
                        help="optional jobs.yaml to preload")
    p_jobs.add_argument("--workflows", default=None,
                        help="directory of workflow files to load (default: "
                             "$UJIN_WORKFLOWS_DIR or /workflows)")
    p_jobs.add_argument("--plan", default=None,
                        help="INGEST-PLAN file declaring many jobs at once "
                             "(overrides $UJIN_INGEST_PLAN)")
    p_jobs.add_argument(
        "--only", action="append", default=None, metavar="PATTERN",
        help="load only plan jobs whose id matches this fnmatch pattern "
             "(comma-separated or repeatable; applied after full resolution)")
    p_jobs.add_argument(
        "--exclude", action="append", default=None, metavar="PATTERN",
        help="exclude plan jobs whose id matches this fnmatch pattern "
             "(comma-separated or repeatable; subtracts from --only)")
    p_jobs.add_argument(
        "--tag", action="append", default=None, metavar="PATTERN",
        help="load only plan jobs carrying a `tags:` value matching this fnmatch "
             "pattern (comma-separated or repeatable; composes with --only/--exclude)")
    p_jobs.add_argument(
        "--exclude-tag", action="append", default=None, metavar="PATTERN",
        help="exclude plan jobs carrying a `tags:` value matching this fnmatch "
             "pattern (comma-separated or repeatable; subtracts from --tag)")
    p_jobs.add_argument(
        "--env-file", default=None, metavar="PATH",
        help="load KEY=VALUE pairs from PATH and use them for ${VAR} substitution "
             "when resolving the plan/workflows directory (process env vars take "
             "precedence; the file only fills in variables not already set)")
    p_jobs.add_argument(
        "--watch-plan", action="store_true",
        help="watch the --plan file and auto-reload (with prune) on change, no "
             "restart needed (off by default; also enabled by $UJIN_WATCH_PLAN)")
    p_jobs.add_argument(
        "--watch-interval", type=float, default=None, metavar="SECONDS",
        help="poll interval for --watch-plan, in seconds (default: 2.0; also "
             "$UJIN_WATCH_INTERVAL)")
    p_jobs.add_argument(
        "--profile", default=None, metavar="NAME",
        help="apply the named overlay from the plan's `profiles:` block over "
             "`defaults:` before the per-job merge (default: $UJIN_PLAN_PROFILE; "
             "the flag wins; unknown name exits non-zero)")
    p_jobs.add_argument("--host", default="0.0.0.0", help="bind address (default: 0.0.0.0)")
    p_jobs.add_argument("--port", type=int, default=8902, help="port (default: 8902)")
    p_jobs.set_defaults(func=_cmd_jobs_serve)

    p_plan = sub.add_parser(
        "plan", help="plan-level utilities (validate or render a plan file or workflows directory)",
        description="Subcommands for working with INGEST-PLAN files and workflows "
                    "directories without starting the server.",
    )
    plan_sub = p_plan.add_subparsers(dest="plan_cmd", required=True, metavar="<subcommand>")
    p_plan_validate = plan_sub.add_parser(
        "validate",
        help="validate a plan file or workflows dir without starting the server",
        description="Load an INGEST-PLAN file or a workflows directory using the same "
                    "loaders as `jobs-serve`, print each resolved job id and each "
                    "failure with an actionable error, then exit zero (all ok) or "
                    "non-zero (any failures). Unrecognized/misspelled keys are reported "
                    "as advisory `WARN` lines that do NOT affect the exit code unless "
                    "`--strict` is given. A missing or unreadable path exits non-zero "
                    "immediately with a clean `ujin: …` message (no traceback).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  ujin plan validate ./ingest-plan.yaml\n"
               "  ujin plan validate ./workflows/\n"
               "  ujin plan validate ./ingest-plan.yaml --json\n"
               "  ujin plan validate ./ingest-plan.yaml --strict   # fail CI on unknown keys\n"
               "  ujin plan validate ./ingest-plan.yaml --schema   # check document shape",
    )
    p_plan_validate.add_argument("path",
                                 help="INGEST-PLAN file or workflows directory to validate")
    p_plan_validate.add_argument("--json", action="store_true",
                                 help="emit machine-readable JSON instead of a human table")
    p_plan_validate.add_argument("--strict", action="store_true",
                                 help="treat unrecognized/misspelled keys as failures "
                                      "(non-zero exit) instead of advisory warnings")
    p_plan_validate.add_argument("--schema", action="store_true",
                                 help="also check the unresolved document(s) against the "
                                      "JSON Schema `ujin plan schema` emits, reporting "
                                      "structural shape violations (e.g. a non-list `jobs:`, "
                                      "a string `source:`) with the offending JSON path")
    p_plan_validate.add_argument(
        "--env-file", default=None, metavar="PATH",
        help="load KEY=VALUE pairs from PATH and use them for ${VAR} substitution "
             "when resolving the plan/workflows directory (process env vars take "
             "precedence; the file only fills in variables not already set)")
    p_plan_validate.add_argument(
        "--only", action="append", default=None, metavar="PATTERN",
        help="validate only jobs whose id matches this fnmatch pattern "
             "(comma-separated or repeatable; matched after full resolution)")
    p_plan_validate.add_argument(
        "--exclude", action="append", default=None, metavar="PATTERN",
        help="exclude jobs whose id matches this fnmatch pattern "
             "(comma-separated or repeatable; subtracts from --only)")
    p_plan_validate.add_argument(
        "--tag", action="append", default=None, metavar="PATTERN",
        help="validate only jobs carrying a `tags:` value matching this fnmatch "
             "pattern (comma-separated or repeatable; composes with --only/--exclude)")
    p_plan_validate.add_argument(
        "--exclude-tag", action="append", default=None, metavar="PATTERN",
        help="exclude jobs carrying a `tags:` value matching this fnmatch pattern "
             "(comma-separated or repeatable; subtracts from --tag)")
    p_plan_validate.add_argument(
        "--profile", default=None, metavar="NAME",
        help="apply the named overlay from the plan's `profiles:` block over "
             "`defaults:` before the per-job merge (default: $UJIN_PLAN_PROFILE; "
             "the flag wins; unknown name exits non-zero)")
    p_plan_validate.set_defaults(func=_cmd_plan_validate)

    p_plan_render = plan_sub.add_parser(
        "render",
        help="resolve and print every fully-expanded job in a plan or workflows dir",
        description="Load an INGEST-PLAN file or a workflows directory using the same "
                    "loaders as `jobs-serve` and `plan validate`, then print each "
                    "fully-expanded JobSpec — after defaults: deep-merge, "
                    "include:/use: fragment resolution, matrix:/for_each: fan-out, "
                    "and ${VAR}/${VAR:-default} substitution — in human-readable YAML. "
                    "--json emits the resolved specs as a JSON array (same shape as "
                    "the REST /jobs endpoint). "
                    "--explain annotates each job with its provenance — the "
                    "originating source file and, for matrix:/for_each: jobs, the "
                    "matrix entry index and variable map that produced it. "
                    "Exits zero when all jobs resolve; non-zero on any failure.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  ujin plan render ./ingest-plan.yaml\n"
               "  ujin plan render ./workflows/\n"
               "  ujin plan render ./ingest-plan.yaml --summary\n"
               "  ujin plan render ./ingest-plan.yaml --json\n"
               "  ujin plan render ./ingest-plan.yaml --explain\n"
               "  ujin plan render ./ingest-plan.yaml --explain --json",
    )
    p_plan_render.add_argument("path",
                               help="INGEST-PLAN file or workflows directory to render")
    p_plan_render.add_argument("--json", action="store_true",
                               help="emit resolved specs as a JSON array instead of YAML")
    p_plan_render.add_argument(
        "--summary", action="store_true",
        help="print a concise overview — total job count and one line per job "
             "(id, source kind/url, schedule, number of sinks) — instead of the "
             "full expanded specs; composes with --only/--exclude and --profile")
    p_plan_render.add_argument(
        "--explain", action="store_true",
        help="annotate each rendered job with its provenance (source file, and the "
             "matrix entry index + variable map for matrix/for_each-expanded jobs)")
    p_plan_render.add_argument(
        "--env-file", default=None, metavar="PATH",
        help="load KEY=VALUE pairs from PATH and use them for ${VAR} substitution "
             "when resolving the plan/workflows directory (process env vars take "
             "precedence; the file only fills in variables not already set)")
    p_plan_render.add_argument(
        "--only", action="append", default=None, metavar="PATTERN",
        help="render only jobs whose id matches this fnmatch pattern "
             "(comma-separated or repeatable; matched after full resolution)")
    p_plan_render.add_argument(
        "--exclude", action="append", default=None, metavar="PATTERN",
        help="exclude jobs whose id matches this fnmatch pattern "
             "(comma-separated or repeatable; subtracts from --only)")
    p_plan_render.add_argument(
        "--tag", action="append", default=None, metavar="PATTERN",
        help="render only jobs carrying a `tags:` value matching this fnmatch "
             "pattern (comma-separated or repeatable; composes with --only/--exclude)")
    p_plan_render.add_argument(
        "--exclude-tag", action="append", default=None, metavar="PATTERN",
        help="exclude jobs carrying a `tags:` value matching this fnmatch pattern "
             "(comma-separated or repeatable; subtracts from --tag)")
    p_plan_render.add_argument(
        "--profile", default=None, metavar="NAME",
        help="apply the named overlay from the plan's `profiles:` block over "
             "`defaults:` before the per-job merge (default: $UJIN_PLAN_PROFILE; "
             "the flag wins; unknown name exits non-zero)")
    p_plan_render.set_defaults(func=_cmd_plan_render)

    p_plan_diff = plan_sub.add_parser(
        "diff",
        help="diff two plans/workflows dirs by resolved job id",
        description="Resolve OLD and NEW (each a plan file or a workflows "
                    "directory) using the same loaders as `plan validate` and "
                    "`plan render` — defaults: deep-merge, include:/use: "
                    "fragments, matrix:/for_each: fan-out, and ${VAR} "
                    "substitution all expanded — then report, keyed by final job "
                    "id, the jobs added in NEW, removed in NEW, and changed "
                    "(present in both but with a differing resolved spec). Exits "
                    "zero whenever both inputs resolve (including no difference). "
                    "--exit-code (alias --check) changes the exit convention: "
                    "exits non-zero when there are any differences, zero when "
                    "the plans are identical — useful for CI gates. "
                    "--json emits the same information as a structured object "
                    "with distinct added/removed/changed groupings (unaffected "
                    "by --exit-code). An unreadable or schema-invalid input "
                    "exits non-zero with a clean `ujin: …` message (no traceback).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  ujin plan diff ./old-plan.yaml ./new-plan.yaml\n"
               "  ujin plan diff ./workflows/ ./new-workflows/\n"
               "  ujin plan diff ./old-plan.yaml ./new-plan.yaml --json\n"
               "  ujin plan diff ./old-plan.yaml ./new-plan.yaml --exit-code  "
               "# exit 1 if any diff",
    )
    p_plan_diff.add_argument("old", metavar="OLD",
                             help="baseline plan file or workflows directory")
    p_plan_diff.add_argument("new", metavar="NEW",
                             help="updated plan file or workflows directory")
    p_plan_diff.add_argument("--json", action="store_true",
                             help="emit the diff as machine-readable JSON with "
                                  "distinct added/removed/changed groupings")
    p_plan_diff.add_argument(
        "--exit-code", "--check", dest="exit_code", action="store_true",
        help="exit non-zero when the two resolved plans differ in any "
             "added/removed/changed job id; exit zero when they are identical "
             "(without this flag, plan diff always exits zero on a successful "
             "resolve regardless of differences)")
    p_plan_diff.add_argument(
        "--env-file", default=None, metavar="PATH",
        help="load KEY=VALUE pairs from PATH and use them for ${VAR} substitution "
             "when resolving both plans (process env vars take precedence; the "
             "file only fills in variables not already set)")
    p_plan_diff.set_defaults(func=_cmd_plan_diff)

    p_plan_schema = plan_sub.add_parser(
        "schema",
        help="print the JSON Schema for the INGEST-PLAN / workflow file format",
        description="Print a machine-readable JSON Schema (draft 2020-12) describing "
                    "the INGEST-PLAN / workflow file format ujin's plan and workflow "
                    "loaders accept: the per-job shape (source/transforms/sinks/"
                    "schedule), the top-level defaults: block, include:/use: fragment "
                    "references, matrix:/for_each: fan-out, vars: defaults, and "
                    "profiles: overlays. The schema is emitted to stdout as a single "
                    "deterministic JSON document; --out PATH writes it to a file "
                    "instead. It describes the unresolved file and is intentionally "
                    "permissive (defaults:/include:/matrix: are resolved before a "
                    "JobSpec is built), constraining the document shape and key types "
                    "— enough to reject a structurally-malformed plan while accepting "
                    "every shipped example. Read-only: it resolves nothing and never "
                    "starts the server.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  ujin plan schema\n"
               "  ujin plan schema --out ingest-plan.schema.json\n"
               "  ujin plan schema | python3 -m json.tool   # verify it parses as JSON",
    )
    p_plan_schema.add_argument(
        "--out", default=None, metavar="PATH",
        help="write the JSON Schema to PATH instead of stdout")
    p_plan_schema.set_defaults(func=_cmd_plan_schema)

    p_plan_env = plan_sub.add_parser(
        "env",
        help="report the environment variables a plan/workflow file references",
        description="Report the ${VAR} environment variables a plan file (or "
                    "workflows directory) requires — resolving include:/use: "
                    "fragments first, then collecting every ${VAR} reference. For "
                    "each variable it shows the default declared by its "
                    "${VAR:-default} form, whether any occurrence is required "
                    "(${VAR:?message}), and whether it is currently set in the "
                    "environment. Strictly read-only: it reuses the same _ENV_REF "
                    "regex and fragment-resolution the loaders use but never expands "
                    "${VAR} or alters resolution. A plan with no ${VAR} references "
                    "succeeds and reports an empty set; --json emits machine-readable "
                    "output and --env-file feeds the set/unset check (process env "
                    "wins). A missing/unreadable path exits non-zero with a clean "
                    "`ujin: …` message (no traceback).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  ujin plan env ./ingest-plan.yaml\n"
               "  ujin plan env ./workflows/\n"
               "  ujin plan env ./ingest-plan.yaml --json\n"
               "  ujin plan env ./ingest-plan.yaml --env-file .env",
    )
    p_plan_env.add_argument(
        "path", help="INGEST-PLAN file or workflows directory to inspect")
    p_plan_env.add_argument(
        "--json", action="store_true",
        help="emit machine-readable JSON instead of a human table")
    p_plan_env.add_argument(
        "--env-file", default=None, metavar="PATH",
        help="load KEY=VALUE pairs from PATH and treat them as set when determining "
             "which referenced variables are set (process env vars take precedence; "
             "the file only fills in variables not already set)")
    p_plan_env.set_defaults(func=_cmd_plan_env)

    p_plan_eject = plan_sub.add_parser(
        "eject",
        help="materialize resolved jobs to standalone single-job YAML files",
        description="Resolve an INGEST-PLAN file (or a workflows directory) using the "
                    "same loaders as `jobs-serve` and `plan render` — `defaults:` "
                    "deep-merge, `include:`/`use:` fragment inlining, "
                    "`matrix:`/`for_each:` fan-out, `profiles:` overlays, and "
                    "`${VAR}` substitution all applied — then write one YAML file per "
                    "resolved job id into the `--out` directory. Each file is named "
                    "`<id>.yaml` and contains only the fully-resolved JobSpec fields; "
                    "no `defaults:`, `include:`, `matrix:`, or `${VAR}` placeholders "
                    "remain. Loading any ejected file back through the single-file "
                    "workflow loader yields the same job spec it had inside the plan. "
                    "The output directory is created (including parents) if it does "
                    "not exist; existing files are overwritten. Exits zero when all "
                    "jobs resolve; non-zero on any failure.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  ujin plan eject ./ingest-plan.yaml --out ./ejected/\n"
               "  ujin plan eject ./workflows/ --out ./ejected/\n"
               "  ujin plan eject ./ingest-plan.yaml --out ./ejected/ --only 'feed-*'\n"
               "  ujin plan eject ./ingest-plan.yaml --out ./ejected/ "
               "--exclude feed-tech\n"
               "  ujin plan eject ./ingest-plan.yaml --out ./ejected/ --profile fast\n"
               "  ujin plan eject ./ingest-plan.yaml --out ./ejected/ "
               "--env-file .env",
    )
    p_plan_eject.add_argument("path",
                              help="INGEST-PLAN file or workflows directory to eject")
    p_plan_eject.add_argument(
        "--out", required=True, metavar="DIR",
        help="directory to write one <id>.yaml per resolved job into "
             "(created with parents if it does not exist; existing files are overwritten)")
    p_plan_eject.add_argument(
        "--env-file", default=None, metavar="PATH",
        help="load KEY=VALUE pairs from PATH and use them for ${VAR} substitution "
             "when resolving the plan/workflows directory (process env vars take "
             "precedence; the file only fills in variables not already set)")
    p_plan_eject.add_argument(
        "--only", action="append", default=None, metavar="PATTERN",
        help="eject only jobs whose id matches this fnmatch pattern "
             "(comma-separated or repeatable; matched after full resolution)")
    p_plan_eject.add_argument(
        "--exclude", action="append", default=None, metavar="PATTERN",
        help="exclude jobs whose id matches this fnmatch pattern "
             "(comma-separated or repeatable; subtracts from --only)")
    p_plan_eject.add_argument(
        "--tag", action="append", default=None, metavar="PATTERN",
        help="eject only jobs carrying a `tags:` value matching this fnmatch "
             "pattern (comma-separated or repeatable; composes with --only/--exclude)")
    p_plan_eject.add_argument(
        "--exclude-tag", action="append", default=None, metavar="PATTERN",
        help="exclude jobs carrying a `tags:` value matching this fnmatch pattern "
             "(comma-separated or repeatable; subtracts from --tag)")
    p_plan_eject.add_argument(
        "--profile", default=None, metavar="NAME",
        help="apply the named overlay from the plan's `profiles:` block over "
             "`defaults:` before the per-job merge (default: $UJIN_PLAN_PROFILE; "
             "the flag wins; unknown name exits non-zero)")
    p_plan_eject.set_defaults(func=_cmd_plan_eject)

    p_watch = sub.add_parser(
        "watch", help="watch a URL's regions for change (adaptive, jittered)",
        description="Poll one URL on the adaptive engine and report when the "
                    "selected regions change. Omit --selector to watch the whole "
                    "page; pass --webhook to POST change events instead of logging.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  ujin watch https://example.com\n"
               "  ujin watch https://example.com --selector main --selector .price\n"
               "  ujin watch https://example.com --webhook https://hooks/me",
    )
    p_watch.add_argument("url", help="page URL to watch")
    p_watch.add_argument("--selector", action="append", default=[], metavar="CSS",
                         help="CSS selector to watch (repeatable; omit for whole page)")
    p_watch.add_argument("--webhook", default=None, metavar="URL",
                         help="POST change events here (default: log to stdout)")
    p_watch.add_argument("--render", action="store_true",
                         help="render via obscura before extracting (JS pages)")
    p_watch.add_argument("--base", type=float, default=60.0,
                         help="starting interval in seconds (default: 60)")
    p_watch.add_argument("--min", type=float, default=5.0,
                         help="fastest interval in seconds (default: 5)")
    p_watch.add_argument("--max", type=float, default=3600.0,
                         help="slowest interval in seconds (default: 3600)")
    p_watch.set_defaults(func=_cmd_watch)

    p_learned = sub.add_parser(
        "learned", help="inspect the durable per-host learned state in a SiteStore db",
        description="Open an existing SiteStore database read-only and print, per "
                    "host, the learned adaptive state: recommended interval (via "
                    "ujin.adapt.derive_signals), concurrency factor, penalty/backoff "
                    "(health, cooldown, rate-limited), last observed status/latency, "
                    "and any observed robots Crawl-delay. With --strategy-db, also "
                    "show the recommended (backend, render_mode) for each host. "
                    "Defaults to a table; --json emits machine-readable output; "
                    "--host filters to one host.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  ujin learned site_state.db\n"
               "  ujin learned site_state.db --host example.com --json\n"
               "  ujin learned site_state.db --strategy-db strategy.db",
    )
    p_learned.add_argument("db", nargs="?", default=None,
                           help="path to a SiteStore database (the path your "
                                "poller/scraper persists to)")
    p_learned.add_argument("--host", default=None, metavar="HOST",
                           help="show only this host (default: every learned host)")
    p_learned.add_argument("--strategy-db", default=None, metavar="PATH",
                           help="also show StrategyFeedback.recommend(host) from "
                                "this strategy-feedback database")
    p_learned.add_argument("--json", action="store_true",
                           help="emit machine-readable JSON instead of a table")
    p_learned.set_defaults(func=_cmd_learned)

    p_mcp = sub.add_parser(
        "mcp-serve", help="run the MCP server for agents (stdio; --http for HTTP)",
        description="Expose scraping + the job control plane as MCP tools for "
                    "Claude Code / Desktop or any MCP client. Defaults to stdio.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  ujin mcp-serve                       # stdio (for `claude mcp add`)\n"
               "  ujin mcp-serve --http --port 8903    # streamable HTTP",
    )
    p_mcp.add_argument("--http", action="store_true",
                       help="streamable HTTP transport instead of stdio")
    p_mcp.add_argument("--host", default="127.0.0.1",
                       help="bind address for --http (default: 127.0.0.1)")
    p_mcp.add_argument("--port", type=int, default=8903,
                       help="port for --http (default: 8903)")
    p_mcp.set_defaults(func=_cmd_mcp_serve)

    p_obs = sub.add_parser(
        "obscura-build", help="init + build the bundled obscura renderer (needs cargo)",
        description="Init the obscura git submodule and build its release binary "
                    "(needs the Rust toolchain). The first build compiles V8 and is "
                    "slow (~15-20 min). Never run at pip-install time.",
    )
    p_obs.set_defaults(func=_cmd_obscura_build)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
