"""Pollable roles. Dependency-free roles (callable/command) import directly;
web roles (http/api/rss) pull optional deps lazily inside ``poll()``."""
from eujin.poll.base import Pollable, PollResult, decide_changed, fingerprint
from eujin.poll.callable import CallablePollable
from eujin.poll.command import CommandPollable

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
        from eujin.poll.http import HttpPollable

        return HttpPollable
    if name == "RssPollable":
        from eujin.poll.rss import RssPollable

        return RssPollable
    if name == "ApiPollable":
        from eujin.poll.api import ApiPollable

        return ApiPollable
    raise AttributeError(f"module 'eujin.poll' has no attribute {name!r}")
