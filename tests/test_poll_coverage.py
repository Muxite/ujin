"""Offline unit tests for poll subsystem edge/error branches.

Covers previously uncovered lines:
  ujin/poll/base.py     26, 79  — bytes fingerprint, decide_changed(None, ...)
  ujin/poll/command.py  25, 42-44, 47-48 — empty argv, timeout, generic exc
  ujin/poll/rss.py      23-24, 29-30     — ImportError, parse_feed exception
  ujin/poll/api.py      53-54, 72-73     — ImportError, request exception
  ujin/poll/site.py     54-58            — render=True (ObscuraFetcher) path
"""
from __future__ import annotations

import asyncio
import sys

import pytest

from ujin.poll.api import ApiPollable
from ujin.poll.base import PollResult, decide_changed, fingerprint
from ujin.poll.command import CommandPollable
from ujin.poll.rss import RssPollable
from ujin.poll.site import SitePollable


# ── base.py: line 26 (bytes branch) ─────────────────────────────────────────

def test_fingerprint_bytes_input():
    assert fingerprint(b"hello") == fingerprint(b"hello")
    assert fingerprint(b"hello") != fingerprint(b"world")
    # bytearray also takes the bytes branch
    assert fingerprint(bytearray(b"hi")) == fingerprint(b"hi")


# ── base.py: line 79 (decide_changed with None new_fp) ──────────────────────

def test_decide_changed_none_fp_is_always_false():
    assert decide_changed(None, None) is False
    prev = PollResult(fingerprint="abc")
    assert decide_changed(None, prev) is False


# ── command.py: line 25 (empty argv) ────────────────────────────────────────

def test_command_empty_argv_raises():
    with pytest.raises(ValueError, match="argv must be non-empty"):
        CommandPollable([])


# ── command.py: lines 42-44 (asyncio.TimeoutError during communicate) ───────

async def test_command_timeout(monkeypatch):
    async def _fake_wait_for(coro, timeout):
        coro.close()  # prevent "coroutine was never awaited" warning
        raise asyncio.TimeoutError()

    monkeypatch.setattr("asyncio.wait_for", _fake_wait_for)
    r = await CommandPollable(["echo", "hi"], timeout=0.001).poll(None)
    assert r.ok is False
    assert "timed out" in r.error


# ── command.py: lines 47-48 (non-FileNotFoundError exception) ───────────────

async def test_command_generic_exception(monkeypatch):
    async def _boom(*args, **kwargs):
        raise PermissionError("permission denied")

    monkeypatch.setattr("asyncio.create_subprocess_exec", _boom)
    r = await CommandPollable(["echo", "hi"]).poll(None)
    assert r.ok is False
    assert "PermissionError" in r.error


# ── rss.py: lines 23-24 (feedparser/rss module unavailable) ─────────────────

async def test_rss_import_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "ujin.sources.rss", None)
    r = await RssPollable("http://example.test/feed.xml").poll(None)
    assert r.ok is False
    assert "feedparser required" in r.error


# ── rss.py: lines 29-30 (parse_feed raises) ─────────────────────────────────

async def test_rss_parse_exception(monkeypatch):
    async def _bad_parse(url, *, timeout_secs=20):
        raise ConnectionError("unreachable")

    monkeypatch.setattr("ujin.sources.rss.parse_feed", _bad_parse)
    r = await RssPollable("http://example.test/feed.xml").poll(None)
    assert r.ok is False
    assert "ConnectionError" in r.error


# ── api.py: lines 53-54 (aiohttp not installed) ─────────────────────────────

async def test_api_import_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "aiohttp", None)
    r = await ApiPollable("http://example.test/api").poll(None)
    assert r.ok is False
    assert "aiohttp required" in r.error


# ── api.py: lines 72-73 (exception during HTTP request) ─────────────────────

async def test_api_connection_exception(monkeypatch):
    import aiohttp

    class _BrokenSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        def request(self, *args, **kwargs):
            raise aiohttp.ClientConnectionError("no route to host")

    monkeypatch.setattr("aiohttp.ClientSession", lambda **kw: _BrokenSession())
    r = await ApiPollable("http://example.test/api").poll(None)
    assert r.ok is False
    assert r.error


# ── site.py: lines 54-58 (render=True, ObscuraFetcher path) ─────────────────

async def test_site_render_mode(obscura_stub_bin):
    p = SitePollable("https://x.test/page", render=True)
    r = await p.poll(None)
    assert r.ok is True
    assert r.fingerprint is not None
    assert r.status == 200
