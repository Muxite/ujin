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
  GET    /plan                  read-only: jobs the active plan/workflows resolve to,
                                each with provenance (source file + matrix vars)
  POST   /plan/reload           re-read the configured INGEST-PLAN and upsert jobs
                                (?prune drops removed jobs; ?dry_run previews only)
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

    from .api_models import JobCreate, PlanIntrospection
except ModuleNotFoundError:  # pragma: no cover - jobs extra missing
    JobCreate = None  # type: ignore[assignment,misc]
    PlanIntrospection = None  # type: ignore[assignment,misc]
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


def _load_fragment(
    ref: str, root: Path, seen: "tuple[Path, ...]", _cache: "dict | None" = None
) -> Any:
    """Parse a fragment file and recursively resolve its own includes.

    *_cache* (keyed by resolved absolute Path) avoids re-reading the same
    fragment file when multiple jobs share the same ``include:`` path within
    one plan-load call.  The cached value is the already-resolved fragment
    (result of ``_resolve_includes``), so sub-includes within the fragment are
    inlined only once regardless of how many jobs reference it.  ``None``
    disables caching (the default for callers that don't create a cache).
    """
    import yaml

    fp = _resolve_fragment_path(ref, root)
    if fp in seen:
        chain = " -> ".join(p.name for p in (*seen, fp))
        raise WorkflowIncludeError(f"cyclic include detected: {chain}")
    if _cache is not None and fp in _cache:
        return _cache[fp]
    text = _expand_env(fp.read_text(encoding="utf-8"))
    result = _resolve_includes(yaml.safe_load(text), fp.parent, seen + (fp,), _cache)
    if _cache is not None:
        _cache[fp] = result
    return result


def _resolve_includes(
    node: Any, root: Path, seen: "tuple[Path, ...]" = (), _cache: "dict | None" = None
) -> Any:
    """Inline every ``include:``/``use:`` fragment found anywhere in *node*.

    A mapping with an ``include:`` is deep-merged *over* the referenced
    fragment(s) (the mapping's own keys win; multiple refs apply left-to-right). A
    list item whose include expands to a list is spliced into the list (so a
    transform-pipeline fragment drops straight into ``transforms:``). With no
    include keys present, *node* is returned structurally unchanged.

    *_cache* is threaded to :func:`_load_fragment` so repeated references to the
    same fragment path within one plan-load are read and parsed only once.
    """
    if isinstance(node, dict):
        refs, rest = _split_include(node)
        # The ``matrix:``/``for_each:`` value is opaque to fragment resolution: it
        # holds rows/axes plus the additive ``include:``/``exclude:`` filter lists
        # (handled later by ``_matrix_rows``), never ``include:``/``use:``
        # *fragments*. Recursing would misread a matrix ``include:`` list of
        # variable maps as a fragment path; the other matrix forms (inline list,
        # ``{from: ...}``, cross-product axes) carry no fragments either, so passing
        # the value through unchanged stays byte-for-byte equivalent for them.
        resolved = {
            k: (v if k in _MATRIX_KEYS else _resolve_includes(v, root, seen, _cache))
            for k, v in rest.items()
        }
        if not refs:
            return resolved
        merged: Any = None
        for ref in refs:
            frag = _load_fragment(ref, root, seen, _cache)
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
            resolved = _resolve_includes(item, root, seen, _cache)
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
# ``for_each:``) is fanned into one job per row, with each row's variables
# substituted into every ``{{ var }}`` placeholder across the template
# (source/transforms/sinks/schedule/name/id/...). The matrix value takes one of
# three shapes, all yielding the same downstream fan-out:
#
#   * an inline **list** of variable maps (one job per map);
#   * a **``{from: <path>}``** mapping naming an external data file holding that list;
#   * a **cross-product axes** mapping of axis-name -> value-list — any mapping with
#     no ``from:`` key — fanned into one job per cartesian-product combination.
#
# The ``from:`` key is the disambiguator between the two mapping shapes (so ``from``
# is reserved as an axis name). Kept in its own code path so it neither entangles
# with, nor conflicts much with, the includes work. A file carrying no matrix key
# passes through structurally unchanged.
#
# Two optional, strictly additive filters may ride along when the matrix value is a
# **mapping** (they are reserved keys, so ``exclude``/``include`` cannot be axis
# names):
#
#   * ``include:`` — a list of *full* variable maps appended to the base rows. With
#     no axes and no ``from:``, ``include:`` alone is the whole fan-out — the
#     inline-list form written as a mapping so it too can carry an ``exclude:``.
#   * ``exclude:`` — a list of *partial* variable maps; a row (base or include) is
#     dropped when it agrees with an exclude entry on every key that entry specifies
#     (an entry naming a key the row lacks never matches). Exclude runs over the whole
#     fan-out, so it prunes a cross-product, a ``{from: ...}`` list, or an inline list
#     alike, down to the wanted subset.
#
# Both default to absent: a matrix block with neither resolves byte-for-byte as
# before.

_MATRIX_KEYS = ("matrix", "for_each")

# ``{{ var }}`` placeholder (whitespace-tolerant). Distinct from the ``${VAR}``
# env syntax (already expanded at file-read time), so the two never collide.
_VAR_REF = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

# Runs of non-alphanumerics collapse to a single ``-`` when slugifying a
# cross-product axis value into the default job-id fragment (e.g. ``"4k monitor"``
# -> ``"4k-monitor"``).
_SLUG_STRIP = re.compile(r"[^A-Za-z0-9]+")


# ── env-var requirements report (``ujin plan env``) ────────────────────────── #
#
# A strictly-additive, **read-only** introspection: walk a plan/workflow file and
# every ``include:``/``use:`` fragment it pulls in, and report which ``${VAR}``
# references the file needs — each variable's declared default (``${VAR:-default}``),
# whether any occurrence is required (``${VAR:?message}``), and whether it is set in
# the environment. It reuses the very same ``_ENV_REF`` regex ``_expand_env`` scans
# with and the same fragment-path resolution ``include:`` uses, but it never expands
# or mutates anything, so ``${VAR}`` substitution stays byte-for-byte unchanged.


def _scan_env_refs(text: str, refs: "dict[str, dict]") -> None:
    """Record every ``${VAR}`` reference in *text* into *refs* (reusing ``_ENV_REF``).

    *refs* maps a variable name to ``{"default": str | None, "required": bool}``. The
    **first** ``${VAR:-default}`` seen sets the variable's default; **any**
    ``${VAR:?…}`` occurrence marks it required; a plain ``${VAR}`` just registers the
    name. Mutates *refs* in place, preserving first-encounter insertion order, so the
    same regex groups ``_expand_env`` reads drive the report without expanding.
    """
    for m in _ENV_REF.finditer(text):
        name, op, operand = m.group(1), m.group(2), m.group(3)
        info = refs.setdefault(name, {"default": None, "required": False})
        if op == ":-":
            if info["default"] is None:
                info["default"] = operand
        elif op == ":?":
            info["required"] = True


def _find_include_refs(node: Any) -> "list[str]":
    """Every ``include:``/``use:`` fragment path referenced anywhere in *node*.

    Mirrors :func:`_resolve_includes`'s traversal — recurses through mappings and
    lists but **skips** ``matrix:``/``for_each:`` values (whose own ``include:``
    filter lists are variable maps, not fragment paths) — collecting only the string
    fragment refs. Used to discover the fragment files a plan pulls in so their
    ``${VAR}`` references are reported too; it inlines nothing.
    """
    out: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k in _INCLUDE_KEYS:
                if isinstance(v, str):
                    out.append(v)
                elif isinstance(v, (list, tuple)):
                    out.extend(r for r in v if isinstance(r, str))
            elif k in _MATRIX_KEYS:
                continue
            else:
                out.extend(_find_include_refs(v))
    elif isinstance(node, list):
        for item in node:
            out.extend(_find_include_refs(item))
    return out


