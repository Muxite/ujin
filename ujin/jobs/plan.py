"""Ingest-plan loader — one mounted file that declares a whole fleet of jobs.

An *ingest plan* is a single YAML document that expands into many jobs. Where the
[workflows directory](../../docs/WORKFLOWS.md) is "one file = one job", a plan is
"one file = a topology": shared configuration is factored out into ``defaults``
(deep-merged into every job), reused through named ``templates`` (opted into with
``use:``), and multiplied across a per-job ``matrix`` (cartesian expansion).
``${VAR}`` / ``${VAR:-default}`` environment references are substituted last.

The document shape is intentionally tiny::

    defaults:                 # optional: merged into every job (per-job keys win)
      schedule: {mode: adaptive, base: 600}
    templates:                # optional: named fragments a job opts into
      feed: {source: {kind: rss}}
    jobs:                     # a list of job mappings (or a bare top-level list)
      - id: homepage
        source: {kind: http, config: {url: "https://example.com/"}}
      - id: mirror-${region}  # matrix vars substitute anywhere in the body
        use: feed
        matrix: {region: [us, eu]}
        source: {config: {url: "https://${region}.example/feed"}}

Precedence, lowest to highest: ``defaults`` < template(s) (in listed order) < the
job's own keys.

:func:`load_plan` / :func:`parse_plan` are non-fatal *for their caller*: they raise
:class:`PlanError` with an actionable message on a malformed document, and the jobs
service catches that to surface it on ``/health`` rather than crashing.

Only :func:`ujin.jobs.app._expand_env` is reused, for the environment pass.
"""
from __future__ import annotations

import copy
import itertools
import os
import re
from pathlib import Path
from typing import Any

from .app import _expand_env
from .model import JobSpec

#: Environment variable naming a default plan file when no ``--plan`` is passed.
PLAN_ENV = "UJIN_INGEST_PLAN"

#: Top-level keys a plan document may carry. ``fragments`` is an alias of
#: ``templates``. Anything else is a typo.
_ALLOWED_TOP = ("defaults", "templates", "fragments", "jobs")

#: Per-job control keys consumed by the loader itself (never handed to JobSpec).
_CONTROL_KEYS = ("use", "matrix")

#: A matrix reference ``${name}`` (no ``:-default`` form — that is left for the
#: env pass). A value that is *exactly* ``${name}`` is a whole-scalar reference:
#: it is replaced by the raw combo value so a numeric entry keeps its type.
_MATRIX_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class PlanError(ValueError):
    """A plan document is malformed (bad shape, unknown key/template, bad matrix)."""


def load_plan(path: str | None = None) -> list[JobSpec]:
    """Load an ingest plan into :class:`~ujin.jobs.model.JobSpec` objects.

    *path* wins; otherwise ``$UJIN_INGEST_PLAN`` is consulted. When neither names
    a file, an empty list is returned so a no-plan deploy loads nothing. A missing
    file or invalid YAML raises :class:`PlanError` with an actionable message.
    """
    source = path or os.environ.get(PLAN_ENV)
    if not source:
        return []
    import yaml

    p = Path(source)
    if not p.exists():
        raise PlanError(f"ingest plan not found: {source}")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:  # noqa: F841 -- surfaced in the message
        raise PlanError(
            f"ingest plan ({source}) is not valid YAML/JSON: {exc}"
        ) from None
    return parse_plan(data, source=str(source))


def parse_plan(data: Any, *, source: str | None = None) -> list[JobSpec]:
    """Resolve a plan document into a list of :class:`~ujin.jobs.model.JobSpec`.

    Validates the top-level shape, applies ``defaults`` and ``templates``, fans
    out any per-job ``matrix``, substitutes ``${VAR}`` references, and assigns a
    stable id to every produced job. Raises :class:`PlanError` on a malformed
    document. *source* names the plan (a path) for error messages and id fallback.
    """
    stem = Path(source).stem if source else "plan"
    if data is None:
        return []
    if isinstance(data, list):
        defaults, templates, jobs = {}, {}, data
    elif isinstance(data, dict):
        unknown = [k for k in data if k not in _ALLOWED_TOP]
        if unknown:
            raise PlanError(
                _pfx(source) + f"unknown top-level key(s) {unknown}; "
                "allowed keys: defaults, templates, jobs"
            )
        defaults = _require_mapping(data.get("defaults"), "defaults", source)
        templates = _require_mapping(
            data.get("templates", data.get("fragments")), "templates", source
        )
        if "jobs" not in data:
            raise PlanError(_pfx(source) + "must define a 'jobs' list")
        jobs = data["jobs"]
        if not isinstance(jobs, list):
            raise PlanError(
                _pfx(source) + f"'jobs' must be a list of jobs, got "
                f"{type(jobs).__name__}"
            )
    else:
        raise PlanError(
            _pfx(source) + f"must be a mapping or a list of jobs, got "
            f"{type(data).__name__}"
        )

    specs: list[JobSpec] = []
    for i, job in enumerate(jobs):
        if not isinstance(job, dict):
            raise PlanError(
                _pfx(source) + f"job #{i} must be a mapping, got "
                f"{type(job).__name__}"
            )
        merged = _apply_templates(defaults, templates, job, i, source)
        matrix = merged.pop("matrix", None)
        combos = [{}] if matrix is None else _matrix_combos(matrix, i, source)
        for local, combo in enumerate(combos):
            specs.append(
                _build_spec(
                    merged, combo, matrix is not None, local, len(specs), stem, source
                )
            )
    return specs


