"""Offline unit tests for MultiPollable (ujin/poll/multi.py).

Covers all public behaviour:
  - concurrent polling of multiple children
  - list payloads concatenated into one combined list
  - non-list payloads appended as-is
  - None payload not appended
  - failing child (exception) contributes nothing
  - not-ok child contributes nothing
  - all children fail -> ok=False
  - empty children -> ok=True, empty payload
  - changed detection (first poll / same fingerprint / different fingerprint)
"""
from __future__ import annotations

import pytest

from ujin.poll.base import PollResult, fingerprint
from ujin.poll.multi import MultiPollable


# ── helpers ──────────────────────────────────────────────────────────────────

class _OkChild:
    """Always returns ok=True with the configured payload."""

    def __init__(self, key: str, payload):
        self.key = key
        self._payload = payload

    async def poll(self, prev):
        return PollResult(ok=True, changed=True,
                          fingerprint=fingerprint(self._payload),
                          payload=self._payload)


class _FailChild:
    """poll() always raises RuntimeError."""

    def __init__(self, key: str = "bad"):
        self.key = key

    async def poll(self, prev):
        raise RuntimeError("network down")


class _NotOkChild:
    """poll() returns ok=False."""

    def __init__(self, key: str = "notok"):
        self.key = key

    async def poll(self, prev):
        return PollResult(ok=False, changed=False, fingerprint=None, payload=None)


# ── list payload concatenation ────────────────────────────────────────────────

async def test_multi_concatenates_list_payloads():
    m = MultiPollable([_OkChild("a", [1, 2]), _OkChild("b", [3, 4])])
    r = await m.poll(None)
    assert r.ok is True
    assert r.payload == [1, 2, 3, 4]


async def test_multi_single_child_list_passthrough():
    m = MultiPollable([_OkChild("x", ["alpha", "beta"])])
    r = await m.poll(None)
    assert r.payload == ["alpha", "beta"]


async def test_multi_three_children_concatenate_in_order():
    m = MultiPollable([
        _OkChild("a", ["x"]),
        _OkChild("b", ["y", "z"]),
        _OkChild("c", ["w"]),
    ])
    r = await m.poll(None)
    assert r.payload == ["x", "y", "z", "w"]


# ── non-list payloads ─────────────────────────────────────────────────────────

async def test_multi_scalar_payload_appended():
    m = MultiPollable([_OkChild("x", "scalar")])
    r = await m.poll(None)
    assert r.payload == ["scalar"]


async def test_multi_dict_payload_appended():
    m = MultiPollable([_OkChild("x", {"key": "val"})])
    r = await m.poll(None)
    assert r.payload == [{"key": "val"}]


async def test_multi_mixed_list_and_scalar():
    m = MultiPollable([_OkChild("a", [1, 2]), _OkChild("b", "three")])
    r = await m.poll(None)
    assert r.payload == [1, 2, "three"]


async def test_multi_none_payload_not_appended():
    class _NoneChild:
        key = "none"

        async def poll(self, prev):
            return PollResult(ok=True, changed=False, fingerprint=None, payload=None)

    m = MultiPollable([_NoneChild()])
    r = await m.poll(None)
    assert r.payload == []


# ── failing / not-ok children ────────────────────────────────────────────────

async def test_failing_child_does_not_sink_poll():
    m = MultiPollable([_FailChild("bad"), _OkChild("good", ["item"])])
    r = await m.poll(None)
    assert r.ok is True
    assert r.payload == ["item"]


async def test_not_ok_child_contributes_nothing():
    m = MultiPollable([_NotOkChild(), _OkChild("good", ["x"])])
    r = await m.poll(None)
    assert r.ok is True
    assert r.payload == ["x"]


async def test_all_children_fail_returns_not_ok():
    m = MultiPollable([_FailChild("a"), _FailChild("b")])
    r = await m.poll(None)
    assert r.ok is False
    assert r.payload == []


async def test_all_children_not_ok_returns_not_ok():
    m = MultiPollable([_NotOkChild("a"), _NotOkChild("b")])
    r = await m.poll(None)
    assert r.ok is False


# ── empty children ────────────────────────────────────────────────────────────

async def test_no_children_returns_ok_empty():
    m = MultiPollable([])
    r = await m.poll(None)
    assert r.ok is True
    assert r.payload == []


# ── changed / fingerprint ─────────────────────────────────────────────────────

async def test_changed_true_on_first_poll():
    m = MultiPollable([_OkChild("x", ["a"])])
    r = await m.poll(None)
    assert r.changed is True


async def test_changed_false_when_fingerprint_matches_prev():
    m = MultiPollable([_OkChild("x", ["a"])])
    first = await m.poll(None)
    second = await m.poll(first)
    assert second.changed is False
    assert second.fingerprint == first.fingerprint


async def test_changed_true_when_payload_differs():
    first = await MultiPollable([_OkChild("x", ["a"])]).poll(None)
    second = await MultiPollable([_OkChild("x", ["b"])]).poll(first)
    assert second.changed is True


async def test_fingerprint_is_set_on_result():
    m = MultiPollable([_OkChild("x", ["hello"])])
    r = await m.poll(None)
    assert r.fingerprint is not None
    assert r.fingerprint == fingerprint(["hello"])


# ── key ───────────────────────────────────────────────────────────────────────

def test_default_key():
    assert MultiPollable([]).key == "multi"


def test_custom_key():
    assert MultiPollable([], key="sweep").key == "sweep"


# ── concurrency ───────────────────────────────────────────────────────────────

async def test_children_polled_concurrently():
    """asyncio.gather is used, so all children are launched together."""
    import asyncio

    polled: list[str] = []

    class _TrackedChild:
        def __init__(self, key: str):
            self.key = key

        async def poll(self, prev):
            await asyncio.sleep(0)   # yield so both are in flight together
            polled.append(self.key)
            return PollResult(ok=True, changed=True,
                              fingerprint=self.key, payload=[self.key])

    m = MultiPollable([_TrackedChild("a"), _TrackedChild("b")])
    r = await m.poll(None)
    assert set(r.payload) == {"a", "b"}
    assert set(polled) == {"a", "b"}
