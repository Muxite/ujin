"""External ingest-plan loader — declare many jobs from one file.

ujin ships no site-specific ingest plans; a **plan** is plain data the consuming
program owns (mounted as a file, or set via ``$UJIN_INGEST_PLAN``) describing many
jobs at once: shared ``defaults:`` deep-merged into every job, plus an optional
per-job ``matrix:`` that fans one job template out into one job per combination.

Plan schema::

    defaults:                        # deep-merged into every job (lists replace)
      sinks: [{kind: sqlite, config: {}}]
      schedule: {mode: interval, seconds: 300}
    jobs:
      - id: status-${name}           # optional; "${var}" substituted per matrix combo
        matrix:
          name: [github, npm, pypi]  # mapping of axis -> values -> cartesian product
          # OR explicit rows:
          # - {name: github, base: 300}
          # - {name: npm, base: 600}
        source:
          kind: http
          config:
            url: "https://example.com/${name}"     # "${VAR}" also expands from env

Substitution runs in two passes per generated job: first the per-combo matrix
variables (type-preserving when a scalar is *exactly* one `${var}` reference, e.g.
`base: ${base}` keeps `base` numeric), then `${ENV_VAR}` / `${ENV_VAR:-default}`
against the process environment (reusing :func:`ujin.jobs.app._expand_env`) — a name
the matrix pass doesn't recognize is left untouched so the env pass can still expand
it. A job with no `matrix:` still gets a single pass through both (an effectively
empty combo, so only `${ENV_VAR}` refs resolve).

The plan path may point at a single file **or a directory** of plan files. A
directory is loaded like the workflows dir: every ``*.yaml``/``*.yml`` file in it
is parsed in sorted (deterministic) order and its jobs merged into one set, with
a malformed file isolated (recorded + skipped) rather than aborting the whole
set. See :func:`load_plan_dir`.

See ``examples/ingest-plan.yaml`` (single file), ``examples/ingest-plan.d/``
(directory) and ``docs/INGEST_PLAN.md`` for full examples and reference. This
module is self-contained aside from reusing ``app._expand_env`` for leaf-level
environment expansion.
"""
from __future__ import annotations

import copy
import itertools
import logging
import os
import re
from pathlib import Path
from typing import Any

log = logging.getLogger("ujin.jobs.plan")

#: Env var holding a path to a plan file (mountable as a volume), mirroring
#: ``ujin.poll.marketplace.PROFILES_ENV`` (``UJIN_MARKETPLACE_PROFILES``).
PLAN_ENV = "UJIN_INGEST_PLAN"

_MATRIX_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_WHOLE_MATRIX_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")

_TOP_LEVEL_KEYS = {"defaults", "jobs"}

#: Glob matching plan files inside a plan *directory* (``*.yaml``/``*.yml``),
#: mirroring :func:`ujin.jobs.app._load_workflows_dir`.
_PLAN_GLOB = "*.y*ml"


class PlanError(ValueError):
    """Raised when an ingest plan file is missing, malformed, or invalid."""


def _deep_merge(base: Any, override: Any) -> Any:
    """Merge *override* onto *base*; nested mappings merge, everything else
    (including lists) is replaced wholesale by *override*."""
    if isinstance(base, dict) and isinstance(override, dict):
        merged = {k: copy.deepcopy(v) for k, v in base.items()}
        for key, value in override.items():
            merged[key] = _deep_merge(merged.get(key), value)
        return merged
    return copy.deepcopy(override)


def _matrix_combos(matrix: Any) -> list[dict[str, Any]]:
    """Expand a ``matrix:`` value into a list of variable-name -> value combos.

    A mapping of ``axis -> [values]`` becomes the cartesian product of all axes.
    A list is taken as explicit rows (each entry already one combo). An empty
    matrix (``{}``/``[]``) yields no combos, so that job entry produces zero jobs.
    """
    if not matrix:
        return []
    if isinstance(matrix, list):
        combos = []
        for row in matrix:
            if not isinstance(row, dict):
                raise PlanError(f"matrix list entries must be mappings, got {row!r}")
            combos.append(dict(row))
        return combos
    if isinstance(matrix, dict):
        axes: list[str] = []
        values: list[list[Any]] = []
        for axis, vals in matrix.items():
            if not isinstance(vals, list):
                raise PlanError(f"matrix axis {axis!r} must be a list, got {vals!r}")
            axes.append(axis)
            values.append(vals)
        return [dict(zip(axes, combo)) for combo in itertools.product(*values)]
    raise PlanError(f"matrix must be a mapping or a list of mappings, got {matrix!r}")


def _subst_matrix_scalar(value: Any, combo: dict[str, Any]) -> Any:
    if not isinstance(value, str):
        return value
    whole = _WHOLE_MATRIX_REF.match(value)
    if whole:
        name = whole.group(1)
        return combo[name] if name in combo else value  # type-preserving

    def _sub(m: "re.Match[str]") -> str:
        name = m.group(1)
        return str(combo[name]) if name in combo else m.group(0)  # passthrough

    return _MATRIX_REF.sub(_sub, value)


