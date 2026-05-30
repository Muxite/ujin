"""eujin — adaptive multi-role poller + shared web-scraping toolkit.

Poll anything (HTTP pages, RSS, JSON APIs, shell commands, Python callables) on
an adaptive cadence with jitter so aggregate load stays smooth instead of spiky.

Top-level API::

    from eujin import PollEngine, HttpPollable, CallablePollable
    engine = PollEngine()
    engine.add(CallablePollable(my_fn, key="job"), base=30, on_change=cb)
    await engine.run()          # long-running daemon
    results = await engine.sweep()  # one pass (cron-friendly)

The scrape toolkit (``eujin.fetch``, ``eujin.extract``, ``eujin.cache``,
``eujin.sources``) remains available for direct use.
"""

__version__ = "0.2.0"

# Lightweight, dependency-free re-exports. Pollables that need optional deps
# (aiohttp, feedparser) import them lazily inside poll().
from eujin.poll.base import Pollable, PollResult, fingerprint
from eujin.poll.callable import CallablePollable
from eujin.poll.command import CommandPollable

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
        from eujin.engine import PollEngine

        return PollEngine
    if name == "HttpPollable":
        from eujin.poll.http import HttpPollable

        return HttpPollable
    if name == "RssPollable":
        from eujin.poll.rss import RssPollable

        return RssPollable
    if name == "ApiPollable":
        from eujin.poll.api import ApiPollable

        return ApiPollable
    if name == "AdaptiveInterval":
        from eujin.adapt.interval import AdaptiveInterval

        return AdaptiveInterval
    raise AttributeError(f"module 'eujin' has no attribute {name!r}")
