"""Offline validation of jobs/workflow/plan config — no server, no store, no I/O.

Mirrors the exact yaml-shape, ``defaults:``/``include:``/``use:`` resolution,
``matrix:``/``for_each:`` fan-out, and ``${VAR}``/``${VAR:-default}``
substitution rules that :mod:`ujin.jobs.app` and :mod:`ujin.jobs.plan` use at
startup (``_preload_specs`` for a single jobs file, ``_specs_from_workflow_file``
/``_load_workflows_dir`` for a workflows directory, ``load_plan``/``load_plan_dir``
for an ingest plan), but walks entries **one at a time** wherever the loader
itself allows it, so a single bad job doesn't hide problems in the rest of the
file, and reports every problem with the offending source, the job, and the
underlying error rather than crashing or silently skipping.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .app import (
    WorkflowIncludeError,
    WorkflowMatrixError,
    _expand_env,
    _file_directives,
    _MATRIX_KEYS,
    _resolve_job,
    _specs_from_matrix,
)

_JOBS_FILE_KEYS = {"jobs", "defaults", "include", "use"}


@dataclass
class ValidationIssue:
    """One actionable problem found while validating a source."""

    source: str
    entry: str
    error: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.source} [{self.entry}]: {self.error}"


@dataclass
class ValidationResult:
    """Outcome of a :func:`validate` call."""

    loaded: list[str] = field(default_factory=list)
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues


def _check_matrix_shape(entry: dict) -> str | None:
    """Return an error message if entry's optional ``matrix:`` key is malformed."""
    if "matrix" not in entry:
        return None
    matrix = entry["matrix"]
    if not isinstance(matrix, dict):
        return f"'matrix:' must be a mapping of variable -> list of values, got {type(matrix).__name__}"
    for key, values in matrix.items():
        if not isinstance(values, list):
            return f"matrix variable '{key}' must be a list of values, got {type(values).__name__}"
    return None


def _validate_entry(entry: Any, source: str, label: str, seen_ids: dict, issues: list) -> str | None:
    """Validate one raw job dict; returns the job id on success, else None."""
    from .model import JobSpec

    if not isinstance(entry, dict):
        issues.append(ValidationIssue(source, label, f"job entry must be a mapping, got {type(entry).__name__}"))
        return None

    matrix_error = _check_matrix_shape(entry)
    if matrix_error:
        issues.append(ValidationIssue(source, label, matrix_error))
        return None

    try:
        spec = JobSpec.from_dict(entry)
    except Exception as exc:  # noqa: BLE001 - report every failure mode, don't crash
        issues.append(ValidationIssue(source, label, str(exc)))
        return None

    if spec.id in seen_ids:
        issues.append(ValidationIssue(
            source, label,
            f"duplicate job id {spec.id!r} (already defined in {seen_ids[spec.id]})",
        ))
        return None

    seen_ids[spec.id] = source
    return spec.id


def _register_specs(specs: list, source: str, seen_ids: dict, issues: list, label_of) -> list[str]:
    """Duplicate-id-check a list of already-built :class:`JobSpec`\\ s.

    Used for the ``matrix:``/``for_each:`` fan-out path, where the loader itself
    (:func:`ujin.jobs.app._specs_from_matrix`) builds every fanned-out
    :class:`JobSpec` in one pass rather than one entry at a time, so per-row
    schema errors can't be isolated the way plain job entries are.
    """
    loaded = []
    for spec in specs:
        if spec.id in seen_ids:
            issues.append(ValidationIssue(
                source, label_of(spec),
                f"duplicate job id {spec.id!r} (already defined in {seen_ids[spec.id]})",
            ))
            continue
        seen_ids[spec.id] = source
        loaded.append(spec.id)
    return loaded


def _validate_jobs_file(path: str, seen_ids: dict, issues: list) -> list[str]:
    import yaml

    try:
        text = _expand_env(open(path, encoding="utf-8").read())
        data = yaml.safe_load(text) or {}
    except OSError as exc:
        issues.append(ValidationIssue(path, "<file>", f"could not read file: {exc}"))
        return []
    except yaml.YAMLError as exc:
        issues.append(ValidationIssue(path, "<file>", f"invalid YAML: {exc}"))
        return []

    defaults: dict = {}
    file_includes: list = []
    if isinstance(data, dict):
        defaults, file_includes, data = _file_directives(data)
        unknown = set(data) - _JOBS_FILE_KEYS
        if unknown:
            issues.append(ValidationIssue(
                path, "<file>",
                f"unknown top-level key(s): {', '.join(sorted(unknown))} "
                f"(expected only 'jobs', 'defaults', 'include'/'use')",
            ))
        raw = data.get("jobs", [])
    else:
        raw = data

    loaded = []
    for i, entry in enumerate(raw or []):
        label = f"job #{i}"
        if isinstance(entry, dict):
            try:
                entry = _resolve_job(entry, defaults, file_includes, path)
            except WorkflowIncludeError as exc:
                issues.append(ValidationIssue(path, label, str(exc)))
                continue
        job_id = _validate_entry(entry, path, label, seen_ids, issues)
        if job_id is not None:
            loaded.append(job_id)
    return loaded


