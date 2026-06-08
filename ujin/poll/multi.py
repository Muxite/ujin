"""MultiPollable — fan one workflow over several child pollables.

Polls each child once per ``poll()`` and concatenates their **list** payloads
into a single list (non-list payloads are appended as-is). Lets one workflow
file sweep many similar targets — e.g. several Amazon search terms — while the
downstream ``select``/``dedupe`` transforms and sinks see one combined batch.

Children are polled concurrently; a child that fails contributes nothing rather
than sinking the whole poll. ``changed`` is true when the combined fingerprint
differs from the previous poll.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from ujin.poll.base import PollResult, decide_changed, fingerprint

log = logging.getLogger("ujin.poll.multi")


class MultiPollable:
    """Aggregate several child pollables into one combined list payload."""

    def __init__(self, children: list[Any], *, key: str = "multi") -> None:
        self.children = list(children)
        self.key = key

    async def poll(self, prev: PollResult | None) -> PollResult:
        results = await asyncio.gather(
            *(c.poll(None) for c in self.children), return_exceptions=True
        )
        combined: list[Any] = []
        ok_any = False
        for child, res in zip(self.children, results):
            if isinstance(res, Exception):
                log.warning("child %s failed: %s", getattr(child, "key", "?"), res)
                continue
            if not getattr(res, "ok", False):
                continue
            ok_any = True
            payload = res.payload
            if isinstance(payload, list):
                combined.extend(payload)
            elif payload is not None:
                combined.append(payload)
        fp = fingerprint(combined)
        return PollResult(
            ok=ok_any or not self.children,
            changed=decide_changed(fp, prev),
            fingerprint=fp,
            payload=combined,
        )
