"""The job pipeline: turn a change into an event, transform it, fan out to sinks.

A :class:`Pipeline` is constructed with already-built transform + sink instances
and is itself an ``on_change(key, result)`` callable, so it drops straight into
:meth:`ujin.engine.PollEngine.add(..., on_change=pipeline)` / a ``Target``.

Event shape (a plain JSON-able dict)::

    {
      "job_id": <key>, "fingerprint": ..., "ts": ..., "status": ...,
      "payload": <jsonable payload>,          # normalized from PollResult.payload
      "regions": {selector: [...]},           # for SitePollable region diffs
    }

Transforms run in order; one returning ``None`` drops the event. Sinks fan out
concurrently — one failing sink never blocks the others.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import Any, Protocol, runtime_checkable

from ujin.diff.events import ChangeEvent

log = logging.getLogger("ujin.jobs.pipeline")


@runtime_checkable
class Transform(Protocol):
    async def apply(self, event: dict) -> "dict | list[dict] | None": ...
    # Return one event (dict), several (list[dict] — fan-out), or None (drop).


@runtime_checkable
class Sink(Protocol):
    async def emit(self, event: dict) -> None: ...


def _jsonable(payload: Any) -> Any:
    """Best-effort normalize a PollResult.payload into JSON-able data.

    Dataclasses (e.g. ScrapeResult, NormalizedLink) recurse via ``asdict``;
    objects exposing ``to_dict`` use it; everything else is returned as-is and
    left for the sink's ``json.dumps(default=str)`` to stringify.
    """
    if payload is None or isinstance(payload, (str, int, float, bool, dict, list)):
        return payload
    if dataclasses.is_dataclass(payload) and not isinstance(payload, type):
        try:
            return dataclasses.asdict(payload)
        except Exception:  # noqa: BLE001
            return str(payload)
    to_dict = getattr(payload, "to_dict", None)
    if callable(to_dict):
        try:
            return to_dict()
        except Exception:  # noqa: BLE001
            return str(payload)
    return payload


def build_event(key: str, result: Any) -> dict:
    """Normalize a PollResult into the pipeline event dict."""
    regions = ChangeEvent.from_result(key, result).regions
    return {
        "job_id": key,
        "fingerprint": getattr(result, "fingerprint", None),
        "ts": getattr(result, "ts", 0.0),
        "status": getattr(result, "status", None),
        "payload": _jsonable(getattr(result, "payload", None)),
        "regions": regions,
    }


class Pipeline:
    """transforms -> sinks, wired as an engine ``on_change`` callable."""

    def __init__(self, transforms: list[Transform], sinks: list[Sink]):
        self._transforms = transforms
        self._sinks = sinks

    async def __call__(self, key: str, result: Any) -> None:
        # Carry a working set of events. A transform may return one event, a list
        # (fan-out, e.g. `chunk`), or None (drop); we flatten between stages.
        events: list[dict] = [build_event(key, result)]
        for t in self._transforms:
            next_events: list[dict] = []
            for ev in events:
                try:
                    out = await t.apply(ev)  # type: ignore[arg-type]
                except Exception:  # noqa: BLE001
                    log.exception("transform %s raised for job %s",
                                  type(t).__name__, key)
                    return
                if out is None:
                    continue  # this event dropped
                if isinstance(out, list):
                    next_events.extend(out)
                else:
                    next_events.append(out)
            events = next_events
            if not events:
                return  # everything dropped
        if not self._sinks:
            return
        for ev in events:  # each (possibly-chunked) event -> all sinks
            results = await asyncio.gather(
                *(s.emit(ev) for s in self._sinks), return_exceptions=True
            )
            for sink, res in zip(self._sinks, results):
                if isinstance(res, Exception):
                    log.warning("sink %s failed for job %s: %s",
                                type(sink).__name__, key, res)
