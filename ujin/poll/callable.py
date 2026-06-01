"""CallablePollable — poll any Python function. The most general role.

Wrap any sync or async callable; ujin fingerprints its return value to detect
change. This is what makes ujin "poll ANYTHING": a DB row count, a file's mtime,
a queue depth, an in-process metric — anything you can express as a function.
"""
from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any, Awaitable, Callable

from ujin.poll.base import PollResult, decide_changed, fingerprint


class CallablePollable:
    def __init__(
        self,
        fn: Callable[[], Any | Awaitable[Any]],
        *,
        key: str,
        fingerprint_fn: Callable[[Any], str] | None = None,
    ) -> None:
        self.fn = fn
        self.key = key
        self._fp = fingerprint_fn or fingerprint

    async def poll(self, prev: PollResult | None) -> PollResult:
        start = time.monotonic()
        try:
            result = self.fn()
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:  # noqa: BLE001
            return PollResult.failure(f"{type(exc).__name__}: {exc}")
        fp = self._fp(result)
        return PollResult(
            ok=True,
            changed=decide_changed(fp, prev),
            fingerprint=fp,
            payload=result,
            latency_ms=int((time.monotonic() - start) * 1000),
        )
