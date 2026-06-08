"""JobManager — translate JobSpecs into engine registrations and drive them.

One manager owns a :class:`ujin.engine.PollEngine`, a :class:`JobStore`, a
broadcast hub, and (optionally) a shared :class:`ScrapeService` backing ``scrape``
sources. Each job becomes:

    source pollable  +  Pipeline(transforms -> sinks)  +  a Target

For ``adaptive`` jobs the Target is registered with the engine, whose ``run()``
loop drives it. For ``cron``/``once``/run-now the manager holds a standalone
Target and drives it through :meth:`PollEngine.poll_once`, so the same global
TokenBucket + concurrency gate still applies.

Source/sink/transform kinds are resolved here from the built-in factories; the
plugin registry (M11) extends those maps with ``plugin:*`` kinds.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from ujin.adapt.interval import AdaptiveInterval
from ujin.engine import PollEngine, Target
from ujin.poll.base import PollResult
from ujin.registry import BuildContext, register

from .model import JobSpec
from .pipeline import Pipeline
from .store import JobStore

log = logging.getLogger("ujin.jobs.manager")


class UnknownKind(ValueError):
    """A spec referenced a source/transform/sink kind that isn't registered."""


@dataclass
class JobHandle:
    spec: JobSpec
    target: Target
    pipeline: Pipeline
    adaptive: bool
    next_fire: float = 0.0  # cron jobs only
    last_error: Optional[str] = None
    state: str = "idle"  # idle | running | paused | done | error

    def summary(self) -> dict[str, Any]:
        t = self.target
        return {
            "id": self.spec.id,
            "name": self.spec.name,
            "state": "paused" if not self.spec.enabled else self.state,
            "enabled": self.spec.enabled,
            "schedule": self.spec.schedule.mode,
            "interval": round(t.interval.current, 2),
            "polls": t.polls,
            "changes": t.changes,
            "circuit": t.breaker.state,
            "last_fingerprint": t.prev.fingerprint if t.prev else None,
            "last_error": self.last_error,
        }


