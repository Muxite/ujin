"""Pollable roles. Dependency-free roles (callable/command) import directly;
web roles (http/api/rss) pull optional deps lazily inside ``poll()``."""
from ujin.poll.base import Pollable, PollResult, decide_changed, fingerprint
from ujin.poll.callable import CallablePollable
from ujin.poll.command import CommandPollable

__all__ = [
    "Pollable",
    "PollResult",
    "fingerprint",
    "decide_changed",
    "CallablePollable",
    "CommandPollable",
    "HttpPollable",
    "RssPollable",
    "ApiPollable",
]


def __getattr__(name: str):
    if name == "HttpPollable":
        from ujin.poll.http import HttpPollable

        return HttpPollable
    if name == "RssPollable":
        from ujin.poll.rss import RssPollable

        return RssPollable
    if name == "ApiPollable":
        from ujin.poll.api import ApiPollable

        return ApiPollable
    raise AttributeError(f"module 'ujin.poll' has no attribute {name!r}")
