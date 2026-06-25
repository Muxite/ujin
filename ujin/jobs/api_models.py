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

    def to_spec(self) -> JobSpec:
        return JobSpec(
            name=self.name,
            source=SourceSpec(kind=self.source.kind, config=self.source.config),
            transforms=[TransformSpec(kind=t.kind, config=t.config)
                        for t in self.transforms],
            sinks=[SinkSpec(kind=s.kind, config=s.config) for s in self.sinks],
            schedule=ScheduleSpec(**self.schedule.model_dump()),
            enabled=self.enabled,
        )
