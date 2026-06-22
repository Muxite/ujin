"""Job specs — pure, JSON-able dataclasses (no FastAPI/Pydantic here).

Keeping these dependency-free means the core (and the persistence layer) never
pulls the ``service`` extra. The Pydantic request/response wrappers live in
:mod:`ujin.jobs.api_models`, added with the service in M10.

A :class:`JobSpec` is the whole declarative job:

    source       one SourceSpec   (kind + config)
    transforms   list[TransformSpec]
    sinks        list[SinkSpec]
    schedule     ScheduleSpec      (adaptive | cron | once)

``kind`` strings may be a built-in name (``http``/``api``/``select``/``webhook``
…) or a plugin reference (``plugin:my_source``), resolved through the registry
(M11).
"""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class SourceSpec:
    """What to poll. ``kind`` in http|rss|api|command|site|scrape|browser|plugin:*."""

    kind: str
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class TransformSpec:
    """One pipeline step. ``kind`` in
    select|regex|template|dedupe|chunk|flatten|sort|limit|rename|plugin:*."""

    kind: str
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class SinkSpec:
    """One output. ``kind`` in
    webhook|forward|ws|jsonl|file|stdout|sqlite|csv|plugin:*."""

    kind: str
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScheduleSpec:
    """When the source fires.

    ``adaptive`` (default) registers the job as an engine target whose interval
    grows when nothing changes and shrinks on change (knobs mirror
    :meth:`ujin.engine.PollEngine.add`). ``cron`` fires on a 5-field schedule.
    ``once`` runs a single time and does not reschedule.
    """

    mode: str = "adaptive"  # adaptive | cron | once
    base: float = 60.0
    min: float = 5.0
    max: float = 3600.0
    grow: float = 1.6
    shrink: float = 0.4
    jitter: str = "decorrelated"
    cron: str | None = None  # "*/5 * * * *" when mode == "cron"


@dataclass
class JobSpec:
    """A complete declarative job."""

    name: str
    source: SourceSpec
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    transforms: list[TransformSpec] = field(default_factory=list)
    sinks: list[SinkSpec] = field(default_factory=list)
    schedule: ScheduleSpec = field(default_factory=ScheduleSpec)
    enabled: bool = True
    created_at: float = field(default_factory=time.time)

    # -- (de)serialization ------------------------------------------------- #
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JobSpec":
        """Build a JobSpec from a plain dict (nested specs coerced)."""
        data = dict(data)
        src = data.get("source") or {}
        source = src if isinstance(src, SourceSpec) else SourceSpec(**src)
        transforms = [
            t if isinstance(t, TransformSpec) else TransformSpec(**t)
            for t in data.get("transforms", [])
        ]
        sinks = [
            s if isinstance(s, SinkSpec) else SinkSpec(**s)
            for s in data.get("sinks", [])
        ]
        sched = data.get("schedule") or {}
        schedule = sched if isinstance(sched, ScheduleSpec) else ScheduleSpec(**sched)
        return cls(
            name=data["name"],
            source=source,
            id=data.get("id") or uuid.uuid4().hex,
            transforms=transforms,
            sinks=sinks,
            schedule=schedule,
            enabled=data.get("enabled", True),
            created_at=data.get("created_at", time.time()),
        )


@dataclass
class JobState:
    """Runtime view of a job (not persisted as part of the spec)."""

    spec: JobSpec
    state: str = "idle"  # idle | running | paused | error | done
    last_run_ts: float = 0.0
    last_fingerprint: str | None = None
    runs: int = 0
    changes: int = 0
    last_error: str | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.spec.id,
            "name": self.spec.name,
            "state": self.state,
            "enabled": self.spec.enabled,
            "schedule": self.spec.schedule.mode,
            "runs": self.runs,
            "changes": self.changes,
            "last_run_ts": self.last_run_ts,
            "last_fingerprint": self.last_fingerprint,
            "last_error": self.last_error,
        }
