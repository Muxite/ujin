"""Offline unit tests for ujin.poll.multi.MultiPollable."""
from __future__ import annotations

import pytest

from ujin.poll.base import PollResult, fingerprint
from ujin.poll.multi import MultiPollable


# --------------------------------------------------------------------------- #
# Fake child pollable
# --------------------------------------------------------------------------- #
class FakeChild:
    def __init__(self, key: str, payload=None, *, ok: bool = True, exc=None):
        self.key = key
        self._payload = payload
        self._ok = ok
        self._exc = exc

    async def poll(self, prev):
        if self._exc is not None:
            raise self._exc
        fp = fingerprint(self._payload)
        return PollResult(ok=self._ok, changed=True, fingerprint=fp, payload=self._payload)


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
async def test_empty_children_returns_ok_empty_list():
    mp = MultiPollable([])
    result = await mp.poll(None)
    assert result.ok is True
    assert result.payload == []


async def test_single_child_list_payload_extended():
    child = FakeChild("a", payload=[1, 2, 3])
    mp = MultiPollable([child], key="test")
    result = await mp.poll(None)
    assert result.ok is True
    assert result.payload == [1, 2, 3]


async def test_multiple_children_lists_concatenated():
    c1 = FakeChild("c1", payload=["x", "y"])
    c2 = FakeChild("c2", payload=["z"])
    mp = MultiPollable([c1, c2])
    result = await mp.poll(None)
    assert result.payload == ["x", "y", "z"]


async def test_non_list_payload_appended():
    child = FakeChild("a", payload="hello")
    mp = MultiPollable([child])
    result = await mp.poll(None)
    assert result.payload == ["hello"]


async def test_none_payload_not_appended():
    child = FakeChild("a", payload=None)
    mp = MultiPollable([child])
    result = await mp.poll(None)
    assert result.payload == []


async def test_failed_child_exception_skipped():
    bad = FakeChild("bad", exc=RuntimeError("boom"))
    good = FakeChild("good", payload=[42])
    mp = MultiPollable([bad, good])
    result = await mp.poll(None)
    assert result.ok is True
    assert result.payload == [42]


async def test_child_not_ok_skipped():
    not_ok = FakeChild("n", payload=[1], ok=False)
    ok_child = FakeChild("o", payload=[2])
    mp = MultiPollable([not_ok, ok_child])
    result = await mp.poll(None)
    assert result.payload == [2]


async def test_all_children_fail_returns_not_ok():
    bad1 = FakeChild("b1", exc=ValueError("err1"))
    bad2 = FakeChild("b2", ok=False, payload=[99])
    mp = MultiPollable([bad1, bad2])
    result = await mp.poll(None)
    assert result.ok is False
    assert result.payload == []


async def test_changed_on_first_poll():
    child = FakeChild("a", payload=[1])
    mp = MultiPollable([child])
    result = await mp.poll(None)
    assert result.changed is True


async def test_not_changed_when_fingerprint_same():
    child = FakeChild("a", payload=[1])
    mp = MultiPollable([child])
    r1 = await mp.poll(None)
    r2 = await mp.poll(r1)
    assert r2.changed is False


async def test_changed_when_payload_differs():
    class MutableChild:
        key = "mut"
        seq = iter([[1], [2]])

        async def poll(self, prev):
            p = next(self.seq)
            fp = fingerprint(p)
            return PollResult(ok=True, changed=True, fingerprint=fp, payload=p)

    mp = MultiPollable([MutableChild()])
    r1 = await mp.poll(None)
    r2 = await mp.poll(r1)
    assert r2.changed is True
    assert r2.payload == [2]


async def test_key_default():
    mp = MultiPollable([])
    assert mp.key == "multi"


async def test_key_custom():
    mp = MultiPollable([], key="mykey")
    assert mp.key == "mykey"


async def test_mixed_list_and_scalar_payloads():
    c1 = FakeChild("a", payload=["a", "b"])
    c2 = FakeChild("b", payload="scalar")
    c3 = FakeChild("c", payload=[3])
    mp = MultiPollable([c1, c2, c3])
    result = await mp.poll(None)
    assert result.payload == ["a", "b", "scalar", 3]


async def test_fingerprint_is_set_on_result():
    child = FakeChild("a", payload=["item"])
    mp = MultiPollable([child])
    result = await mp.poll(None)
    assert result.fingerprint == fingerprint(["item"])