def _walk_plan_env_refs(
    path: Path, refs: "dict[str, dict]", seen: "tuple[Path, ...]"
) -> None:
    """Scan one file's raw text for ``${VAR}`` refs, then recurse into its fragments.

    Reads *path*'s raw (unexpanded) text — so the ``${VAR}`` references survive to be
    scanned by :func:`_scan_env_refs` — then parses it to discover ``include:``/
    ``use:`` fragments, resolving each path exactly as the loader does
    (:func:`_resolve_fragment_path`: relative to this file's dir, then
    ``$UJIN_WORKFLOWS_DIR``) and recursing into it. *seen* (resolved paths) guards
    against include cycles. Lenient by design: an unparseable file, or a fragment that
    can't be resolved, is skipped rather than raised — the report still lists every
    reference found so far.
    """
    import yaml

    rp = path.resolve()
    if rp in seen:
        return
    seen = seen + (rp,)
    text = path.read_text(encoding="utf-8")
    _scan_env_refs(text, refs)
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return
    for ref in _find_include_refs(data):
        try:
            frag = _resolve_fragment_path(ref, path.parent)
        except WorkflowIncludeError:
            continue
        _walk_plan_env_refs(frag, refs, seen)


def collect_plan_env_refs(path) -> "dict[str, dict]":
    """Collect the ``${VAR}`` references a plan/workflow file (and its fragments) need.

    *path* is a plan/workflow file or a directory of workflow files (scanned
    non-recursively as ``*.y*ml``, like :func:`_load_workflows_dir`). Every file's
    raw text and the raw text of each ``include:``/``use:`` fragment it pulls in are
    scanned with the same ``_ENV_REF`` regex ``_expand_env`` uses, so a reference is
    reported with the default declared by its ``${VAR:-default}`` form and whether any
    occurrence is required (``${VAR:?message}``). Set-vs-unset is left to the caller
    (it reads ``os.environ`` after any ``--env-file`` overlay).

    Returns an insertion-ordered ``dict`` of variable name ->
    ``{"default": str | None, "required": bool}``, in first-encounter order across the
    file(s) then their fragments. A file with no ``${VAR}`` references yields ``{}``.
    Strictly read-only: nothing is expanded, resolved into jobs, or mutated.
    """
    refs: dict[str, dict] = {}
    root = Path(path)
    files = sorted(root.glob("*.y*ml")) if root.is_dir() else [root]
    for fp in files:
        _walk_plan_env_refs(fp, refs, ())
    return refs


# ── file-level ``vars:`` defaults ──────────────────────────────────────────── #
#
# An optional top-level ``vars:`` mapping (variable-name -> default value) supplies
# fallbacks for the ``{{ var }}`` substitution the file already uses: a placeholder
# that no matrix entry and no process-environment value sets resolves from these
# defaults instead of being left verbatim. Precedence for a ``{{ NAME }}``:
#   1. the matrix/for_each per-entry value (highest);
#   2. the process-environment value ``os.environ[NAME]``, if set;
#   3. the file-level ``vars: NAME`` default (lowest);
#   4. otherwise the placeholder is left verbatim (unchanged permissive behaviour).
# The env overlay (2 over 3) is baked in by :func:`_overlay_env_vars`; the matrix
# overlay (1 over the rest) is applied around expansion in :func:`_expand_matrix`.
# The ``${VAR}`` / ``${VAR:-default}`` read-time env syntax is unrelated and
# unchanged. A file with no ``vars:`` key yields an empty mapping here, so the whole
# feature is a strict no-op and resolution stays byte-for-byte identical.


def _overlay_env_vars(raw: Any) -> "dict[str, Any]":
    """Overlay process-env onto a validated ``vars:`` mapping (env value wins).

    *raw* is the (already type-checked) value of a top-level ``vars:`` key, or
    ``None``/empty when none was declared. Returns a name -> value dict in which a
    *set* process-environment value for a name replaces the file-level default; a
    matrix per-entry var (merged later) overrides both. With *raw* ``None``/empty the
    result is ``{}``, so callers add no substitution and the load is unchanged.
    """
    if not raw:
        return {}
    return {k: os.environ.get(k, v) for k, v in raw.items()}


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


def _slug(value: Any) -> str:
    """A compact, id-safe token derived from a scalar axis value.

    Runs of non-alphanumerics collapse to a single ``-`` and the ends are trimmed,
    so ``"4k monitor"`` -> ``"4k-monitor"``; an all-symbol value yields ``"x"``.
    Used to build the default id of a cross-product combination — two values that
    slug to the same token are caught by ``_expand_matrix``'s duplicate-id check.
    """
    return _SLUG_STRIP.sub("-", str(value)).strip("-") or "x"


def _cross_product_rows(axes: dict, key: str) -> list:
    """Fan a mapping of *axis-name -> value-list* into cartesian-product rows.

    ``{size: [s, m], region: [us, eu]}`` yields the four ``{size, region}``
    combinations — one variable map per combination. Returns ``(vars, suffix)``
    pairs where *vars* is the combination's per-axis map and *suffix* is a stable
    id fragment built from the combination's values.

    Both the row order and each *suffix* are derived with the **axis names sorted**,
    so they are independent of the order the axes were written: reordering the axes
    in the file leaves every combination with the same default id. Raises
    ``ValueError`` (routing the file into the loader's ``failed`` list) on an empty
    axes map, or an axis whose value is not a non-empty list.
    """
    import itertools

    if not axes:
        raise ValueError(
            f"`{key}:` cross-product needs at least one axis "
            "(a mapping of axis-name -> list of values)"
        )
    names = sorted(axes)
    value_lists = []
    for name in names:
        vals = axes[name]
        if not isinstance(vals, list) or not vals:
            raise ValueError(
                f"`{key}:` axis {name!r} must be a non-empty list of values, got "
                f"{type(vals).__name__}"
            )
        value_lists.append(vals)
    return [
        (dict(zip(names, combo)), "-".join(_slug(v) for v in combo))
        for combo in itertools.product(*value_lists)
    ]


# Reserved keys of a matrix *mapping* that are not cross-product axes: the
# ``from:`` data-file disambiguator plus the additive ``exclude:``/``include:``
# filters. Pulled out before the remaining keys are read as axes.
_MATRIX_FILTER_KEYS = ("exclude", "include")


def _index_rows(rows: Any, key: str) -> list:
    """Validate a plain list of variable maps into ``(vars, str(index))`` rows.

    Shared by the inline-list and ``{from: <path>}`` forms — both supply an explicit
    list whose rows are suffixed by their 0-based position. Raises ``ValueError`` (so
    the file lands in the loader's ``failed`` list) on a non-list / empty list, or a
    row that is not a mapping.
    """
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"`{key}:` must be a non-empty list of variable maps")
    out: list = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(
                f"`{key}:` entry {i} must be a variable map, got "
                f"{type(row).__name__}"
            )
        out.append((row, str(i)))
    return out