def _pfx(source: str | None) -> str:
    """Message prefix naming the offending plan, e.g. ``ingest plan (x.yaml): ``."""
    return f"ingest plan ({source}): " if source else "ingest plan: "


def _require_mapping(value: Any, key: str, source: str | None) -> dict:
    """Return *value* as a mapping (``None`` -> ``{}``); else raise ``PlanError``."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PlanError(
            _pfx(source) + f"'{key}' must be a mapping, got {type(value).__name__}"
        )
    return value


def _apply_templates(
    defaults: dict, templates: dict, job: dict, index: int, source: str | None
) -> dict:
    """Deep-merge ``defaults`` < opted-in ``templates`` < the job's own keys."""
    use = job.get("use")
    names: list[str] = []
    if use is not None:
        names = [use] if isinstance(use, str) else list(use)
    merged = copy.deepcopy(defaults)
    for name in names:
        if name not in templates:
            raise PlanError(
                _pfx(source) + f"job #{index} references unknown template "
                f"{name!r}; defined templates: {sorted(templates)}"
            )
        merged = _deep_merge(merged, templates[name])
    body = {k: v for k, v in job.items() if k != "use"}
    return _deep_merge(merged, body)


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* onto a deep copy of *base* (override wins).

    Nested mappings merge key-by-key; every other value (including lists) is
    replaced wholesale, so a job that sets ``sinks:`` overrides — not appends to —
    the defaults' ``sinks:``.
    """
    out = copy.deepcopy(base)
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = copy.deepcopy(val)
    return out


def _matrix_combos(matrix: Any, index: int, source: str | None) -> list[dict]:
    """Expand a ``matrix:`` mapping into a list of ``{var: value}`` combinations.

    ``{var: [values], ...}`` becomes the cartesian product of its axes. An empty
    mapping, or any axis that is not a non-empty list, is a hard error.
    """
    if not isinstance(matrix, dict) or not matrix:
        raise PlanError(
            _pfx(source) + f"job #{index}: 'matrix' must be a non-empty mapping "
            "of axis -> list of values"
        )
    axes = []
    for var, values in matrix.items():
        if not isinstance(values, list) or not values:
            raise PlanError(
                _pfx(source) + f"job #{index}: matrix axis {var!r} must be a "
                "non-empty list of values"
            )
        axes.append([(var, v) for v in values])
    return [dict(pairs) for pairs in itertools.product(*axes)]


def _subst_matrix_scalar(value: Any, combo: dict) -> Any:
    """Substitute matrix vars into *value*, type-preserving for whole-scalar refs.

    A value that is exactly ``${var}`` (with ``var`` in *combo*) becomes the raw
    combo value, so a numeric matrix entry stays an int/float. Partial references
    (``"${var}/path"``) are string-substituted. References whose name is not a
    matrix var are left untouched for the later env pass.
    """
    if isinstance(value, dict):
        return {k: _subst_matrix_scalar(v, combo) for k, v in value.items()}
    if isinstance(value, list):
        return [_subst_matrix_scalar(v, combo) for v in value]
    if isinstance(value, str):
        whole = _MATRIX_REF.fullmatch(value)
        if whole is not None and whole.group(1) in combo:
            return combo[whole.group(1)]

        def _sub(m: "re.Match[str]") -> str:
            name = m.group(1)
            return str(combo[name]) if name in combo else m.group(0)

        return _MATRIX_REF.sub(_sub, value)
    return value


def _expand_env_tree(value: Any) -> Any:
    """Apply ``${VAR}`` / ``${VAR:-default}`` env substitution across a tree."""
    if isinstance(value, dict):
        return {k: _expand_env_tree(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env_tree(v) for v in value]
    if isinstance(value, str):
        return _expand_env(value)
    return value


def _build_spec(
    merged: dict,
    combo: dict,
    has_matrix: bool,
    local: int,
    seq: int,
    stem: str,
    source: str | None,
) -> JobSpec:
    """Substitute one matrix combo into *merged*, resolve its id, build a JobSpec.

    *local* is the combination index within this entry; *seq* is the running
    position across the whole plan (used only for the no-id fallback).
    """
    body = _build_body(merged, combo, has_matrix, local, seq, stem)
    job_id = body["id"]

    src = body.get("source")
    if not isinstance(src, dict) or not src.get("kind"):
        raise PlanError(
            _pfx(source) + f"job {job_id!r} needs a 'source' with a 'kind'"
        )
    try:
        return JobSpec.from_dict(body)
    except Exception as exc:  # noqa: BLE001 -- surfaced as an actionable PlanError
        raise PlanError(
            _pfx(source) + f"job {job_id!r} is invalid: {exc}"
        ) from None


def _build_body(merged, combo, has_matrix, local, seq, stem) -> dict:
    """Resolve one plan combination before constructing its JobSpec."""
    body = _expand_env_tree(_subst_matrix_scalar(copy.deepcopy(merged), combo))

    raw_id = merged.get("id")
    if raw_id is None:
        job_id = f"{stem}-{seq}"
    else:
        referenced = {n for n in _MATRIX_REF.findall(str(raw_id)) if n in combo}
        resolved = _expand_env(_subst_matrix_scalar(str(raw_id), combo))
        # A matrix job whose id names no axis would collide across combos, so the
        # combination index is appended to keep ids distinct (mirror-0, mirror-1).
        job_id = f"{resolved}-{local}" if has_matrix and not referenced else resolved
    body["id"] = job_id
    body.setdefault("name", job_id)

    return body
