"""Pydantic request/response models for the jobs API.

These live in the service layer (not :mod:`ujin.jobs.model`) so the core job
dataclasses + store stay dependency-free. ``to_spec`` converts an incoming
:class:`JobCreate` into the internal :class:`ujin.jobs.model.JobSpec`.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .model import JobSpec, ScheduleSpec, SinkSpec, SourceSpec, TransformSpec


class SpecItem(BaseModel):
    kind: str
    config: dict[str, Any] = Field(default_factory=dict)


class Schedule(BaseModel):
    mode: str = "adaptive"  # adaptive | cron | once
    base: float = 60.0
    min: float = 5.0
    max: float = 3600.0
    grow: float = 1.6
    shrink: float = 0.4
    jitter: str = "decorrelated"
    cron: str | None = None


class JobCreate(BaseModel):
    name: str
    source: SpecItem
    transforms: list[SpecItem] = Field(default_factory=list)
    sinks: list[SpecItem] = Field(default_factory=list)
    schedule: Schedule = Field(default_factory=Schedule)
    enabled: bool = True
    tags: list[str] = Field(default_factory=list)

    def to_spec(self) -> JobSpec:
        return JobSpec(
            name=self.name,
            source=SourceSpec(kind=self.source.kind, config=self.source.config),
            transforms=[TransformSpec(kind=t.kind, config=t.config)
                        for t in self.transforms],
            sinks=[SinkSpec(kind=s.kind, config=s.config) for s in self.sinks],
            schedule=ScheduleSpec(**self.schedule.model_dump()),
            enabled=self.enabled,
            tags=list(self.tags),
        )


# ── plan/workflows introspection (read-only `GET /plan`) ───────────────────── #
#
# Response shape for ``GET /plan``: the jobs the active workflows dir + INGEST-PLAN
# currently resolve to, each with the provenance ``plan render --explain`` produces
# (originating source file, plus the matrix entry for fanned-out jobs). Mirrors the
# ``{"id", "provenance"}`` pairing of ``plan render --explain --json``.


class MatrixProvenance(BaseModel):
    """The ``matrix:``/``for_each:`` entry that produced a fanned-out job."""

    key: str  # the matrix key used ("matrix" or "for_each")
    index: int  # 0-based position of the row in the matrix list
    vars: dict[str, Any] = Field(default_factory=dict)  # the variable map substituted in


class JobProvenance(BaseModel):
    """Where a resolved job came from: its source file, and matrix entry if any."""

    source: str  # the plan/workflow file the job was resolved from
    matrix: MatrixProvenance | None = None  # present only for matrix-expanded jobs


class PlanJob(BaseModel):
    """One resolved job id paired with its provenance and its declared tags."""

    id: str
    provenance: JobProvenance
    tags: list[str] = Field(default_factory=list)  # the job's resolved `tags:` (post fan-out/merge)


class PlanFailure(BaseModel):
    """A plan/workflow file or job that failed to resolve (mirrors ``GET /health``)."""

    id: str
    error: str


class PlanIntrospection(BaseModel):
    """Read-only view of what the active plan / workflows directory resolves to.

    ``plan_path`` is the configured INGEST-PLAN file (``null`` when none is set);
    ``workflows_dir`` is the configured workflows directory. ``jobs`` is the set of
    jobs currently resolved from both, each with provenance; ``failed`` lists any
    file/job that did not resolve, exactly as ``GET /health`` reports it.
    """

    plan_path: str | None = None
    workflows_dir: str | None = None
    jobs: list[PlanJob] = Field(default_factory=list)
    failed: list[PlanFailure] = Field(default_factory=list)