def _matrix_filters(
    exclude: Any, include: Any, key: str
) -> "tuple[list, list]":
    """Validate the optional ``exclude:``/``include:`` lists into ``(exclude, include)``.

    Each, when present, must be a **list of variable maps**; ``None`` (absent) becomes
    ``[]`` so callers treat it as a no-op. A malformed value (not a list, or a
    non-mapping entry) raises ``ValueError`` with an actionable ``ujin: …``-style
    message, routing the containing file/job through the normal ``failed`` path
    instead of crashing the loader.
    """
    def _check(val: Any, name: str) -> list:
        if val is None:
            return []
        if not isinstance(val, list):
            raise ValueError(
                f"`{key}:` `{name}:` must be a list of variable maps, got "
                f"{type(val).__name__}"
            )
        for i, entry in enumerate(val):
            if not isinstance(entry, dict):
                raise ValueError(
                    f"`{key}:` `{name}:` entry {i} must be a variable map, got "
                    f"{type(entry).__name__}"
                )
        return val

    return _check(exclude, "exclude"), _check(include, "include")


def _row_excluded(row_vars: dict, exclude: list) -> bool:
    """True if *row_vars* agrees with any *exclude* entry on every key it specifies.

    A partial exclude entry matches when each key it names is present in *row_vars*
    with an equal value; an entry naming a key the row lacks never matches (so an
    exclude that references an absent key is a no-op, leaving the row in place).
    """
    return any(
        all(k in row_vars and row_vars[k] == v for k, v in entry.items())
        for entry in exclude
    )


def _matrix_rows(value: Any, key: str, root: "Path | None") -> list:
    """Resolve a ``matrix:``/``for_each:`` value into ``(vars, default_suffix)`` rows.

    Three base input shapes, all producing the same downstream fan-out (one job per
    row):

    * an **inline list** of variable maps — suffixed by each row's 0-based index;
    * a **``{from: <path>}``** mapping naming an external data file holding that list
      (see :func:`_load_matrix_file`) — identical to inline once loaded;
    * a **cross-product axes** mapping of axis-name -> value-list (any mapping with no
      ``from:`` key) — one row per cartesian-product combination, suffixed by the
      combination's slugified values (see :func:`_cross_product_rows`).

    When the value is a **mapping** it may also carry two optional, strictly additive
    filters (reserved keys, never axis names): ``include:`` — a list of full variable
    maps appended (index-suffixed) to the base rows — and ``exclude:`` — a list of
    partial variable maps; a row (base *or* include) is dropped when it agrees with
    any exclude entry on every key that entry specifies (see :func:`_row_excluded`).
    Exclude runs over the whole fan-out, so it prunes a cross-product, a
    ``{from: ...}`` list, or an inline list alike. With no axes and no ``from:``,
    ``include:`` alone is the whole fan-out — the inline-list form written as a
    mapping so it too can pair with an ``exclude:``.

    The ``from:`` key disambiguates the two base mapping shapes, so it is reserved (an
    axes mapping cannot name an axis ``from``). Raises ``ValueError`` on a malformed
    value — including one whose filters prune the fan-out to zero jobs — so the
    containing file lands in the loader's ``failed`` list.
    """
    if not isinstance(value, (list, dict)):
        raise ValueError(
            f"`{key}:` must be a list of variable maps or a mapping of "
            f"axis-name -> value-list, got {type(value).__name__}"
        )
    exclude: list = []
    include: list = []
    if isinstance(value, dict):
        exclude, include = _matrix_filters(
            value.get("exclude"), value.get("include"), key
        )
        had_include = "include" in value
        base_def = {k: v for k, v in value.items() if k not in _MATRIX_FILTER_KEYS}
        if "from" in base_def:
            base = _index_rows(_load_matrix_file(base_def, key, root), key)
        elif base_def or not had_include:
            # Cross-product axes — also the empty-mapping error path, which routes
            # through :func:`_cross_product_rows` for its "at least one axis" message.
            base = _cross_product_rows(base_def, key)
        else:
            base = []  # include-only: the inline list written as a mapping
    else:
        base = _index_rows(value, key)

    rows = base + [(extra, str(j)) for j, extra in enumerate(include)]
    rows = [(v, s) for (v, s) in rows if not _row_excluded(v, exclude)]
    if not rows:
        raise ValueError(
            f"`{key}:` expanded to zero jobs after applying `exclude:`/`include:`; "
            "loosen `exclude:` or supply rows"
        )
    return rows


def _expand_matrix(
    entry: dict,
    base_id: str,
    root: "Path | None" = None,
    prov: "list | None" = None,
    var_defaults: "dict | None" = None,
) -> list:
    """Fan one job mapping out over its ``matrix:``/``for_each:`` rows.

    The matrix value is an **inline list** of variable maps, a ``{from: <path>}``
    mapping naming an external data file that supplies that list (see
    :func:`_load_matrix_file`), or a **cross-product axes** mapping of
    axis-name -> value-list fanned into one row per combination (see
    :func:`_matrix_rows`/:func:`_cross_product_rows`). Returns one dict per row, each
    a copy of *entry* (minus the matrix key) with the row's variables substituted in.
    A stable, distinct id/name is derived per generated job: an explicit ``id``
    template (e.g. ``id: feed-{{ slug }}``) is honored after substitution, otherwise
    ``<base_id>-<suffix>`` — the suffix being the row index (inline/file forms) or the
    combination's slugified values (cross-product form), so the id is deterministic
    and, for cross-product, independent of axis order. *root* is the containing file's
    directory, used to resolve a ``from:`` data file. A matrix mapping may also carry
    optional ``exclude:``/``include:`` filter lists that prune / append rows before id
    derivation (see :func:`_matrix_rows`). Raises ``ValueError`` (so the file lands in
    the ``failed`` list, not aborting startup) on a malformed matrix or colliding ids.

    When *prov* is a list it collects, in lock-step with the returned jobs, one
    ``{"key", "index", "vars"}`` record per generated job naming the matrix key, the
    0-based row index, and the variable map that produced it (for cross-product, the
    per-axis combination map) — the provenance surfaced by ``plan render --explain``.
    Default ``None`` collects nothing, so the expansion is byte-for-byte unchanged.

    *var_defaults* are the file-level ``vars:`` fallbacks (already env-overlaid); each
    row is substituted as ``{**var_defaults, **row}`` so a per-entry var overrides a
    default while an otherwise-unset ``{{ var }}`` resolves from the default. Empty
    (the default) means ``{**{}, **row} == row``, so the substitution is unchanged.
    Provenance still records only the per-entry ``row`` (not the defaults).
    """
    var_defaults = var_defaults or {}
    present = [k for k in _MATRIX_KEYS if k in entry]
    if len(present) > 1:
        raise ValueError(f"use only one of {present}, not both")
    key = present[0]
    rows = _matrix_rows(entry[key], key, root)
    template = {k: v for k, v in entry.items() if k not in _MATRIX_KEYS}

    out: list = []
    seen: set = set()
    for i, (row, suffix) in enumerate(rows):
        d = _subst_vars(template, {**var_defaults, **row} if var_defaults else row)
        d.setdefault("id", f"{base_id}-{suffix}")
        d.setdefault("name", d["id"])
        if d["id"] in seen:
            raise ValueError(
                f"`{key}:` produced duplicate id {d['id']!r}; give the `id:` "
                "template a variable that differs per entry"
            )
        seen.add(d["id"])
        out.append(d)
        if prov is not None:
            prov.append({"key": key, "index": i, "vars": dict(row)})
    return out