def _subst_matrix_tree(node: Any, combo: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        return {k: _subst_matrix_tree(v, combo) for k, v in node.items()}
    if isinstance(node, list):
        return [_subst_matrix_tree(v, combo) for v in node]
    return _subst_matrix_scalar(node, combo)


def _expand_env_tree(node: Any) -> Any:
    from .app import _expand_env

    if isinstance(node, dict):
        return {k: _expand_env_tree(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_expand_env_tree(v) for v in node]
    if isinstance(node, str):
        return _expand_env(node)
    return node


def _fallback_stem(entry: dict[str, Any], entry_index: int) -> str:
    raw_id = entry.get("id")
    if isinstance(raw_id, str) and "${" not in raw_id:
        return raw_id
    return f"job-{entry_index}"


def _assign_ids(entry: dict[str, Any], entry_index: int, combo_jobs: list[dict]) -> list[str]:
    ids = [job.get("id") for job in combo_jobs]
    if all(ids) and len(set(ids)) == len(ids):
        return ids
    stem = _fallback_stem(entry, entry_index)
    if len(combo_jobs) == 1:
        return [stem]
    return [f"{stem}-{i}" for i in range(len(combo_jobs))]


def parse_plan(data: Any) -> list[dict[str, Any]]:
    """Parse an already-``yaml.safe_load``-ed plan document into job dicts.

    ``data`` must be a mapping with only ``defaults:`` / ``jobs:`` at the top
    level (or ``None``/empty, which yields no jobs). Each entry in ``jobs:`` is
    deep-merged onto ``defaults:``, fanned out by its own ``matrix:`` (if any),
    and returned as a plain dict ready for ``JobSpec.from_dict``.
    """
    if not data:
        return []
    if not isinstance(data, dict):
        raise PlanError(f"ingest plan must be a mapping, got {type(data).__name__}")
    extra = set(data) - _TOP_LEVEL_KEYS
    if extra:
        raise PlanError(f"ingest plan has unknown top-level key(s): {sorted(extra)}")

    defaults = data.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise PlanError("ingest plan `defaults:` must be a mapping")

    jobs = data.get("jobs") or []
    if not isinstance(jobs, list):
        raise PlanError("ingest plan `jobs:` must be a list")

    specs: list[dict[str, Any]] = []
    for entry_index, entry in enumerate(jobs):
        if not isinstance(entry, dict):
            raise PlanError(f"ingest plan job #{entry_index} must be a mapping, got {entry!r}")
        matrix_spec = entry.get("matrix")
        body = {k: v for k, v in entry.items() if k != "matrix"}
        merged = _deep_merge(defaults, body)
        combos = _matrix_combos(matrix_spec) if "matrix" in entry else [{}]

        combo_jobs = []
        for combo in combos:
            substituted = _subst_matrix_tree(merged, combo)
            combo_jobs.append(_expand_env_tree(substituted))

        for job, job_id in zip(combo_jobs, _assign_ids(entry, entry_index, combo_jobs)):
            job = dict(job)
            job["id"] = job_id
            job.setdefault("name", job_id)
            specs.append(job)
    return specs


def _load_plan_file(p: Path) -> list[dict[str, Any]]:
    """Read + parse a single plan *file*. Raises :class:`PlanError` on any
    problem (missing, unreadable, invalid YAML, or bad schema)."""
    import yaml

    if not p.exists():
        raise PlanError(f"ingest plan file not found: {p}")
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise PlanError(f"ingest plan {p} could not be read: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PlanError(f"ingest plan {p} is not valid YAML: {exc}") from exc
    return parse_plan(data)


def load_plan_dir(path: str | os.PathLike[str]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Load every plan file in directory *path* in a stable, sorted order.

    Mirrors :func:`ujin.jobs.app._load_workflows_dir`: each ``*.yaml``/``*.yml``
    file is parsed independently and its jobs appended to a single merged list.
    A file that is malformed is isolated — its error is recorded and the file
    skipped, so the remaining valid files still load. Returns a
    ``(jobs, failures)`` tuple where *failures* is a list of
    ``{"file": <name>, "error": <message>}`` dicts (empty when all files loaded).
    """
    root = Path(path)
    jobs: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for fp in sorted(root.glob(_PLAN_GLOB)):
        try:
            jobs.extend(_load_plan_file(fp))
        except PlanError as exc:
            log.warning("skipping ingest plan file %s: %s", fp, exc)
            failures.append({"file": fp.name, "error": str(exc)})
    return jobs, failures


def load_plan(path: str | None = None) -> list[dict[str, Any]]:
    """Load + parse an ingest plan from *path* or ``$UJIN_INGEST_PLAN``.

    Returns an empty list when neither is set (no plan -> no jobs), so mounting
    nothing is a silent no-op rather than an error. When the resolved path is a
    **directory**, every plan file inside it is loaded and merged (malformed
    files are skipped, mirroring the workflows dir); use :func:`load_plan_dir`
    to also capture the per-file failures. When it is a single **file** that is
    missing, unreadable, or malformed, :class:`PlanError` is raised.
    """
    src = path or os.environ.get(PLAN_ENV)
    if not src:
        return []
    p = Path(src)
    if p.is_dir():
        jobs, _failures = load_plan_dir(p)
        return jobs
    return _load_plan_file(p)
