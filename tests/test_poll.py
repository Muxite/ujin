"""Pollable roles (offline) + base fingerprint/change logic."""
from __future__ import annotations

from ujin.poll.base import PollResult, decide_changed, fingerprint
from ujin.poll.callable import CallablePollable
from ujin.poll.command import CommandPollable


def test_fingerprint_stable_and_distinct():
    assert fingerprint("abc") == fingerprint("abc")
    assert fingerprint("abc") != fingerprint("abd")
    assert fingerprint({"a": 1, "b": 2}) == fingerprint({"b": 2, "a": 1})  # key order


def test_decide_changed():
    assert decide_changed("x", None) is True               # first time
    prev = PollResult(fingerprint="x")
    assert decide_changed("x", prev) is False
    assert decide_changed("y", prev) is True


async def test_callable_sync_change_then_stable():
    seq = iter([1, 1, 2])
    p = CallablePollable(lambda: next(seq), key="c")
    r1 = await p.poll(None)
    assert r1.ok and r1.changed and r1.payload == 1
    r2 = await p.poll(r1)
    assert not r2.changed                                   # same value
    r3 = await p.poll(r2)
    assert r3.changed and r3.payload == 2


async def test_callable_async_fn():
    async def fn():
        return "hello"

    p = CallablePollable(fn, key="a")
    r = await p.poll(None)
    assert r.ok and r.payload == "hello"


async def test_callable_captures_errors():
    def boom():
        raise ValueError("nope")

    r = await CallablePollable(boom, key="b").poll(None)
    assert not r.ok and "nope" in r.error


async def test_command_change_detection():
    p = CommandPollable(["printf", "hello"], key="cmd")
    r1 = await p.poll(None)
    assert r1.ok and r1.changed and r1.payload == "hello"
    r2 = await p.poll(r1)
    assert not r2.changed                                   # same output


async def test_command_nonzero_exit_is_failure():
    r = await CommandPollable(["sh", "-c", "exit 3"]).poll(None)
    assert not r.ok and r.status == 3


async def test_command_missing_binary():
    r = await CommandPollable(["this-binary-does-not-exist-xyz"]).poll(None)
    assert not r.ok