def _expand_matrix_entries(
    entries: list,
    stem: str,
    root: "Path | None" = None,
    prov: "list | None" = None,
    var_defaults: "dict | None" = None,
) -> list:
    """Replace each matrix-bearing entry with its fan-out; pass others through.

    A list with no matrix anywhere *and* no ``vars:`` defaults is returned unchanged,
    so matrix-free files with no ``vars:`` key parse to exactly the same entries (and
    thus the same JobSpecs) as before. *root* (the containing file's directory) is
    threaded to :func:`_expand_matrix` so a ``matrix: {from: <path>}`` data file
    resolves relative to it.

    *var_defaults* are the file-level ``vars:`` fallbacks (already env-overlaid). When
    non-empty they are threaded into :func:`_expand_matrix` for matrix entries and,
    for a non-matrix entry, applied as a :func:`_subst_vars` pass so a ``{{ var }}``
    placeholder left unset by env/matrix resolves from the default. Empty (the
    default) skips that pass entirely, leaving every entry byte-for-byte unchanged.

    When *prov* is a list it is filled, in lock-step with the returned entries, with
    one element per output entry: the matrix provenance record (see
    :func:`_expand_matrix`) for a fanned-out job, or ``None`` for an entry that
    carried no matrix. Default ``None`` collects nothing — byte-for-byte unchanged.
    """
    var_defaults = var_defaults or {}
    if not any(_has_matrix(e) for e in entries) and not var_defaults:
        if prov is not None:
            prov.extend([None] * len(entries))
        return entries
    out: list = []
    for i, entry in enumerate(entries):
        if not _has_matrix(entry):
            out.append(_subst_vars(entry, var_defaults) if var_defaults else entry)
            if prov is not None:
                prov.append(None)
            continue
        base = entry.get("id") or (stem if len(entries) == 1 else f"{stem}-{i}")
        out.extend(
            _expand_matrix(entry, base, root, prov=prov, var_defaults=var_defaults)
        )
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
# `profiles:` is a top-level-only key (a map of named overlays for `defaults:`).
_PLAN_TOP_KEYS = frozenset({"jobs", "defaults", "include", "use", "vars", "profiles"})

# Top-level keys of a WORKFLOW file: it may be a container (`jobs:`/`defaults:`) OR a
# single job written directly at the top level, so both key sets are accepted there.
# `vars:`/`profiles:` are top-level-only keys (a default-value map / overlay map),
# never per-job keys.
_WORKFLOW_TOP_KEYS = _JOB_KEYS | {"jobs", "defaults", "vars", "profiles"}


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


# ── profile overlays (`profiles:` + --profile / $UJIN_PLAN_PROFILE) ─────────── #
#
# A top-level `profiles:` mapping declares named overlays — each one deep-merged
# OVER the plan's base `defaults:` block when its name is selected (so a profile
# key wins over the same key in `defaults:`). The resulting effective defaults are
# then deep-merged UNDER each job exactly as before, so per-job keys still win and
# the established `${VAR}` / `{{ var }}` / matrix substitution order is unchanged.
# Selection is `--profile NAME` (flag) with a `$UJIN_PLAN_PROFILE` env fallback (the
# flag wins); neither set means no profile, no `profiles:` lookup, and resolution is
# byte-for-byte unchanged — strictly additive.

PLAN_PROFILE_ENV = "UJIN_PLAN_PROFILE"


class PlanProfileError(ValueError):
    """A selected ``--profile`` / ``$UJIN_PLAN_PROFILE`` name can't be applied.

    Raised when a profile is selected but the file declares no ``profiles:`` block
    (in strict/plan mode), the name is absent from it, or the overlay is malformed.
    A ``ValueError`` subclass so the loader's existing file-level handling records it
    in ``failed`` with a clean ``ujin: …`` message; callers that must exit non-zero
    (``plan validate``/``plan render``, ``jobs-serve`` startup) surface it as such.
    """


def _profile_overlay(
    data: Any, profile: "str | None", name: str, *, strict: bool = True
) -> dict:
    """Return the deep-merge overlay for the selected *profile* (``{}`` if none).

    *data* is the parsed top-level plan/workflow node (after include resolution).
    With *profile* falsy this is a no-op returning ``{}`` so resolution is unchanged.
    When a profile is selected it is looked up in *data*'s ``profiles:`` mapping and
    the matching overlay returned for the caller to deep-merge over ``defaults:``.

    Raises :class:`PlanProfileError` (clean, naming the available profiles) when the
    profile cannot be applied. In *strict* mode (a single plan file) a missing
    ``profiles:`` block is an error; in lenient mode (a workflow file in a directory
    scan, where files legitimately may not declare profiles) a file with no
    ``profiles:`` key is left unchanged so only a present-but-unknown name fails.
    """
    if not profile:
        return {}
    profiles = data.get("profiles") if isinstance(data, dict) else None
    if profiles is None:
        if strict:
            raise PlanProfileError(
                f"plan {name}: profile {profile!r} selected but the file declares "
                "no `profiles:` block"
            )
        return {}
    if not isinstance(profiles, dict):
        raise PlanProfileError(
            f"plan {name}: `profiles:` must be a mapping of name -> overlay, got "
            f"{type(profiles).__name__}"
        )
    if profile not in profiles:
        available = ", ".join(sorted(profiles)) if profiles else "(none defined)"
        raise PlanProfileError(
            f"plan {name}: profile {profile!r} not found; available profiles: "
            f"{available}"
        )
    overlay = profiles[profile]
    if overlay is None:
        return {}
    if not isinstance(overlay, dict):
        raise PlanProfileError(
            f"plan {name}: profile {profile!r} must be a mapping, got "
            f"{type(overlay).__name__}"
        )
    return overlay


def _check_plan_profile(plan_src, profile: "str | None") -> None:
    """Validate the selected *profile* against *plan_src* without loading jobs.

    Raises :class:`PlanProfileError` (clean message) when *profile* is selected but
    cannot be applied to the plan. A falsy *profile* or *plan_src*, or a plan that
    can't be read/parsed at all, is a no-op here — the normal loader reports any
    file-level problem at load time. Used by ``jobs-serve`` as a pre-flight so a bad
    ``--profile`` exits non-zero before the server binds a port (mirroring ``--only``).
    """
    if not profile or not plan_src:
        return
    import yaml

    p = Path(plan_src)
    try:
        data = _resolve_includes(
            yaml.safe_load(_expand_env(p.read_text(encoding="utf-8"))), p.parent
        )
    except Exception:  # noqa: BLE001 - a file-level problem is reported at load time
        return
    _profile_overlay(data, profile, p.name)


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


