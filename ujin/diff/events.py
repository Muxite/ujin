"""Change-event sinks — deliver site-change events to a callback or webhook.

Sinks are async callables with the engine's ``on_change(key, result)`` shape,
so they drop straight into ``PollEngine.add(..., on_change=sink)`` or a
``Target.on_change`` slot. They read the :class:`RegionDiff` the SitePollable
attaches to ``result.payload['region_diff']``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

log = logging.getLogger("ujin.diff.events")


@dataclass
class ChangeEvent:
    key: str
    fingerprint: str | None
    regions: dict[str, list[str]] = field(default_factory=dict)
    ts: float = 0.0

    @classmethod
    def from_result(cls, key: str, result: Any) -> "ChangeEvent":
        payload = result.payload if isinstance(getattr(result, "payload", None), dict) else {}
        diff = payload.get("region_diff")
        regions = diff.as_dict() if diff is not None and hasattr(diff, "as_dict") else {}
        return cls(
            key=key,
            fingerprint=getattr(result, "fingerprint", None),
            regions=regions,
            ts=getattr(result, "ts", 0.0),
        )


class CallbackSink:
    """Wrap a user callback ``(ChangeEvent) -> None | Awaitable`` as an on_change."""

    def __init__(self, cb: Callable[[ChangeEvent], Any | Awaitable[Any]]):
        self._cb = cb

    async def __call__(self, key: str, result: Any) -> None:
        import asyncio

        event = ChangeEvent.from_result(key, result)
        ret = self._cb(event)
        if asyncio.iscoroutine(ret):
            await ret


class WebhookSink:
    """POST the change event as JSON to a webhook URL (aiohttp, lazy import)."""

    def __init__(self, url: str, *, timeout_secs: int = 10):
        self._url = url
        self._timeout = timeout_secs

    async def __call__(self, key: str, result: Any) -> None:
        import aiohttp

        event = ChangeEvent.from_result(key, result)
        body = {
            "key": event.key,
            "fingerprint": event.fingerprint,
            "regions": event.regions,
            "ts": event.ts,
        }
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(self._url, json=body) as resp:
                    if resp.status >= 400:
                        log.warning("webhook %s -> HTTP %s", self._url, resp.status)
        except Exception as exc:  # noqa: BLE001
            log.warning("webhook POST to %s failed: %s", self._url, exc)
