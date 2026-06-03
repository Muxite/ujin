"""ujin jobs — the unified job control plane.

A *job* is the single configurable unit ujin exposes over REST/WS::

    source  ->  transforms  ->  sinks      (on a schedule)

The ``source`` is any pollable role (http/rss/api/site/command/scrape or a
plugin); ``transforms`` filter/reshape each change event; ``sinks`` fan the
result out (webhook/file/ws/...). The whole thing rides on the existing
:class:`ujin.engine.PollEngine`, so adaptive cadence, jitter, backoff, circuit
breaking, and global rate smoothing all come for free.

This package layer is intentionally split:
- :mod:`ujin.jobs.model` — pure dataclass specs (JSON-able, dependency-free).
- :mod:`ujin.jobs.store` — sqlite persistence (stdlib only).
The service layer (manager/scheduler/pipeline/app) is added in M10.
"""
from __future__ import annotations

from .model import (
    JobSpec,
    JobState,
    ScheduleSpec,
    SinkSpec,
    SourceSpec,
    TransformSpec,
)
from .store import JobStore

__all__ = [
    "JobSpec",
    "JobState",
    "ScheduleSpec",
    "SinkSpec",
    "SourceSpec",
    "TransformSpec",
    "JobStore",
]