def _specs_from_workflow_file(
    path,
    warnings: list | None = None,
    provenance: list | None = None,
    profile: "str | None" = None,
) -> list:
    """Parse one workflow file into JobSpecs, deriving stable ids from the stem.

    A workflow file is the same declarative shape as a job. The filename stem is
    the **workflow id** (and default name) so the same file maps to the same
    workflow across restarts/redeploys — unless the file sets an explicit ``id``.
    A file may also hold a list / ``{jobs: [...]}``; entries without an ``id``
    fall back to ``<stem>-<index>`` to stay deterministic.

    Four additive conveniences run before id derivation: ``include:``/``use:``
    fragments are inlined (relative to the file's dir, then ``$UJIN_WORKFLOWS_DIR``),
    a top-level ``defaults:`` mapping is deep-merged *under* every job (per-job keys
    win), a ``matrix:``/``for_each:`` key fans a job out into one per variable
    map — given inline, or by a ``{from: <path>}`` data file resolved like an
    ``include:`` fragment — with each map substituted into the merged result (see
    ``_expand_matrix``), and a top-level ``vars:`` mapping supplies default values
    for any ``{{ var }}`` placeholder the process environment and matrix leave unset
    (matrix per-entry var > env value > ``vars:`` default; see ``_overlay_env_vars``).
    The layers compose in that order; a file using none resolves byte-for-byte
    as it did before.

    When *provenance* is a list it is filled, in lock-step with the returned specs,
    with one ``{"id", "source", ["matrix"]}`` record per job — its originating file
    and, for a matrix-expanded job, the matrix entry (key/index/vars) that produced
    it. This is what ``plan render --explain`` consumes. Default ``None`` collects
    nothing, leaving the resolution byte-for-byte unchanged.
    """
    import yaml

    from .model import JobSpec

    path = Path(path)
    stem = path.stem
    data = yaml.safe_load(_expand_env(path.read_text(encoding="utf-8"))) or {}

    # Inline reusable fragments, then peel off a top-level `defaults:` block. Both
    # are no-ops when absent, so the paths below stay identical to the old loader.
    data = _resolve_includes(data, path.parent, _cache={})

    # A bare scalar (integer, plain string, etc.) is not a valid workflow shape —
    # neither a single-job mapping nor a list of jobs. Catch it here so the caller
    # gets a clean ``ujin: …`` message instead of a cryptic ``'int' object is not
    # iterable`` or ``dictionary update sequence …`` error from the iteration below.
    if not isinstance(data, (dict, list)):
        raise ValueError(
            f"top-level must be a job mapping or a list of jobs, "
            f"got {type(data).__name__}"
        )

    # Advisory schema check on the resolved top level. A workflow file's top level
    # may be a container (`jobs:`/`defaults:`) OR a single job written directly, so
    # both key sets are valid here; an unrecognized key (a typo) is reported, never
    # silently dropped. Single-job files validate fully here; `jobs:` lists also get
    # a per-entry check below.
    _report_warnings(
        _key_problems(data, _WORKFLOW_TOP_KEYS, f"workflow {path.name}", top=True),
        warnings,
    )

    # Peel off the optional top-level `vars:` defaults (validated as a mapping) and
    # env-overlay them. They backfill `{{ var }}` placeholders left unset by the
    # process environment and by matrix entries; with no `vars:` key this is empty and
    # the whole file resolves byte-for-byte as before. Prefix-free message — the
    # `_load_workflows_dir` wrapper adds the `ujin: workflow <file>:` context.
    raw_vars = data.pop("vars", None) if isinstance(data, dict) else None
    if raw_vars is not None and not isinstance(raw_vars, dict):
        raise ValueError(
            "`vars:` must be a mapping of name -> default value, got "
            f"{type(raw_vars).__name__}"
        )
    var_defaults = _overlay_env_vars(raw_vars)

    # Profile overlay (lenient for a workflow file: a file with no `profiles:` key is
    # left unchanged, so a directory scan with a `--profile` set only fails files that
    # declare profiles but not the selected name). Pop `profiles:` so a single-job
    # mapping below doesn't carry it as a stray key. Merged OVER base defaults so the
    # overlay wins over `defaults:` but a per-job key still wins over the overlay.
    raw_profiles = data.pop("profiles", None) if isinstance(data, dict) else None
    overlay = _profile_overlay(
        {"profiles": raw_profiles}, profile, path.name, strict=False
    )

    defaults = data.pop("defaults", None) or {} if isinstance(data, dict) else {}
    if overlay:
        defaults = _deep_merge(defaults, overlay)

    # Single-job mapping (no top-level `jobs:` list) -> the whole file is one job.
    # A `matrix:`/`for_each:` key turns that mapping into a *template* fanned into
    # several jobs; without one it stays a single job exactly as before.
    if isinstance(data, dict) and "jobs" not in data:
        merged = _deep_merge(defaults, data) if defaults else dict(data)
        if not _has_matrix(merged):
            if var_defaults:
                merged = _subst_vars(merged, var_defaults)
            merged.setdefault("id", stem)
            merged.setdefault("name", stem)
            if provenance is not None:
                provenance.append({"id": merged["id"], "source": str(path)})
            return [JobSpec.from_dict(merged)]
        raw = [merged]
    else:
        raw = data.get("jobs", data) if isinstance(data, dict) else data
        # Validate before merging so a non-mapping entry raises a clear error
        # (naming the file and position) rather than a cryptic TypeError.
        for _j, _e in enumerate(raw or []):
            if not isinstance(_e, dict):
                raise ValueError(
                    f"workflow {path.name}: jobs entry {_j} must be a mapping, "
                    f"got {type(_e).__name__}"
                )
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
    prov_rows = [] if provenance is not None else None
    raw = _expand_matrix_entries(
        raw, stem, path.parent, prov=prov_rows, var_defaults=var_defaults
    )
    specs = []
    for i, entry in enumerate(raw):
        d = dict(entry)
        d.setdefault("id", stem if len(raw) == 1 else f"{stem}-{i}")
        d.setdefault("name", d["id"])
        specs.append(JobSpec.from_dict(d))
        if provenance is not None:
            rec = {"id": d["id"], "source": str(path)}
            matrix = prov_rows[i] if prov_rows is not None else None
            if matrix is not None:
                rec["matrix"] = matrix
            provenance.append(rec)
    return specs