def _validate_workflow_file(path: Path, seen_ids: dict, issues: list) -> list[str]:
    import yaml

    stem = path.stem
    spath = str(path)
    try:
        raw_text = open(path, encoding="utf-8").read()
    except OSError as exc:
        issues.append(ValidationIssue(spath, "<file>", f"could not read file: {exc}"))
        return []

    # Matrix fan-out is resolved off the *unexpanded* text, mirroring
    # ujin.jobs.app._specs_from_workflow_file, so `{{ var }}` substitutes before
    # `${ENV}`. A malformed matrix/include covers the whole file (no per-row
    # isolation upstream either), rather than one issue per fanned-out job.
    try:
        parsed = yaml.safe_load(raw_text) or {}
    except yaml.YAMLError as exc:
        issues.append(ValidationIssue(spath, "<file>", f"invalid YAML: {exc}"))
        return []

    if isinstance(parsed, dict) and any(k in parsed for k in _MATRIX_KEYS):
        try:
            specs = _specs_from_matrix(parsed, stem, path)
        except (WorkflowMatrixError, WorkflowIncludeError, ValueError) as exc:
            issues.append(ValidationIssue(spath, stem, str(exc)))
            return []
        return _register_specs(specs, spath, seen_ids, issues, lambda s: s.id)

    try:
        data = yaml.safe_load(_expand_env(raw_text)) or {}
    except yaml.YAMLError as exc:
        issues.append(ValidationIssue(spath, "<file>", f"invalid YAML: {exc}"))
        return []

    defaults: dict = {}
    file_includes: list = []
    if isinstance(data, dict):
        defaults, file_includes, data = _file_directives(data)

    if isinstance(data, dict) and "jobs" not in data:
        try:
            d = _resolve_job(data, defaults, file_includes, path)
        except WorkflowIncludeError as exc:
            issues.append(ValidationIssue(spath, stem, str(exc)))
            return []
        d.setdefault("id", stem)
        d.setdefault("name", stem)
        job_id = _validate_entry(d, spath, stem, seen_ids, issues)
        return [job_id] if job_id is not None else []

    raw = data.get("jobs", data) if isinstance(data, dict) else data
    raw = raw or []
    loaded = []
    for i, entry in enumerate(raw):
        label = stem if len(raw) == 1 else f"{stem}-{i}"
        if isinstance(entry, dict):
            try:
                entry = _resolve_job(entry, defaults, file_includes, path)
            except WorkflowIncludeError as exc:
                issues.append(ValidationIssue(spath, label, str(exc)))
                continue
            entry.setdefault("id", label)
            entry.setdefault("name", entry["id"])
        job_id = _validate_entry(entry, spath, label, seen_ids, issues)
        if job_id is not None:
            loaded.append(job_id)
    return loaded


def _validate_workflows_dir(path: str, seen_ids: dict, issues: list) -> list[str]:
    root = Path(path)
    if not root.is_dir():
        issues.append(ValidationIssue(path, "<dir>", "workflows directory not found"))
        return []
    loaded = []
    for fp in sorted(root.glob("*.y*ml")):
        loaded.extend(_validate_workflow_file(fp, seen_ids, issues))
    return loaded


def _validate_plan(path: str, seen_ids: dict, issues: list) -> list[str]:
    """Dry-load an ingest plan (single file or directory) and validate every job.

    Reuses :mod:`ujin.jobs.plan` (``defaults:`` deep-merge + ``matrix:`` fan-out
    + ``${VAR}`` expansion) so a job is checked the same way
    :func:`ujin.jobs.app.create_jobs_app` would load it via ``--plan``/
    ``$UJIN_INGEST_PLAN``.
    """
    from .plan import PlanError, _load_plan_file, load_plan_dir

    p = Path(path)
    if not p.exists():
        issues.append(ValidationIssue(path, "<path>", "ingest plan not found"))
        return []

    if p.is_dir():
        jobs, failures = load_plan_dir(p)
        for failure in failures:
            issues.append(ValidationIssue(f"{path}/{failure['file']}", "<file>", failure["error"]))
    else:
        try:
            jobs = _load_plan_file(p)
        except PlanError as exc:
            issues.append(ValidationIssue(path, "<file>", str(exc)))
            return []

    loaded = []
    for i, job in enumerate(jobs):
        label = job.get("id") if isinstance(job, dict) else None
        job_id = _validate_entry(job, path, label or f"job #{i}", seen_ids, issues)
        if job_id is not None:
            loaded.append(job_id)
    return loaded


def validate(
    jobs_path: str | None = None,
    *,
    workflows_dir: str | None = None,
    plan_path: str | None = None,
) -> ValidationResult:
    """Dry-load *jobs_path*/*workflows_dir*/*plan_path*, reporting every problem.

    Reuses the same yaml shape, ``defaults:``/``include:``/``use:`` resolution,
    ``matrix:``/``for_each:`` fan-out, and ``${VAR}`` expansion rules as the
    running server (:mod:`ujin.jobs.app`, :mod:`ujin.jobs.plan`), but never
    starts anything and never fails fast on the first bad entry — every job in
    every source is checked (except a malformed ``matrix:`` fan-out, which the
    loader itself can only isolate at the whole-file/whole-entry level).
    """
    issues: list[ValidationIssue] = []
    seen_ids: dict[str, str] = {}
    loaded: list[str] = []

    if jobs_path:
        loaded.extend(_validate_jobs_file(jobs_path, seen_ids, issues))
    if workflows_dir:
        loaded.extend(_validate_workflows_dir(workflows_dir, seen_ids, issues))
    if plan_path:
        loaded.extend(_validate_plan(plan_path, seen_ids, issues))

    return ValidationResult(loaded=loaded, issues=issues)
