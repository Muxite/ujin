"""ujin — adaptive multi-role poller + shared web-scraping toolkit.

Poll anything (HTTP pages, RSS, JSON APIs, shell commands, Python callables) on
an adaptive cadence with jitter so aggregate load stays smooth instead of spiky.

Top-level API::

    from ujin import PollEngine, HttpPollable, CallablePollable
    engine = PollEngine()
    engine.add(CallablePollable(my_fn, key="job"), base=30, on_change=cb)
    await engine.run()          # long-running daemon
    results = await engine.sweep()  # one pass (cron-friendly)

The scrape toolkit (``ujin.fetch``, ``ujin.extract``, ``ujin.cache``,
``ujin.sources``) remains available for direct use.
"""

__version__ = "0.3.0"

# Lightweight, dependency-free re-exports. Pollables that need optional deps
# (aiohttp, feedparser) import them lazily inside poll().
from ujin.poll.base import Pollable, PollResult, fingerprint
from ujin.poll.callable import CallablePollable
from ujin.poll.command import CommandPollable

__all__ = [
    "PollResult",
    "Pollable",
    "fingerprint",
    "CallablePollable",
    "CommandPollable",
    "PollEngine",
    "HttpPollable",
    "RssPollable",
    "ApiPollable",
    "AdaptiveInterval",
    "__version__",
]


def __getattr__(name: str):
    # Lazy attribute access for symbols whose modules pull optional deps.
    if name == "PollEngine":
        from ujin.engine import PollEngine

        return PollEngine
    if name == "HttpPollable":
        from ujin.poll.http import HttpPollable

        return HttpPollable
    if name == "RssPollable":
        from ujin.poll.rss import RssPollable

        return RssPollable
    if name == "ApiPollable":
        from ujin.poll.api import ApiPollable

        return ApiPollable
    if name == "AdaptiveInterval":
        from ujin.adapt.interval import AdaptiveInterval

        return AdaptiveInterval
    raise AttributeError(f"module 'ujin' has no attribute {name!r}")
