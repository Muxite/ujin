"""Example ujin plugin: a custom sink + a custom source.

Drop this file into the directory ujin scans for plugins (``UJIN_PLUGINS_DIR``,
default ``/plugins``), then `POST /plugins/reload` (or restart). The kinds below
become usable in any job as ``plugin:hello`` / ``plugin:ticker``.

This runs in-process with no sandbox — only mount code you trust.
"""
from __future__ import annotations

import logging

from ujin import register
from ujin.poll.base import PollResult

log = logging.getLogger("ujin.plugins.hello")


@register.sink("hello")
def make_hello_sink(cfg: dict):
    """A sink that logs a greeting for every event.

    config: greeting (default "hello")
    """
    greeting = cfg.get("greeting", "hello")

    class _HelloSink:
        async def emit(self, event: dict) -> None:
            log.info("%s — job %s changed (fp=%s)",
                     greeting, event.get("job_id"), event.get("fingerprint"))

    return _HelloSink()


@register.source("ticker")
def make_ticker(cfg: dict):
    """A trivial source that emits an incrementing counter each poll.

    config: key (default "ticker")
    """
    key = cfg.get("key", "ticker")

    class _Ticker:
        def __init__(self) -> None:
            self.key = key
            self._n = 0

        async def poll(self, prev: PollResult | None) -> PollResult:
            self._n += 1
            return PollResult(ok=True, changed=True, fingerprint=str(self._n),
                              payload={"tick": self._n})

    return _Ticker()
