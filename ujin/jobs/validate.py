"""Offline validation of jobs/workflow config — no server, no store, no I/O.

Mirrors the exact yaml-shape and ``${VAR}``/``${VAR:-default}`` substitution
rules that :mod:`ujin.jobs.app` uses at startup (``_preload_specs`` for a
single jobs file, ``_specs_from_workflow_file``/``_load_workflows_dir`` for a
workflows directory), but walks entries **one at a time** so a single bad job
doesn't hide problems in the rest of the file, and reports every problem with
the offending source, the job, and the underlying error rather than crashing
or silently skipping.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .app import _expand_env

_JOBS_FILE_KEYS = {"jobs"}


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

    if isinstance(data, dict):
        unknown = set(data) - _JOBS_FILE_KEYS
        if unknown:
            issues.append(ValidationIssue(
                path, "<file>",
                f"unknown top-level key(s): {', '.join(sorted(unknown))} (expected only 'jobs')",
            ))
        raw = data.get("jobs", [])
    else:
        raw = data

    loaded = []
    for i, entry in enumerate(raw or []):
        job_id = _validate_entry(entry, path, f"job #{i}", seen_ids, issues)
        if job_id is not None:
            loaded.append(job_id)
    return loaded


def _validate_workflow_file(path: Path, seen_ids: dict, issues: list) -> list[str]:
    import yaml

    stem = path.stem
    try:
        data = yaml.safe_load(_expand_env(open(path, encoding="utf-8").read())) or {}
    except OSError as exc:
        issues.append(ValidationIssue(str(path), "<file>", f"could not read file: {exc}"))
        return []
    except yaml.YAMLError as exc:
        issues.append(ValidationIssue(str(path), "<file>", f"invalid YAML: {exc}"))
        return []

    if isinstance(data, dict) and "jobs" not in data:
        d = dict(data)
        d.setdefault("id", stem)
        d.setdefault("name", stem)
        job_id = _validate_entry(d, str(path), stem, seen_ids, issues)
        return [job_id] if job_id is not None else []

    raw = data.get("jobs", data) if isinstance(data, dict) else data
    raw = raw or []
    loaded = []
    for i, entry in enumerate(raw):
        label = stem if len(raw) == 1 else f"{stem}-{i}"
        if isinstance(entry, dict):
            entry = dict(entry)
            entry.setdefault("id", label)
            entry.setdefault("name", entry["id"])
        job_id = _validate_entry(entry, str(path), label, seen_ids, issues)
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


def validate(jobs_path: str | None = None, *, workflows_dir: str | None = None) -> ValidationResult:
    """Dry-load *jobs_path* and/or *workflows_dir*, reporting every problem found.

    Reuses the same yaml shape and ``${VAR}`` expansion rules as the running
    server (:mod:`ujin.jobs.app`), but never starts anything and never fails
    fast on the first bad entry — every job in every source is checked.
    """
    issues: list[ValidationIssue] = []
    seen_ids: dict[str, str] = {}
    loaded: list[str] = []

    if jobs_path:
        loaded.extend(_validate_jobs_file(jobs_path, seen_ids, issues))
    if workflows_dir:
        loaded.extend(_validate_workflows_dir(workflows_dir, seen_ids, issues))

    return ValidationResult(loaded=loaded, issues=issues)