def _load_workflows_dir(
    path: str,
    failed: list | None = None,
    warnings: list | None = None,
    provenance: list | None = None,
    profile: "str | None" = None,
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

    When *provenance* is a list it collects, in lock-step with the returned specs, a
    per-job ``{"id", "source", ["matrix"]}`` record (see
    :func:`_specs_from_workflow_file`) for ``plan render --explain``. Default ``None``
    collects nothing.
    """
    root = Path(path)
    if not root.is_dir():
        return []
    specs = []
    # Non-recursive on purpose (no ``**``): subdirectory fragments stay out of
    # the workflow set — see the docstring. The dir-scan tests guard this.
    for fp in sorted(root.glob("*.y*ml")):
        try:
            specs.extend(
                _specs_from_workflow_file(
                    fp, warnings=warnings, provenance=provenance, profile=profile
                )
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("skipping workflow file %s: %s", fp, exc)
            if failed is not None:
                failed.append({"id": fp.stem, "error": f"ujin: workflow {fp.name}: {exc}"})
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
    ``jobs:`` list plus an optional ``defaults:`` and/or ``vars:`` mapping. An empty
    file is treated as zero jobs. Anything else (a bare scalar, a mapping without
    ``jobs:``, a non-list ``jobs:``, a non-mapping ``defaults:``/``vars:``) raises
    ``ValueError`` naming *name* so the caller can report it instead of crashing
    startup. The ``vars:`` block is only type-checked here; its env-overlaid defaults
    are extracted by :func:`_load_ingest_plan`.
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
        plan_vars = data.get("vars")
        if plan_vars is not None and not isinstance(plan_vars, dict):
            raise ValueError(
                f"plan {name}: `vars:` must be a mapping of name -> default value, "
                f"got {type(plan_vars).__name__}"
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
    provenance: list | None = None,
    profile: "str | None" = None,
) -> list:
    """Load an INGEST-PLAN file into JobSpecs, deriving stable per-job ids.

    Each resulting job gets a stable id (explicit ``id`` wins; otherwise
    ``<plan-stem>-<index>``, with a per-entry ``matrix:`` suffix) so re-loading the
    plan upserts the same jobs rather than duplicating them. The additive
    layers run in the same order as a workflow file: ``include:``/``use:`` fragments
    are inlined first (relative to the plan's directory then ``$UJIN_WORKFLOWS_DIR``),
    a top-level ``defaults:`` mapping is deep-merged *under* every job (per-job keys
    win), then ``matrix:``/``for_each:`` fans an entry into one job per variable map
    (the variable maps given inline, or by a ``{from: <path>}`` data file resolved
    like an ``include:`` fragment). A top-level ``vars:`` mapping supplies default
    values for any ``{{ var }}`` placeholder the process environment and matrix leave
    unset (matrix per-entry var > env value > ``vars:`` default).

    When *profile* names an entry of a top-level ``profiles:`` mapping, that overlay
    is deep-merged *over* the base ``defaults:`` before the per-job merge, so each
    job inherits the overlaid values unless it sets the key itself and the matrix/
    env/var substitution order is unchanged. A selected profile absent from
    ``profiles:`` fails the whole plan (one ``failed`` record) with a clean
    ``ujin: …`` message. ``profile=None`` (the default) skips this step entirely so
    resolution is byte-for-byte unchanged.

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

    When *provenance* is a list it collects, in lock-step with the returned specs, a
    per-job ``{"id", "source", ["matrix"]}`` record naming the plan file and, for a
    matrix-expanded job, the matrix entry (key/index/vars) that produced it — the
    provenance surfaced by ``plan render --explain``. Default ``None`` collects
    nothing, leaving resolution byte-for-byte unchanged.
    """
    import yaml

    from .model import JobSpec

    path = Path(path)
    stem = path.stem
    specs: list = []

    # ── file-level parse: one failure record covers the whole plan ──
    try:
        data = yaml.safe_load(_expand_env(path.read_text(encoding="utf-8")))
        data = _resolve_includes(data, path.parent, _cache={})
        defaults, entries = _plan_entries(data, path.name)
        # Profile overlay: deep-merge the selected `profiles:` entry OVER the base
        # `defaults:` before the per-job merge below, so each job inherits the
        # overlaid values unless it sets the key itself. A selected-but-absent
        # profile raises PlanProfileError (a ValueError), failing the whole plan
        # here with a clean message; profile=None is a no-op (unchanged).
        overlay = _profile_overlay(data, profile, path.name)
        if overlay:
            defaults = _deep_merge(defaults, overlay)
    except (OSError, yaml.YAMLError, WorkflowIncludeError, ValueError) as exc:
        log.warning("skipping ingest plan %s: %s", path, exc)
        if failed is not None:
            failed.append({"id": stem, "error": f"ujin: {path.name}: {exc}"})
        return specs

    # Advisory top-level schema check: an unrecognized top-level key — e.g.
    # `defaluts:` for `defaults:` — is reported, not silently dropped. Only a
    # mapping plan has top-level keys; a top-level list plan has none.
    _report_warnings(
        _key_problems(data, _PLAN_TOP_KEYS, f"plan {path.name}", top=True),
        warnings,
    )

    # File-level `vars:` defaults (validated as a mapping by `_plan_entries` above,
    # so a non-mapping already failed the whole plan). Env-overlaid here and threaded
    # into matrix expansion so a `{{ var }}` left unset by env/matrix resolves from
    # the default. Empty when no `vars:` key -> resolution is byte-for-byte unchanged.
    var_defaults = _overlay_env_vars(
        data.get("vars") if isinstance(data, dict) else None
    )

    # ── per-job resolution: isolate matrix/build errors so one bad job is
    # reported while the rest still load. Ids stay stable across reloads. ──
    seen: set = set()
    for i, entry in enumerate(entries):
        base = (entry.get("id") if isinstance(entry, dict) else None) or (
            stem if len(entries) == 1 else f"{stem}-{i}"
        )
        try:
            if not isinstance(entry, dict):
                raise ValueError(
                    f"plan {path.name}: jobs entry {i} must be a mapping, "
                    f"got {type(entry).__name__}"
                )
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
            prov_rows = [] if provenance is not None else None
            fanned = _expand_matrix_entries(
                [merged], base, path.parent, prov=prov_rows,
                var_defaults=var_defaults,
            )
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
                if provenance is not None:
                    rec = {"id": d["id"], "source": str(path)}
                    matrix = prov_rows[j] if prov_rows is not None else None
                    if matrix is not None:
                        rec["matrix"] = matrix
                    provenance.append(rec)
        except Exception as exc:  # noqa: BLE001
            log.warning("skipping plan job %s in %s: %s", base, path, exc)
            if failed is not None:
                failed.append({"id": base, "error": f"ujin: plan job {base} in {path.name}: {exc}"})
            # The entry still names a job the plan declares; record its id so prune
            # keeps it (a transient build error must not drop a still-present job).
            if present is not None:
                present.append(base)
    return specs


def _apply_ingest_plan(
    manager, plan_src, only=None, exclude=None, *, prune=False, profile=None
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
        plan_src, failed=plan_status["failed"], present=present, profile=profile
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


def _spec_differs(current: "JobSpec", resolved: "JobSpec") -> bool:
    """True if *resolved* would change the running job *current* on a reload.

    Compares the declared job (source/transforms/sinks/schedule/name/enabled),
    ignoring ``created_at`` — that is stamped at parse time, not declared by the
    plan, so an otherwise-identical re-read reports as *unchanged* rather than
    *updated*.
    """
    a = current.to_dict(); a.pop("created_at", None)
    b = resolved.to_dict(); b.pop("created_at", None)
    return a != b


def _dryrun_ingest_plan(
    manager, plan_src, only=None, exclude=None, *, prune=False, profile=None
) -> "dict[str, list]":
    """Preview a reload of *plan_src* WITHOUT mutating *manager*.

    Resolves the plan through the **identical** :func:`_load_ingest_plan`
    resolution (defaults/include/matrix/``${VAR}``) and ``--only``/``--exclude``
    selection as :func:`_apply_ingest_plan`, then classifies every resolved job id
    against the running jobs instead of upserting it:

      * ``added``     — id not currently running (a real reload would create it);
      * ``updated``   — id running but the resolved spec differs (would re-register);
      * ``unchanged`` — id running with an identical resolved spec (a no-op upsert).

    With *prune* set it also lists, under ``removed``, the plan-owned ids the
    re-read plan no longer declares — exactly the ids a real prune would delete,
    with the same file-level-read-failure guard, but **nothing is removed**.

    Returns the ``{"path", "loaded", "failed"}`` summary shape (``loaded`` stays
    empty — nothing is loaded in a dry run) extended with the classification
    lists, so the response mirrors the real reload summary. Raises ``ValueError``
    on an ``--only`` pattern that matches nothing, like the real apply, so the
    caller reports it without killing the server.
    """
    plan_status: dict[str, list] = {
        "path": str(plan_src), "loaded": [], "failed": [],
    }
    present: list[str] = []
    plan_specs = _load_ingest_plan(
        plan_src, failed=plan_status["failed"], present=present, profile=profile
    )
    if only is not None or exclude is not None:
        plan_specs = _select_jobs(plan_specs, only, exclude)
    added: list[str] = []
    updated: list[str] = []
    unchanged: list[str] = []
    for spec in plan_specs:
        handle = manager.get(spec.id)
        if handle is None:
            added.append(spec.id)
        elif _spec_differs(handle.spec, spec):
            updated.append(spec.id)
        else:
            unchanged.append(spec.id)
    plan_status["added"] = added
    plan_status["updated"] = updated
    plan_status["unchanged"] = unchanged
    if prune:
        # Mirror _apply_ingest_plan's prune gate exactly, but only *report* the
        # ids — never delete. A file-level read failure (no present ids + a
        # failure record) means the empty id set can't be trusted, so report none.
        keep = set(present)
        file_failed = not present and bool(plan_status["failed"])
        removed: list[str] = []
        if not file_failed:
            removed = [jid for jid in manager.plan_owned_ids() if jid not in keep]
        plan_status["removed"] = sorted(removed)
    return plan_status


def _introspect_active_plan(
    plan_src, wf_dir, only=None, exclude=None, profile=None
) -> "dict[str, Any]":
    """Resolve the active workflows dir + INGEST-PLAN into a read-only payload.

    Backs the ``GET /plan`` endpoint. Runs the **same** loaders and provenance
    plumbing that ``plan render --explain`` uses (:func:`_load_workflows_dir` and
    :func:`_load_ingest_plan` with ``provenance=...``), so each resolved job carries
    its originating source file and — for ``matrix:``/``for_each:`` fan-out — the
    matrix entry (key/index/vars) that produced it. Pure read: nothing is created,
    mutated, or deleted; the manager is never touched.

    Resolution mirrors startup: the workflows dir is scanned unfiltered, while the
    plan honours ``--only``/``--exclude`` exactly as :func:`_apply_ingest_plan` does
    — but an ``--only`` pattern that now matches nothing is treated as "no jobs
    selected" rather than fatal, since this route only reads. Returns the
    ``{"plan_path", "workflows_dir", "jobs", "failed"}`` shape consumed by
    :class:`~ujin.jobs.api_models.PlanIntrospection`, where every ``jobs`` entry is
    ``{"id", "provenance": {"source", ["matrix"]}}``.
    """
    prov: list = []
    failed: list = []
    # Workflows directory: scanned unfiltered, matching the startup load order
    # (only the plan applies --only/--exclude). A non-existent dir yields nothing.
    if wf_dir:
        _load_workflows_dir(wf_dir, failed=failed, provenance=prov, profile=profile)
    # INGEST-PLAN: resolve with provenance, then apply the same selection startup
    # would. A no-match --only is reported as zero selected, never raised (read-only).
    if plan_src:
        plan_prov: list = []
        plan_specs = _load_ingest_plan(
            plan_src, failed=failed, provenance=plan_prov, profile=profile
        )
        if only is not None or exclude is not None:
            try:
                keep = {s.id for s in _select_jobs(plan_specs, only, exclude)}
            except ValueError:
                keep = set()
            plan_prov = [p for p in plan_prov if p["id"] in keep]
        prov.extend(plan_prov)
    jobs = [
        {"id": p["id"], "provenance": {k: v for k, v in p.items() if k != "id"}}
        for p in prov
    ]
    return {
        "plan_path": str(plan_src) if plan_src else None,
        "workflows_dir": wf_dir,
        "jobs": jobs,
        "failed": failed,
    }


# ── opt-in plan-file watcher (--watch-plan / $UJIN_WATCH_PLAN) ──────────────── #
#
# Off by default: without the flag/env no watcher task is created and a no-config
# deploy does zero extra I/O. When enabled (and a plan IS configured) a single
# background task stats the plan file every `interval` seconds and, on a changed
# signature, re-runs the *identical* resolution as ``POST /plan/reload?prune=true``
# — the same :func:`_apply_ingest_plan` (defaults/include/matrix/``${VAR}`` plus
# ``--only``/``--exclude``) and the manager's provenance-aware upsert/prune — so
# added/changed jobs appear and removed plan-owned jobs are pruned with no restart
# and no manual POST.
#
# Safety properties (all inherited from _apply_ingest_plan, so they match the
# manual reload byte-for-byte):
#   * Plan-only: the prune set is ``manager.plan_owned_ids()``, so API-created and
#     positional-preload jobs are never pruned even when absent from the plan.
#   * Non-destructive on a bad save: a file-level read/parse error is reported in
#     ``failed`` and prune is skipped, so a malformed plan never wipes the running
#     job set — the last good jobs stay put.
#   * Race-free with the manual endpoint: ``_apply_ingest_plan`` contains no
#     ``await``, so a watcher tick and a concurrent ``POST /plan/reload`` can never
#     interleave mid-apply; and because re-applying the same ids is idempotent, even
#     a redundant double-apply is harmless.

WATCH_PLAN_ENV = "UJIN_WATCH_PLAN"
WATCH_INTERVAL_ENV = "UJIN_WATCH_INTERVAL"
#: Default seconds between plan-file change checks. A stat-only poll, so cheap.
DEFAULT_WATCH_INTERVAL = 2.0


def _env_truthy(raw: "str | None") -> bool:
    """Shell-ish truthiness for a boolean env var (matches ``ScrapeConfig``)."""
    return bool(raw) and raw.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_watch(
    watch_plan: bool, watch_interval: "float | None"
) -> "tuple[bool, float]":
    """Fold the flag and env into ``(enabled, interval)``.

    The watcher is enabled by ``--watch-plan`` (``watch_plan=True``) **or** a truthy
    ``$UJIN_WATCH_PLAN``. The interval is the ``--watch-interval`` value when given,
    else ``$UJIN_WATCH_INTERVAL``, else :data:`DEFAULT_WATCH_INTERVAL`; a
    non-positive or unparseable value falls back to the default so a typo can never
    busy-loop or stall the watcher.
    """
    enabled = bool(watch_plan) or _env_truthy(os.environ.get(WATCH_PLAN_ENV))
    interval = watch_interval
    if interval is None:
        raw = os.environ.get(WATCH_INTERVAL_ENV)
        if raw:
            try:
                interval = float(raw)
            except ValueError:
                log.warning("ignoring non-numeric %s=%r", WATCH_INTERVAL_ENV, raw)
                interval = None
    if interval is None or interval <= 0:
        interval = DEFAULT_WATCH_INTERVAL
    return enabled, interval


class _PlanWatcher:
    """Background poller that re-applies the INGEST-PLAN when its file changes.

    Stats *plan_src* every *interval* seconds; on a changed signature (mtime + size)
    re-runs :func:`_apply_ingest_plan` with ``prune=True`` and refreshes
    ``app.state.plan`` — the same effect as ``POST /plan/reload?prune=true`` with no
    restart. Construction snapshots the current file signature, so only edits made
    *after* startup trigger a reload. Drive :meth:`poll_once` directly for a
    deterministic, offline test, or schedule :meth:`run` as a background task.
    """

    def __init__(
        self, app, manager, plan_src, only, exclude, interval, *, sleep=None,
        profile=None,
    ):
        self.app = app
        self.manager = manager
        self.plan_src = plan_src
        self.only = only
        self.exclude = exclude
        self.interval = interval
        self.profile = profile
        self._sleep = sleep or asyncio.sleep
        self._sig = self._signature()

    def _signature(self):
        """``(mtime_ns, size)`` of the plan file, or ``None`` if it can't be stat-ed.

        ``None`` (a missing/unreadable file) is treated as "no actionable change":
        a transiently-absent plan must never trigger a reload that wipes jobs.
        """
        try:
            st = os.stat(self.plan_src)
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def poll_once(self) -> bool:
        """One change check; reload (with prune) iff the file changed.

        Returns ``True`` when a reload ran, ``False`` when the file was unchanged,
        unreadable, or an ``--only`` pattern now matches nothing. Synchronous and
        free of ``await`` (it only calls :func:`_apply_ingest_plan`), so it can't
        interleave with a concurrent ``POST /plan/reload``. A reload that resolves to
        a malformed/transient plan is non-destructive: the failure is reported in
        ``app.state.plan['failed']`` and the running jobs are kept.
        """
        sig = self._signature()
        if sig is None or sig == self._sig:
            return False
        # Record the new signature up front so a persistently-broken file is not
        # re-applied (and re-logged) every tick; the next real edit changes it again.
        self._sig = sig
        try:
            status = _apply_ingest_plan(
                self.manager, self.plan_src, only=self.only, exclude=self.exclude,
                prune=True, profile=self.profile,
            )
        except ValueError as exc:
            # --only now matches nothing: surface it, keep the last good job set.
            log.warning("plan watch: %s", exc)
            return False
        self.app.state.plan = status
        if status["failed"]:
            log.warning(
                "plan watch: reloaded %s with %d failure(s); kept last good jobs",
                self.plan_src, len(status["failed"]),
            )
        else:
            log.info(
                "plan watch: reloaded %s (%d job(s), %d pruned)",
                self.plan_src, len(status["loaded"]), len(status.get("pruned", [])),
            )
        return True

    async def run(self, stop=None, *, max_ticks=None) -> None:
        """Poll until *stop* is set (or *max_ticks* reached), surviving tick errors.

        Mirrors :meth:`JobManager.cron_loop`'s shape: sleep *interval*, check once,
        repeat. An unexpected error in a tick is logged and the loop continues — the
        watcher must never take the server down.
        """
        ticks = 0
        while stop is None or not stop.is_set():
            await self._sleep(self.interval)
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 - a tick must never crash the loop
                log.exception("plan watch: unexpected error; continuing")
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                return


def create_jobs_app(
    config_path: str | None = None,
    *,
    workflows_dir: str | None = None,
    plan_path: str | None = None,
    scrape_config: Any = None,
    run_engine: bool = True,
    only: list | None = None,
    exclude: list | None = None,
    watch_plan: bool = False,
    watch_interval: float | None = None,
    profile: str | None = None,
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
    # Active profile overlay: the `--profile` flag (passed as profile) wins over
    # $UJIN_PLAN_PROFILE; neither set -> None -> no overlay (byte-identical to today).
    # Resolved once here so startup, reload, the watcher, and GET /plan all apply the
    # same profile, exactly as plan_src/only/exclude are shared.
    profile_name = profile or os.environ.get(PLAN_PROFILE_ENV) or None
    # Opt-in plan-file watcher. Resolved once here (flag wins over env); off by
    # default, so without it the lifespan creates no watcher task and does no extra
    # I/O — a no-config deploy is byte-identical to today.
    watch_enabled, watch_secs = _resolve_watch(watch_plan, watch_interval)

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
                    manager, plan_src, only=only, exclude=exclude,
                    profile=profile_name,
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

        # Opt-in: a single background task that auto-reloads the plan on file change
        # (prune semantics), exactly as POST /plan/reload?prune=true would. Only
        # started when enabled AND a plan is configured (the reload it mirrors is
        # plan-only); attached to app.state for deterministic testing and cancelled
        # with the others on shutdown. Unset -> app.state.plan_watcher is None and
        # no task/I/O is added.
        app.state.plan_watcher = None
        if watch_enabled and plan_src:
            watcher = _PlanWatcher(
                app, manager, plan_src, only, exclude, watch_secs,
                profile=profile_name,
            )
            app.state.plan_watcher = watcher
            tasks.append(asyncio.create_task(watcher.run()))
            log.info("ujin jobs: watching plan %s every %gs", plan_src, watch_secs)
        elif watch_enabled:
            log.warning(
                "ujin jobs: plan watch requested but no plan configured; "
                "watcher inactive"
            )

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

    @app.get("/plan", response_model=PlanIntrospection)
    def plan_introspect() -> "PlanIntrospection":
        """Read-only view of the jobs the active plan / workflows dir resolve to.

        Re-runs the **same** loaders and provenance plumbing as ``plan render
        --explain`` (via :func:`_introspect_active_plan`) over the configured
        workflows directory and INGEST-PLAN, returning each resolved job's id plus
        its provenance — the originating source file and, for ``matrix:``/
        ``for_each:``-expanded jobs, the matrix entry (key/index/vars) that produced
        it. Purely introspective: it never creates, mutates, or deletes a job, so
        every other endpoint behaves exactly as before. Unresolvable files/jobs are
        reported in ``failed`` exactly as ``GET /health`` lists them. With neither a
        plan nor a populated workflows dir configured, ``jobs`` is empty.
        """
        return PlanIntrospection(
            **_introspect_active_plan(
                plan_src, wf_dir, only=only, exclude=exclude, profile=profile_name
            )
        )

    @app.post("/plan/reload")
    async def plan_reload(
        request: Request, prune: bool = False, dry_run: bool = False
    ) -> dict[str, Any]:
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

        Pass ``?dry_run=true`` (or a ``{"dry_run": true}`` JSON body) to **preview**
        the effect without mutating anything: the plan resolves exactly as a real
        reload would, but instead of upserting, the response reports which plan job
        ids would be ``added``/``updated``/``unchanged`` (and, with ``prune``, which
        would be ``removed``). ``reloaded`` stays ``false`` and ``GET /jobs`` and
        ``GET /health`` are untouched — identical immediately before and after.
        """
        # Accept the opt-ins via either query params (?prune=true&dry_run=true) or a
        # JSON body ({"prune": true, "dry_run": true}). Read the body once for both.
        if not prune or not dry_run:
            try:
                body = await request.json()
            except Exception:  # noqa: BLE001 - no/invalid body -> query params only
                body = None
            if isinstance(body, dict):
                if not prune:
                    prune = bool(body.get("prune", False))
                if not dry_run:
                    dry_run = bool(body.get("dry_run", False))
        if not plan_src:
            out = {"reloaded": False, "detail": "no plan configured",
                   "loaded": [], "failed": []}
            if dry_run:
                out["dry_run"] = True
            return out
        if dry_run:
            # Preview only: never touch the manager or app.state.plan.
            try:
                preview = _dryrun_ingest_plan(
                    app.state.manager, plan_src, only=only, exclude=exclude,
                    prune=prune, profile=profile_name,
                )
            except ValueError as exc:
                return {"reloaded": False, "dry_run": True, "detail": str(exc),
                        "loaded": [], "failed": []}
            return {"reloaded": False, "dry_run": True, **preview}
        try:
            plan_status = _apply_ingest_plan(
                app.state.manager, plan_src, only=only, exclude=exclude, prune=prune,
                profile=profile_name,
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
    watch_plan: bool = False,
    watch_interval: float | None = None,
    profile: str | None = None,
) -> None:
    import uvicorn

    uvicorn.run(
        create_jobs_app(config_path, workflows_dir=workflows_dir, plan_path=plan_path,
                        only=only, exclude=exclude, watch_plan=watch_plan,
                        watch_interval=watch_interval, profile=profile),
        host=host, port=port,
    )