class JobManager:
    def __init__(
        self,
        engine: PollEngine,
        store: JobStore,
        *,
        hub: Any = None,
        scrape_service: Any = None,
    ):
        self.engine = engine
        self.store = store
        self.hub = hub
        self.scrape_service = scrape_service
        self.jobs: dict[str, JobHandle] = {}

    @property
    def _ctx(self) -> BuildContext:
        return BuildContext(scrape_service=self.scrape_service, hub=self.hub,
                            store=self.store)

    # -- spec -> live job -------------------------------------------------- #
    def _build_pipeline(self, spec: JobSpec) -> Pipeline:
        ctx = self._ctx
        transforms = [register.build_transform(t.kind, t.config, ctx)
                      for t in spec.transforms]
        sinks = [register.build_sink(s.kind, s.config, ctx) for s in spec.sinks]
        return Pipeline(transforms, sinks)

    def _wrap_on_change(self, spec: JobSpec, pipeline: Pipeline):
        """on_change that records a (changed) run, then runs the pipeline."""

        async def _cb(key: str, result: PollResult) -> None:
            ts = getattr(result, "ts", time.time())
            self.store.record_run(
                spec.id,
                started_at=ts,
                finished_at=time.time(),
                ok=result.ok,
                changed=result.changed,
                fingerprint=result.fingerprint,
                strategy=getattr(getattr(result, "payload", None), "strategy_used", None),
            )
            # Capture the obtained payload into the durable "collect" buffer so
            # consumers can pull it back by workflow id via /jobs/{id}/results.
            # on_change fires only when content actually changed, so we don't
            # store identical bodies on every poll.
            self.store.record_result(
                spec.id, ts=ts, fingerprint=result.fingerprint,
                payload=getattr(result, "payload", None),
            )
            # compact notice on the global /jobs/events stream (independent of
            # whether the job also configured a `ws` sink)
            if self.hub is not None:
                await self.hub.broadcast_event({
                    "event": "change", "job_id": key,
                    "name": spec.name, "fingerprint": result.fingerprint,
                    "ts": getattr(result, "ts", time.time()),
                })
            await pipeline(key, result)

        return _cb

    def register(self, spec: JobSpec) -> JobHandle:
        """Validate + wire a job. Raises :class:`UnknownKind` on a bad kind."""
        try:
            pollable = register.build_source(spec.source.kind, spec.source.config,
                                             self._ctx)
            pipeline = self._build_pipeline(spec)
        except KeyError as exc:
            raise UnknownKind(str(exc).strip('"')) from None
        pollable.key = spec.id  # the target key is the job id
        on_change = self._wrap_on_change(spec, pipeline)
        sch = spec.schedule
        adaptive = sch.mode == "adaptive"

        if adaptive and spec.enabled:
            target = self.engine.add(
                pollable, base=sch.base, min_interval=sch.min, max_interval=sch.max,
                grow=sch.grow, shrink=sch.shrink, jitter=sch.jitter,
                on_change=on_change,
            )
        else:
            # standalone target (cron/once/paused): not in engine.targets, driven
            # by the manager via engine.poll_once.
            target = Target(
                pollable=pollable,
                interval=AdaptiveInterval(base=sch.base, min_interval=sch.min,
                                          max_interval=sch.max, grow=sch.grow,
                                          shrink=sch.shrink),
                jitter=sch.jitter,
                on_change=on_change,
            )

        handle = JobHandle(spec=spec, target=target, pipeline=pipeline,
                           adaptive=adaptive)
        if sch.mode == "cron" and spec.enabled:
            from .cron import next_fire

            handle.next_fire = next_fire(sch.cron or "* * * * *", now=self.engine.clock())
        self.jobs[spec.id] = handle
        return handle

    # -- CRUD -------------------------------------------------------------- #
    def create(self, spec: JobSpec) -> JobHandle:
        handle = self.register(spec)  # validates kinds before we persist
        self.store.upsert(spec)
        return handle

    def get(self, job_id: str) -> JobHandle | None:
        return self.jobs.get(job_id)

    def list(self) -> list[dict[str, Any]]:
        return [h.summary() for h in self.jobs.values()]

    def delete(self, job_id: str) -> bool:
        handle = self.jobs.pop(job_id, None)
        if handle is None:
            return False
        self.engine.targets.pop(job_id, None)
        self.store.delete(job_id)
        return True

    def pause(self, job_id: str) -> bool:
        handle = self.jobs.get(job_id)
        if handle is None:
            return False
        handle.spec.enabled = False
        handle.state = "paused"
        self.engine.targets.pop(job_id, None)  # stop the adaptive loop driving it
        self.store.set_enabled(job_id, False)
        return True

    def resume(self, job_id: str) -> bool:
        handle = self.jobs.get(job_id)
        if handle is None:
            return False
        handle.spec.enabled = True
        handle.state = "idle"
        self.store.set_enabled(job_id, True)
        if handle.adaptive:
            # re-arm in the engine; reuse the existing target (keeps prev/counters)
            handle.target.next_due = self.engine.clock()
            self.engine.targets[job_id] = handle.target
        elif handle.spec.schedule.mode == "cron":
            from .cron import next_fire

            handle.next_fire = next_fire(handle.spec.schedule.cron or "* * * * *",
                                         now=self.engine.clock())
        return True

    async def run_now(self, job_id: str) -> PollResult | None:
        handle = self.jobs.get(job_id)
        if handle is None:
            return None
        handle.state = "running"
        try:
            result = await self.engine.poll_once(handle.target)
        finally:
            handle.state = "idle"
        if not result.ok:
            handle.last_error = result.error
            handle.state = "error"
        return result

    # -- cron loop --------------------------------------------------------- #
    async def cron_loop(
        self, stop: asyncio.Event | None = None, *, max_ticks: int | None = None
    ) -> None:
        """Drive cron jobs. Uses the engine's clock/sleep for testability."""
        clock, sleep = self.engine.clock, self.engine.sleep
        ticks = 0
        while stop is None or not stop.is_set():
            now = clock()
            due = [
                h for h in self.jobs.values()
                if h.spec.enabled and h.spec.schedule.mode == "cron"
                and h.next_fire <= now and h.target.breaker.allow()
            ]
            from .cron import next_fire

            for h in due:
                await self.engine.poll_once(h.target)
                h.next_fire = next_fire(h.spec.schedule.cron or "* * * * *", now=clock())

            upcoming = [
                h.next_fire for h in self.jobs.values()
                if h.spec.enabled and h.spec.schedule.mode == "cron"
            ]
            nxt = min(upcoming, default=now + 30.0)
            await sleep(max(1.0, nxt - clock()))
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                return

    # -- startup reload ---------------------------------------------------- #
    def load_from_store(self) -> None:
        """Rebuild every persisted job. One bad spec is logged, not fatal."""
        for spec in self.store.list():
            try:
                self.register(spec)
                # a persisted `once` job is considered already-fired; don't re-run
                if spec.schedule.mode == "once":
                    self.jobs[spec.id].state = "done"
            except Exception as exc:  # noqa: BLE001
                log.warning("skipping job %s (%s): %s", spec.id, spec.name, exc)
