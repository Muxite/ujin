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
  POST   /plan/reload           re-read the configured INGEST-PLAN and upsert jobs
  WS     /jobs/events           live stream of change events

Needs the ``jobs`` extra (fastapi, uvicorn, websockets, pydantic, scrape stack).
"""
from __future__ import annotations

import asyncio
import difflib
import logging
import os
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

# Imported at module level so FastAPI can resolve the handler's `req: JobCreate`
# annotation (this module uses `from __future__ import annotations`, which turns
# annotations into strings resolved against module globals). Guarded so the
# package still imports without the jobs extra.
try:
    from fastapi import Request, WebSocket  # for handler annotations

    from .api_models import JobCreate
except ModuleNotFoundError:  # pragma: no cover - jobs extra missing
    JobCreate = None  # type: ignore[assignment,misc]
    Request = None  # type: ignore[assignment,misc]
    WebSocket = None  # type: ignore[assignment,misc]

log = logging.getLogger("ujin.jobs.app")

#: Env var holding a path to a single INGEST-PLAN file (YAML/JSON) declaring many
#: jobs at once (mountable as a volume). Mirrors ``UJIN_MARKETPLACE_PROFILES``; the
#: ``jobs-serve --plan PATH`` flag overrides it. Unset -> no plan is loaded.
INGEST_PLAN_ENV = "UJIN_INGEST_PLAN"

# ``${VAR}`` / ``${VAR:-default}`` / ``${VAR:?message}`` references in a
# workflow/job file, expanded against the environment at load time. Lets a
# mounted file reference secrets (an ingest token, a backend URL) without
# committing them. An unset variable with no default expands to "" (and logs
# once), mirroring shell semantics. The ``:?`` form raises an error when the
# variable is unset or empty, so a missing required secret is caught early.
#
# Regex groups: 1=name, 2=operator (``:-`` or ``:?``), 3=operand (may be "").
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:[-?])([^}]*))?\}")


class EnvVarRequiredError(ValueError):
    """A ``${VAR:?message}`` env var is unset or empty at load time.

    Raised during expansion so the offending plan/workflow lands in the
    ``failed`` list (``GET /health``) with an actionable message, rather than
    expanding silently or crashing startup for every other job.
    """


def _expand_env(text: str) -> str:
    """Expand ``${VAR}`` / ``${VAR:-default}`` / ``${VAR:?message}`` in *text*."""
    def _sub(m: "re.Match[str]") -> str:
        name, op, operand = m.group(1), m.group(2), m.group(3)
        val = os.environ.get(name)
        if op == ":-":
            return val if val is not None else operand
        if op == ":?":
            if not val:  # unset or empty
                detail = f" — {operand.strip()}" if operand and operand.strip() else ""
                raise EnvVarRequiredError(
                    f"required variable {name!r} is unset or empty{detail}"
                )
            return val
        # plain ${VAR} — no operator
        if val is None:
            log.warning("workflow env var %s unset; expanding to empty", name)
            return ""
        return val

    return _ENV_REF.sub(_sub, text)


def _load_env_file(path: Path) -> "dict[str, str]":
    """Parse a KEY=VALUE env-file into a dict.

    Blank lines and lines whose first non-whitespace character is ``#`` are
    skipped. A line missing ``=`` or carrying an invalid identifier key raises
    ``ValueError`` naming the line number so the CLI can emit a clean
    ``ujin: …`` error without a traceback.
    """
    result: dict[str, str] = {}
    text = path.read_text(encoding="utf-8")
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(
                f"env-file {path}: line {i}: expected KEY=VALUE, got {line!r}"
            )
        key, _, value = line.partition("=")
        key = key.rstrip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            raise ValueError(
                f"env-file {path}: line {i}: invalid variable name {key!r}"
            )
        result[key] = value
    return result


# ── deep-merge + reusable fragments (``defaults:`` / ``include:``) ─────────── #
#
# Both features are strictly additive: a workflow file that uses neither resolves
# to exactly the structure it parsed to, so deterministic ids and ${VAR} handling
# are byte-for-byte unchanged. ``defaults:`` is deep-merged *under* each job (job
# keys win); ``include:``/``use:`` splices a fragment file in as if it were inlined.

# Keys (in any mapping, or a list item) that pull in a reusable fragment file.
_INCLUDE_KEYS = ("include", "use")


class WorkflowIncludeError(ValueError):
    """A workflow ``include:``/``use:`` fragment is missing, unreadable, or cyclic.

    Raised during workflow parsing so the offending file lands in the ``failed``
    list (see ``GET /health``) with an actionable message, rather than aborting
    startup for every other workflow.
    """


def _deep_merge(base: Any, over: Any) -> Any:
    """Recursively merge *over* onto *base*; *over* wins on conflict.

    Nested mappings merge key-by-key; lists and scalars in *over* replace the
    corresponding value in *base* (no concatenation). A key present only in
    *base* is kept; a key present only in *over* is added.
    """
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = _deep_merge(out[k], v) if k in out else v
        return out
    return over


def _has_include(node: Any) -> bool:
    return isinstance(node, dict) and any(k in node for k in _INCLUDE_KEYS)


def _split_include(node: dict) -> "tuple[list[str], dict]":
    """Pull out fragment refs from ``include:``/``use:`` keys; keep the rest."""
    refs: list[str] = []
    rest: dict[str, Any] = {}
    for k, v in node.items():
        if k in _INCLUDE_KEYS:
            if isinstance(v, str):
                refs.append(v)
            elif isinstance(v, (list, tuple)):
                refs.extend(v)
            else:
                raise WorkflowIncludeError(
                    f"`{k}:` must be a fragment path or list of paths, "
                    f"got {type(v).__name__}"
                )
        else:
            rest[k] = v
    return refs, rest


def _resolve_fragment_path(ref: str, root: Path) -> Path:
    """Locate a fragment file, relative to *root* then ``$UJIN_WORKFLOWS_DIR``."""
    p = Path(ref)
    if p.is_absolute():
        if p.is_file():
            return p.resolve()
        raise WorkflowIncludeError(f"include fragment not found: {ref}")
    bases = [root]
    env_dir = os.environ.get("UJIN_WORKFLOWS_DIR")
    if env_dir:
        bases.append(Path(env_dir))
    tried = []
    for base in bases:
        cand = base / p
        tried.append(str(cand))
        if cand.is_file():
            return cand.resolve()
    raise WorkflowIncludeError(
        f"include fragment not found: {ref!r} (looked in: {', '.join(tried)})"
    )


def _load_fragment(ref: str, root: Path, seen: "tuple[Path, ...]") -> Any:
    """Parse a fragment file and recursively resolve its own includes."""
    import yaml

    fp = _resolve_fragment_path(ref, root)
    if fp in seen:
        chain = " -> ".join(p.name for p in (*seen, fp))
        raise WorkflowIncludeError(f"cyclic include detected: {chain}")
    text = _expand_env(fp.read_text(encoding="utf-8"))
    return _resolve_includes(yaml.safe_load(text), fp.parent, seen + (fp,))


def _resolve_includes(node: Any, root: Path, seen: "tuple[Path, ...]" = ()) -> Any:
    """Inline every ``include:``/``use:`` fragment found anywhere in *node*.

    A mapping with an ``include:`` is deep-merged *over* the referenced
    fragment(s) (the mapping's own keys win; multiple refs apply left-to-right). A
    list item whose include expands to a list is spliced into the list (so a
    transform-pipeline fragment drops straight into ``transforms:``). With no
    include keys present, *node* is returned structurally unchanged.
    """
    if isinstance(node, dict):
        refs, rest = _split_include(node)
        resolved = {k: _resolve_includes(v, root, seen) for k, v in rest.items()}
        if not refs:
            return resolved
        merged: Any = None
        for ref in refs:
            frag = _load_fragment(ref, root, seen)
            merged = frag if merged is None else _deep_merge(merged, frag)
        if isinstance(merged, list):
            if resolved:
                raise WorkflowIncludeError(
                    f"cannot merge keys {sorted(resolved)} onto a list fragment "
                    f"(include {refs}); put the overrides inside the fragment"
                )
            return merged
        return _deep_merge(merged, resolved)
    if isinstance(node, list):
        out: list = []
        for item in node:
            resolved = _resolve_includes(item, root, seen)
            if _has_include(item) and isinstance(resolved, list):
                out.extend(resolved)
            else:
                out.append(resolved)
        return out
    return node


# ── matrix / for_each fan-out ──────────────────────────────────────────────── #
#
# A self-contained, additive step that runs *after* defaults/include resolution
# (Track 1) and *before* id derivation: a job mapping carrying ``matrix:`` (alias
# ``for_each:``) — a list of variable maps, or a ``{from: <path>}`` mapping naming
# an external data file that supplies that list — is fanned into one job per map,
# with each map's variables substituted into every ``{{ var }}`` placeholder across
# the template (source/transforms/sinks/schedule/name/id/...). Kept in its own code
# path so it neither entangles with, nor conflicts much with, the includes work.
# A file carrying neither key passes through structurally unchanged.

_MATRIX_KEYS = ("matrix", "for_each")

# ``{{ var }}`` placeholder (whitespace-tolerant). Distinct from the ``${VAR}``
# env syntax (already expanded at file-read time), so the two never collide.
_VAR_REF = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def _has_matrix(node: Any) -> bool:
    return isinstance(node, dict) and any(k in node for k in _MATRIX_KEYS)


def _subst_vars(node: Any, vars: dict[str, Any]) -> Any:
    """Deep-copy *node*, substituting ``{{ var }}`` placeholders from *vars*.

    A string that is *exactly* one placeholder is replaced by the variable's raw
    value (type-preserving — an int stays an int, a map stays a map). A
    placeholder embedded in a larger string interpolates ``str(value)``. Unknown
    variables are left verbatim (permissive: a stray ``{{ x }}`` is not fatal).
    Only values are substituted; mapping keys are left as written.
    """
    if isinstance(node, dict):
        return {k: _subst_vars(v, vars) for k, v in node.items()}
    if isinstance(node, list):
        return [_subst_vars(v, vars) for v in node]
    if isinstance(node, str):
        whole = _VAR_REF.fullmatch(node.strip())
        if whole and whole.group(1) in vars:
            return vars[whole.group(1)]
        return _VAR_REF.sub(
            lambda m: str(vars[m.group(1)]) if m.group(1) in vars else m.group(0),
            node,
        )
    return node


def _load_matrix_file(value: dict, key: str, root: "Path | None") -> list:
    """Load matrix rows from an external data file (``matrix: {from: <path>}``).

    The mapping value must carry exactly a ``from:`` string naming a YAML/JSON data
    file. The path is resolved *the same way an ``include:``/``use:`` fragment is*
    — relative to the containing file's directory, then ``$UJIN_WORKFLOWS_DIR`` —
    via :func:`_resolve_fragment_path`. ``${VAR}`` is expanded at read time, like
    every other workflow/plan file. The file must parse to a **non-empty list of
    variable maps** (the same shape an inline ``matrix:`` list would hold).

    Every failure (a bad ``from:``, a missing/unreadable file, a file that is not a
    non-empty list of maps) raises ``ValueError`` naming the offending path, so the
    containing workflow/plan lands in the loader's ``failed`` list (``GET /health``)
    rather than aborting startup.
    """
    import yaml

    ref = value.get("from")
    extra = sorted(k for k in value if k != "from")
    if not isinstance(ref, str) or not ref:
        raise ValueError(
            f"`{key}: {{from: ...}}` needs a `from:` data-file path (a string)"
        )
    if extra:
        raise ValueError(
            f"`{key}: {{from: ...}}` accepts only `from:`, not {extra}"
        )
    if root is None:
        raise ValueError(
            f"`{key}:` data file {ref!r} cannot be resolved without a base directory"
        )
    try:
        fp = _resolve_fragment_path(ref, root)
    except WorkflowIncludeError as exc:
        raise ValueError(f"`{key}:` data file not found: {ref!r}") from exc
    try:
        rows = yaml.safe_load(_expand_env(fp.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"`{key}:` data file {ref!r} could not be read: {exc}") from exc
    if not isinstance(rows, list) or not rows:
        raise ValueError(
            f"`{key}:` data file {ref!r} must contain a non-empty list of "
            "variable maps"
        )
    return rows


def _expand_matrix(entry: dict, base_id: str, root: "Path | None" = None) -> list:
    """Fan one job mapping out over its ``matrix:``/``for_each:`` rows.

    The matrix value is either an **inline list** of variable maps, or a mapping
    ``{from: <path>}`` naming an external data file that supplies that list (see
    :func:`_load_matrix_file`); both yield the same rows. Returns one dict per
    variable map, each a copy of *entry* (minus the matrix key) with the map's
    variables substituted in. A stable, distinct id/name is derived per generated
    job: an explicit ``id`` template (e.g. ``id: feed-{{ slug }}``) is honored after
    substitution, otherwise ``<base_id>-<index>``. *root* is the containing file's
    directory, used to resolve a ``from:`` data file. Raises ``ValueError`` (so the
    file lands in the ``failed`` list, not aborting startup) on a malformed matrix
    or colliding ids.
    """
    present = [k for k in _MATRIX_KEYS if k in entry]
    if len(present) > 1:
        raise ValueError(f"use only one of {present}, not both")
    key = present[0]
    value = entry[key]
    rows = _load_matrix_file(value, key, root) if isinstance(value, dict) else value
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"`{key}:` must be a non-empty list of variable maps")
    template = {k: v for k, v in entry.items() if k not in _MATRIX_KEYS}

    out: list = []
    seen: set = set()
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(
                f"`{key}:` entry {i} must be a variable map, got "
                f"{type(row).__name__}"
            )
        d = _subst_vars(template, row)
        d.setdefault("id", f"{base_id}-{i}")
        d.setdefault("name", d["id"])
        if d["id"] in seen:
            raise ValueError(
                f"`{key}:` produced duplicate id {d['id']!r}; give the `id:` "
                "template a variable that differs per entry"
            )
        seen.add(d["id"])
        out.append(d)
    return out


def _expand_matrix_entries(entries: list, stem: str, root: "Path | None" = None) -> list:
    """Replace each matrix-bearing entry with its fan-out; pass others through.

    A list with no matrix anywhere is returned unchanged, so matrix-free files
    parse to exactly the same entries (and thus the same JobSpecs) as before.
    *root* (the containing file's directory) is threaded to :func:`_expand_matrix`
    so a ``matrix: {from: <path>}`` data file resolves relative to it.
    """
    if not any(_has_matrix(e) for e in entries):
        return entries
    out: list = []
    for i, entry in enumerate(entries):
        if not _has_matrix(entry):
            out.append(entry)
            continue
        base = entry.get("id") or (stem if len(entries) == 1 else f"{stem}-{i}")
        out.extend(_expand_matrix(entry, base, root))
    return out


# ── schema validation: flag misspelled / unrecognized keys ─────────────────── #
#
# Strictly additive and advisory by default: an unrecognized key (e.g. a `defaluts:`
# typo for `defaults:`) is *reported* — a log warning, and, for `ujin plan validate`,
# collected into a `warnings` list — but never changes which jobs load, so a file
# that was valid before stays valid. `ujin plan validate --strict` turns these
# warnings into failures for CI use. The checks run *after* include+defaults
# resolution, so only keys the loader would otherwise ignore are flagged.

# Recognized keys of a single job mapping. Mirrors JobSpec.from_dict's fields plus
# the additive compose/fan-out keys consumed before the spec is built (`include:`/
# `use:` splice, `matrix:`/`for_each:` fan-out).
_JOB_KEYS = frozenset({
    "name", "id", "source", "transforms", "sinks", "schedule", "enabled",
    "created_at", "matrix", "for_each", "include", "use",
})

# Top-level keys of an INGEST-PLAN *mapping* (a top-level list has no keys to check).
_PLAN_TOP_KEYS = frozenset({"jobs", "defaults", "include", "use"})

# Top-level keys of a WORKFLOW file: it may be a container (`jobs:`/`defaults:`) OR a
# single job written directly at the top level, so both key sets are accepted there.
_WORKFLOW_TOP_KEYS = _JOB_KEYS | {"jobs", "defaults"}


def _suggest(unknown: str, allowed: "frozenset[str]") -> "str | None":
    """Closest valid key to *unknown* for a 'did you mean …?' hint, or None."""
    matches = difflib.get_close_matches(unknown, sorted(allowed), n=1, cutoff=0.6)
    return matches[0] if matches else None


def _key_problems(
    mapping: Any, allowed: "frozenset[str]", context: str, *, top: bool = False
) -> "list[str]":
    """Actionable message for each key in *mapping* not in *allowed* (``[]`` if none).

    Each message names *context* (the file, plus the job id when applicable) and the
    offending key, with a ``did you mean <closest>?`` hint when a near match exists.
    A *mapping* that is not a dict yields nothing.
    """
    if not isinstance(mapping, dict):
        return []
    noun = "top-level key" if top else "key"
    out: list[str] = []
    for key in mapping:
        if key in allowed:
            continue
        hint = _suggest(str(key), allowed)
        msg = f"ujin: {context}: unrecognized {noun} {key!r}"
        if hint:
            msg += f"; did you mean {hint!r}?"
        out.append(msg)
    return out


def _report_warnings(messages: "list[str]", warnings: "list | None") -> None:
    """Log each schema warning; also collect into *warnings* when a list is given."""
    for msg in messages:
        log.warning(msg)
        if warnings is not None:
            warnings.append(msg)


# ── job-id selection (--only / --exclude) ──────────────────────────────────── #
#
# Applied AFTER full resolution (includes, defaults, matrix fan-out, ${VAR})
# so patterns always match the final expanded ids. Strictly additive: with no
# patterns the loader and health output are byte-for-byte unchanged.


def _select_jobs(specs, only=None, exclude=None):
    """Filter *specs* by fnmatch job-id patterns.

    ``only``    — keep jobs whose id matches ANY pattern; applied first.
    ``exclude`` — remove jobs whose id matches ANY exclude pattern.
    Both ``None`` and ``[]`` mean "no filter" — the list is returned unchanged.
    Raises ``ValueError`` (clean ``ujin: …`` message) when ``only`` is given
    but no job id matches, so callers can exit non-zero without a traceback.
    """
    from fnmatch import fnmatch

    if not only and not exclude:
        return specs

    def _m(job_id, patterns):
        return any(fnmatch(job_id, p) for p in patterns)

    if only:
        selected = [s for s in specs if _m(s.id, only)]
        if not selected:
            pats = ", ".join(repr(p) for p in only)
            raise ValueError(
                f"ujin: --only {pats}: no job ids matched "
                f"({len(specs)} job(s) resolved)"
            )
    else:
        selected = list(specs)

    if exclude:
        selected = [s for s in selected if not _m(s.id, exclude)]

    return selected


def _preload_specs(path: str) -> list:
    """Parse a jobs.yaml (top-level list, or {jobs: [...]}) into JobSpecs."""
    import yaml

    from .model import JobSpec

    text = _expand_env(open(path, encoding="utf-8").read())
    data = yaml.safe_load(text) or {}
    raw = data.get("jobs", data) if isinstance(data, dict) else data
    return [JobSpec.from_dict(d) for d in (raw or [])]


def _specs_from_workflow_file(path, warnings: list | None = None) -> list:
    """Parse one workflow file into JobSpecs, deriving stable ids from the stem.

    A workflow file is the same declarative shape as a job. The filename stem is
    the **workflow id** (and default name) so the same file maps to the same
    workflow across restarts/redeploys — unless the file sets an explicit ``id``.
    A file may also hold a list / ``{jobs: [...]}``; entries without an ``id``
    fall back to ``<stem>-<index>`` to stay deterministic.

    Three additive conveniences run before id derivation: ``include:``/``use:``
    fragments are inlined (relative to the file's dir, then ``$UJIN_WORKFLOWS_DIR``),
    a top-level ``defaults:`` mapping is deep-merged *under* every job (per-job keys
    win), and a ``matrix:``/``for_each:`` key fans a job out into one per variable
    map — given inline, or by a ``{from: <path>}`` data file resolved like an
    ``include:`` fragment — with each map substituted into the merged result (see
    ``_expand_matrix``).
    The three layers compose in that order; a file using none resolves byte-for-byte
    as it did before.
    """
    import yaml

    from .model import JobSpec

    path = Path(path)
    stem = path.stem
    data = yaml.safe_load(_expand_env(path.read_text(encoding="utf-8"))) or {}

    # Inline reusable fragments, then peel off a top-level `defaults:` block. Both
    # are no-ops when absent, so the paths below stay identical to the old loader.
    data = _resolve_includes(data, path.parent)

    # Advisory schema check on the resolved top level. A workflow file's top level
    # may be a container (`jobs:`/`defaults:`) OR a single job written directly, so
    # both key sets are valid here; an unrecognized key (a typo) is reported, never
    # silently dropped. Single-job files validate fully here; `jobs:` lists also get
    # a per-entry check below.
    _report_warnings(
        _key_problems(data, _WORKFLOW_TOP_KEYS, f"workflow {path.name}", top=True),
        warnings,
    )

    defaults = data.pop("defaults", None) or {} if isinstance(data, dict) else {}

    # Single-job mapping (no top-level `jobs:` list) -> the whole file is one job.
    # A `matrix:`/`for_each:` key turns that mapping into a *template* fanned into
    # several jobs; without one it stays a single job exactly as before.
    if isinstance(data, dict) and "jobs" not in data:
        merged = _deep_merge(defaults, data) if defaults else dict(data)
        if not _has_matrix(merged):
            merged.setdefault("id", stem)
            merged.setdefault("name", stem)
            return [JobSpec.from_dict(merged)]
        raw = [merged]
    else:
        raw = data.get("jobs", data) if isinstance(data, dict) else data
        raw = [
            (_deep_merge(defaults, entry) if defaults else dict(entry))
            for entry in (raw or [])
        ]
        # Per-entry schema check (each merged job mapping in a `jobs:` list).
        for i, merged in enumerate(raw):
            base = (merged.get("id") if isinstance(merged, dict) else None) or (
                stem if len(raw) == 1 else f"{stem}-{i}"
            )
            _report_warnings(
                _key_problems(merged, _JOB_KEYS, f"workflow {path.name} job {base}"),
                warnings,
            )

    # Matrix expansion is a distinct, additive step that runs *after* the
    # defaults/include merge above, so each entry's variables substitute into the
    # already-merged result: matrix-free entries pass through untouched (same ids),
    # matrix entries fan out with stable, per-entry ids.
    raw = _expand_matrix_entries(raw, stem, path.parent)
    specs = []
    for i, entry in enumerate(raw):
        d = dict(entry)
        d.setdefault("id", stem if len(raw) == 1 else f"{stem}-{i}")
        d.setdefault("name", d["id"])
        specs.append(JobSpec.from_dict(d))
    return specs


def _load_workflows_dir(
    path: str, failed: list | None = None, warnings: list | None = None
) -> list:
    """Load every top-level ``*.yaml``/``*.yml`` workflow file in *path* (sorted).

    The scan is **non-recursive** (``*.y*ml``, not ``**/*.y*ml``): only files
    directly in *path* are treated as workflows. This is deliberate — it keeps
    ``include:``/``use:`` fragments kept in a subdirectory (e.g. ``fragments/``)
    out of the workflow set, so a fragment is never loaded as a standalone
    workflow (matching ``docs/WORKFLOWS.md``).

    A file that fails to parse or resolve (bad YAML, missing/cyclic ``include:``)
    is logged and skipped so other workflows still load; when *failed* is given,
    a ``{"id", "error"}`` record is appended to it for ``GET /health``.

    Unrecognized/misspelled keys are *advisory*: each is logged and, when *warnings*
    is given, its ``ujin: …`` message appended there (used by ``ujin plan validate``
    ``--strict``). They never change which workflows load — additive by default.
    """
    root = Path(path)
    if not root.is_dir():
        return []
    specs = []
    # Non-recursive on purpose (no ``**``): subdirectory fragments stay out of
    # the workflow set — see the docstring. The dir-scan tests guard this.
    for fp in sorted(root.glob("*.y*ml")):
        try:
            specs.extend(_specs_from_workflow_file(fp, warnings=warnings))
        except Exception as exc:  # noqa: BLE001
            log.warning("skipping workflow file %s: %s", fp, exc)
            if failed is not None:
                failed.append({"id": fp.stem, "error": str(exc)})
    return specs


# ── INGEST-PLAN: many jobs from one mountable file ─────────────────────────── #
#
# A plan is a single YAML/JSON file declaring MANY jobs at once — the file-driven
# counterpart to the workflows directory when you'd rather mount one file than a
# folder. It reuses the very same additive layers as a workflow file (``include:``/
# ``use:`` fragments, a top-level ``defaults:`` deep-merged under each job, and
# per-job ``matrix:``/``for_each:`` fan-out), so a plan using none of them resolves
# to exactly the jobs of the equivalent plain list. Resolved from ``--plan`` /
# ``$UJIN_INGEST_PLAN`` and loaded on startup alongside the positional jobs.yaml
# preload and the workflows-dir scan. Strictly generic: no site-specific logic.


def _plan_entries(data: Any, name: str) -> "tuple[dict, list]":
    """Split a parsed plan into ``(defaults, entries)``; validate the top shape.

    A plan is either a top-level **list** of job mappings, or a **mapping** with a
    ``jobs:`` list plus an optional ``defaults:`` mapping. An empty file is treated
    as zero jobs. Anything else (a bare scalar, a mapping without ``jobs:``, a
    non-list ``jobs:``/``defaults:``) raises ``ValueError`` naming *name* so the
    caller can report it instead of crashing startup.
    """
    if data is None:
        return {}, []
    if isinstance(data, list):
        return {}, data
    if isinstance(data, dict):
        defaults = data.get("defaults") or {}
        if defaults and not isinstance(defaults, dict):
            raise ValueError(
                f"plan {name}: `defaults:` must be a mapping, got "
                f"{type(defaults).__name__}"
            )
        if "jobs" not in data:
            raise ValueError(
                f"plan {name}: a mapping plan must have a `jobs:` list "
                "(or make the file a top-level list of jobs)"
            )
        jobs = data["jobs"]
        if not isinstance(jobs, list):
            raise ValueError(
                f"plan {name}: `jobs:` must be a list, got {type(jobs).__name__}"
            )
        return defaults, jobs
    raise ValueError(
        f"plan {name}: must be a list of jobs or a mapping with a `jobs:` list, "
        f"got {type(data).__name__}"
    )


def _load_ingest_plan(
    path,
    failed: list | None = None,
    warnings: list | None = None,
    present: list | None = None,
) -> list:
    """Load an INGEST-PLAN file into JobSpecs, deriving stable per-job ids.

    Each resulting job gets a stable id (explicit ``id`` wins; otherwise
    ``<plan-stem>-<index>``, with a per-entry ``matrix:`` suffix) so re-loading the
    plan upserts the same jobs rather than duplicating them. The three additive
    layers run in the same order as a workflow file: ``include:``/``use:`` fragments
    are inlined first (relative to the plan's directory then ``$UJIN_WORKFLOWS_DIR``),
    a top-level ``defaults:`` mapping is deep-merged *under* every job (per-job keys
    win), then ``matrix:``/``for_each:`` fans an entry into one job per variable map
    (the variable maps given inline, or by a ``{from: <path>}`` data file resolved
    like an ``include:`` fragment).

    Errors never abort startup. A **file-level** problem (missing/unreadable file,
    invalid YAML, a shape that is neither a mapping nor a list, an unresolvable or
    cyclic top-level include) fails the whole plan as one ``{"id", "error"}`` record.
    A **per-job** problem (a bad ``matrix:``, a colliding id, a spec that won't
    build) fails just that job; the remaining valid jobs still load. When *failed*
    is given the ``ujin: ...``-style message (naming the offending file/job) is
    appended to it for ``GET /health``.

    Unrecognized/misspelled top-level or per-job keys are *advisory*: each is logged
    and, when *warnings* is given, its ``ujin: …`` message appended there (used by
    ``ujin plan validate --strict``). They never change which jobs load.

    When *present* is given it collects the **derived id of every entry the plan
    resolved to** — whether the spec built or not — so a caller can tell which job
    ids the file currently declares (used by the reload prune mode as the keep-set).
    A file-level failure (the file never parses into entries) leaves *present*
    untouched, distinguishing "couldn't read the plan" from "the plan is empty".
    """
    import yaml

    from .model import JobSpec

    path = Path(path)
    stem = path.stem
    specs: list = []

    # ── file-level parse: one failure record covers the whole plan ──
    try:
        data = yaml.safe_load(_expand_env(path.read_text(encoding="utf-8")))
        data = _resolve_includes(data, path.parent)
        defaults, entries = _plan_entries(data, path.name)
    except (OSError, yaml.YAMLError, WorkflowIncludeError, ValueError) as exc:
        log.warning("skipping ingest plan %s: %s", path, exc)
        if failed is not None:
            failed.append({"id": stem, "error": f"ujin: {exc}"})
        return specs

    # Advisory top-level schema check: an unrecognized top-level key — e.g.
    # `defaluts:` for `defaults:` — is reported, not silently dropped. Only a
    # mapping plan has top-level keys; a top-level list plan has none.
    _report_warnings(
        _key_problems(data, _PLAN_TOP_KEYS, f"plan {path.name}", top=True),
        warnings,
    )

    # ── per-job resolution: isolate matrix/build errors so one bad job is
    # reported while the rest still load. Ids stay stable across reloads. ──
    seen: set = set()
    for i, entry in enumerate(entries):
        base = (entry.get("id") if isinstance(entry, dict) else None) or (
            stem if len(entries) == 1 else f"{stem}-{i}"
        )
        try:
            merged = _deep_merge(defaults, entry) if defaults else dict(entry)
            # Advisory per-job schema check on the merged mapping (names the file
            # and job id); reported, never silently dropped, and the job still loads.
            _report_warnings(
                _key_problems(merged, _JOB_KEYS, f"plan job {base} in {path.name}"),
                warnings,
            )
            # Reuse the workflow matrix helper on this single entry so `matrix:`/
            # `for_each:` behaves byte-for-byte as it does for workflow files
            # (including a `matrix: {from: <path>}` data file relative to the plan).
            fanned = _expand_matrix_entries([merged], base, path.parent)
            for j, raw in enumerate(fanned):
                d = dict(raw)
                d.setdefault("id", base if len(fanned) == 1 else f"{base}-{j}")
                d.setdefault("name", d["id"])
                if d["id"] in seen:
                    raise ValueError(
                        f"duplicate job id {d['id']!r} (give the entry an explicit "
                        "unique `id:`)"
                    )
                seen.add(d["id"])
                if present is not None:
                    present.append(d["id"])
                specs.append(JobSpec.from_dict(d))
        except Exception as exc:  # noqa: BLE001
            log.warning("skipping plan job %s in %s: %s", base, path, exc)
            if failed is not None:
                failed.append({"id": base, "error": f"ujin: plan job {base}: {exc}"})
            # The entry still names a job the plan declares; record its id so prune
            # keeps it (a transient build error must not drop a still-present job).
            if present is not None:
                present.append(base)
    return specs


def _apply_ingest_plan(
    manager, plan_src, only=None, exclude=None, *, prune=False
) -> "dict[str, list]":
    """Resolve *plan_src*, apply ``--only``/``--exclude``, upsert into *manager*.

    Shared by startup and ``POST /plan/reload`` so both go through the identical
    :func:`_load_ingest_plan` resolution (defaults/include/matrix/``${VAR}``),
    :func:`_select_jobs` selection, and error handling. Returns the same
    ``{"path", "loaded", "failed"}`` summary that ``GET /health`` exposes: a
    loader/file-level or per-job problem fails only that record (with a
    ``ujin: …`` message) while the remaining jobs upsert by their stable ids.
    Every upserted job is tagged with ``"plan"`` provenance so prune can find them.
    Re-applying is idempotent — same ids upsert the same jobs.

    With *prune* set, plan-owned jobs whose ids are no longer declared by the
    re-read plan are removed from *manager* and reported in a ``"pruned"`` list
    added to the summary. Only jobs the manager tracks as plan-owned are eligible —
    API-created and positional-preload jobs are never pruned, even when absent from
    the plan. Prune is **skipped** when the plan fails to read at the file level
    (an empty id set there can't be trusted), so a transient read error never wipes
    plan jobs. With *prune* off the summary omits ``"pruned"`` and nothing is
    removed — byte-for-byte the upsert-only behaviour.

    Raises ``ValueError`` (clean ``ujin: …`` message) when an ``--only`` pattern
    matches no resolved job, so the caller decides whether that is fatal (startup
    exits non-zero) or merely reported (reload keeps the server running).
    """
    plan_status: dict[str, list] = {
        "path": str(plan_src), "loaded": [], "failed": [],
    }
    # `present` collects the ids the plan currently declares (built or not); it
    # stays empty on a file-level read failure, which gates prune below.
    present: list[str] = []
    plan_specs = _load_ingest_plan(
        plan_src, failed=plan_status["failed"], present=present
    )
    if only is not None or exclude is not None:
        plan_specs = _select_jobs(plan_specs, only, exclude)
    for spec in plan_specs:
        try:
            manager.create(spec, origin="plan")
            plan_status["loaded"].append(spec.id)
        except Exception as exc:  # noqa: BLE001
            log.warning("plan job %s failed: %s", spec.id, exc)
            plan_status["failed"].append({"id": spec.id, "error": f"ujin: {exc}"})
    if prune:
        # Keep any plan-owned job whose id the file still declares; drop the rest.
        # Skip entirely on a file-level read failure (present empty + a failure
        # record) so a broken file never removes still-wanted jobs. A genuinely
        # empty plan (present empty, no failures) correctly prunes all plan jobs.
        keep = set(present)
        file_failed = not present and bool(plan_status["failed"])
        pruned: list[str] = []
        if not file_failed:
            for jid in manager.plan_owned_ids():
                if jid not in keep:
                    manager.delete(jid)
                    pruned.append(jid)
        plan_status["pruned"] = sorted(pruned)
    return plan_status


def create_jobs_app(
    config_path: str | None = None,
    *,
    workflows_dir: str | None = None,
    plan_path: str | None = None,
    scrape_config: Any = None,
    run_engine: bool = True,
    only: list | None = None,
    exclude: list | None = None,
) -> Any:
    try:
        from fastapi import (
            FastAPI,
            HTTPException,
            Request,
            WebSocket,
            WebSocketDisconnect,
        )
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
    # The `--plan` flag (passed as plan_path) overrides $UJIN_INGEST_PLAN; unset ->
    # no plan is loaded and /health stays byte-identical to a no-plan deploy.
    plan_src = plan_path or os.environ.get(INGEST_PLAN_ENV)

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
                    manager.create(spec, origin="preload")
                except Exception as exc:  # noqa: BLE001
                    log.warning("preload job %s failed: %s", spec.name, exc)

        # "Setup" phase: register every workflow file in the mounted directory.
        # Ids are filename-derived, so re-loading upserts the same workflow rather
        # than duplicating it. A bad file is reported, not fatal.
        wf_status: dict[str, list] = {"dir": wf_dir, "loaded": [], "failed": []}
        for spec in _load_workflows_dir(wf_dir, failed=wf_status["failed"]):
            try:
                manager.create(spec, origin="workflow")
                wf_status["loaded"].append(spec.id)
            except Exception as exc:  # noqa: BLE001
                log.warning("workflow %s failed: %s", spec.id, exc)
                wf_status["failed"].append({"id": spec.id, "error": str(exc)})
        app.state.workflows = wf_status

        # INGEST-PLAN: a single mounted file declaring many jobs. Loaded after the
        # workflows-dir scan; ids are stable so re-loading upserts. A bad plan (or
        # a bad job within it) is reported, not fatal. Only attached to app.state
        # when a plan is configured, so /health is unchanged without one.
        if plan_src:
            try:
                app.state.plan = _apply_ingest_plan(
                    manager, plan_src, only=only, exclude=exclude
                )
            except ValueError as exc:
                # A --only pattern matched no resolved job: fatal at startup.
                print(str(exc), file=sys.stderr)
                sys.exit(1)

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
        out = {"ok": True, "status": "ok", "service": "ujin-jobs",
               "jobs": len(m.jobs),
               "plugins": getattr(app.state, "plugins", {"loaded": [], "failed": []}),
               "workflows": getattr(app.state, "workflows",
                                    {"dir": wf_dir, "loaded": [], "failed": []})}
        # Only present when an INGEST-PLAN is configured -> no-plan deploys unchanged.
        plan = getattr(app.state, "plan", None)
        if plan is not None:
            out["plan"] = plan
        return out

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

    @app.post("/plan/reload")
    async def plan_reload(request: Request, prune: bool = False) -> dict[str, Any]:
        """Re-read the configured INGEST-PLAN and upsert its jobs, no restart.

        Goes through the same loader and ``--only``/``--exclude`` selection as
        startup, so resolution and error handling are identical: a per-job error
        fails only that job (reported in ``failed``) while the rest reload. Refreshes
        the ``/health`` plan status and returns the same ``{"path", "loaded",
        "failed"}`` summary. When no plan is configured it is a safe no-op: nothing
        changes and ``reloaded`` is ``false``. Unlike startup, a ``--only`` pattern
        that now matches nothing is reported rather than fatal — the server keeps
        running. The route is gated by the optional API-key auth like every other
        mutating endpoint.

        Pass ``?prune=true`` (or a ``{"prune": true}`` JSON body) to also **remove**
        plan-owned jobs whose ids vanished from the re-read plan; the removed ids are
        returned in a ``"pruned"`` list. Jobs created via the REST API or the
        positional ``jobs.yaml`` preload are never pruned, even when absent from the
        plan. Without the option the reload is upsert-only and removes nothing — the
        default, byte-for-byte the pre-prune behaviour.
        """
        # Accept the opt-in via either ?prune=true or a {"prune": true} JSON body.
        if not prune:
            try:
                body = await request.json()
            except Exception:  # noqa: BLE001 - no/invalid body -> no prune
                body = None
            if isinstance(body, dict):
                prune = bool(body.get("prune", False))
        if not plan_src:
            return {"reloaded": False, "detail": "no plan configured",
                    "loaded": [], "failed": []}
        try:
            plan_status = _apply_ingest_plan(
                app.state.manager, plan_src, only=only, exclude=exclude, prune=prune
            )
        except ValueError as exc:
            # --only matched no job on reload: report, don't kill the server.
            return {"reloaded": False, "detail": str(exc),
                    "loaded": [], "failed": []}
        app.state.plan = plan_status
        return {"reloaded": True, **plan_status}

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
    only: list | None = None,
    exclude: list | None = None,
) -> None:
    import uvicorn

    uvicorn.run(
        create_jobs_app(config_path, workflows_dir=workflows_dir, plan_path=plan_path,
                        only=only, exclude=exclude),
        host=host, port=port,
    )
