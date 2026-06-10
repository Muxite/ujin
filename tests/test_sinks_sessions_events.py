"""Job sinks (webhook/HMAC, jsonl, ws, sqlite, stdout), the diff event sinks,
session-store persistence, and the scrape component builder."""
from __future__ import annotations

import hashlib
import hmac as hmac_mod
import json

import pytest

from ujin.jobs.sinks import build_sink

# ── job sinks ────────────────────────────────────────────────────────────────

async def test_webhook_sink_posts_signed_payload(fake_origin):
    fake_origin.add("/hook", body="{}", content_type="application/json")
    sink = build_sink("webhook", {
        "url": fake_origin.url("/hook"),
        "hmac_secret": "topsecret",
        "headers": {"X-Custom": "1"},
    })
    await sink.emit({"job_id": "j1", "fingerprint": "fp"})

    req = fake_origin.requests[-1]
    assert req.method == "POST"
    assert req.headers["Content-Type"] == "application/json"
    assert req.headers["X-Custom"] == "1"
    body = json.dumps({"fingerprint": "fp", "job_id": "j1"},
                      default=str, sort_keys=True).encode()
    want = hmac_mod.new(b"topsecret", body, hashlib.sha256).hexdigest()
    assert req.headers["X-Ujin-Signature"] == f"sha256={want}"


async def test_webhook_sink_4xx_logged_not_raised(fake_origin, caplog):
    fake_origin.add("/hook", body="no", status=410)
    sink = build_sink("webhook", {"url": fake_origin.url("/hook")})
    await sink.emit({"job_id": "j1"})  # must not raise
    assert any("410" in r.message for r in caplog.records)


async def test_forward_sink_is_webhook_alias(fake_origin):
    fake_origin.add("/fwd", body="{}")
    sink = build_sink("forward", {"url": fake_origin.url("/fwd"), "method": "put"})
    await sink.emit({"k": 1})
    assert fake_origin.requests[-1].method == "PUT"


async def test_jsonl_sink_appends_lines(tmp_path):
    path = tmp_path / "events.jsonl"
    sink = build_sink("jsonl", {"path": str(path)})
    await sink.emit({"n": 1})
    await sink.emit({"n": 2})
    lines = path.read_text().strip().splitlines()
    assert [json.loads(l)["n"] for l in lines] == [1, 2]


async def test_stdout_sink_prints_with_prefix(capsys):
    sink = build_sink("stdout", {"prefix": "EVT "})
    await sink.emit({"n": 1})
    out = capsys.readouterr().out
    assert out.startswith("EVT ") and '"n": 1' in out


async def test_ws_sink_broadcasts_or_drops():
    events = []

    class _Hub:
        async def broadcast_event(self, event):
            events.append(event)

    sink = build_sink("ws", {}, hub=_Hub())
    await sink.emit({"n": 1})
    assert events == [{"n": 1}]

    hubless = build_sink("ws", {})
    await hubless.emit({"n": 2})  # dropped, not raised


async def test_sqlite_sink_records_or_drops(tmp_path):
    from ujin.jobs import JobStore

    store = JobStore(tmp_path / "jobs.db")
    sink = build_sink("sqlite", {}, store=store)
    await sink.emit({"job_id": "j1", "fingerprint": "fp"})
    events = store.events("j1")
    assert len(events) == 1

    storeless = build_sink("sqlite", {})
    await storeless.emit({"job_id": "j1"})  # dropped, not raised


def test_build_sink_unknown_kind():
    with pytest.raises(ValueError, match="unknown sink kind"):
        build_sink("teleport", {})


# ── diff event sinks ─────────────────────────────────────────────────────────

class _Result:
    def __init__(self, payload=None, fingerprint="fp", ts=1.0):
        self.payload = payload
        self.fingerprint = fingerprint
        self.ts = ts


async def test_callback_sink_sync_and_async():
    from ujin.diff.events import CallbackSink, ChangeEvent

    seen = []
    await CallbackSink(seen.append)("key", _Result())
    assert isinstance(seen[0], ChangeEvent) and seen[0].key == "key"

    async def acb(event):
        seen.append(event)

    await CallbackSink(acb)("key2", _Result())
    assert seen[1].key == "key2"


async def test_change_event_extracts_region_diff():
    from ujin.diff.events import ChangeEvent

    class _Diff:
        def as_dict(self):
            return {"main": ["added headline"]}

    ev = ChangeEvent.from_result("k", _Result(payload={"region_diff": _Diff()}))
    assert ev.regions == {"main": ["added headline"]}
    # non-dict payload degrades to empty regions
    ev2 = ChangeEvent.from_result("k", _Result(payload="raw"))
    assert ev2.regions == {}


async def test_webhook_event_sink_posts(fake_origin):
    from ujin.diff.events import WebhookSink

    fake_origin.add("/hook", body="{}")
    await WebhookSink(fake_origin.url("/hook"))("key", _Result())
    req = fake_origin.requests[-1]
    assert req.method == "POST"


async def test_webhook_event_sink_swallows_errors():
    from ujin.diff.events import WebhookSink

    await WebhookSink("http://127.0.0.1:1/hook")("key", _Result())  # no raise


# ── session store ────────────────────────────────────────────────────────────

async def test_session_store_save_load_roundtrip(tmp_path):
    # async: aiohttp.CookieJar requires a running event loop
    from http.cookies import SimpleCookie

    from yarl import URL

    from ujin.session.store import SessionStore

    path = tmp_path / "cookies.pickle"
    s1 = SessionStore(path)
    cookie = SimpleCookie()
    cookie["sid"] = "abc123"
    s1.jar.update_cookies(cookie, URL("https://site.example.com/"))
    s1.save()
    assert path.exists()

    s2 = SessionStore(path)
    cookies = list(s2.jar)
    assert any(c.key == "sid" and c.value == "abc123" for c in cookies)


async def test_session_store_no_path_noop_save():
    from ujin.session.store import SessionStore

    s = SessionStore()
    s.save()  # no path -> silently does nothing
    s.clear()


async def test_session_store_corrupt_file_starts_empty(tmp_path):
    from ujin.session.store import SessionStore

    path = tmp_path / "cookies.pickle"
    path.write_bytes(b"not a pickle")
    s = SessionStore(path)
    assert list(s.jar) == []


# ── scrape component builder ─────────────────────────────────────────────────

async def test_build_and_close_scrape_components(tmp_path):
    from ujin.scrape.build import (
        build_scrape_components,
        close_scrape_components,
    )
    from ujin.scrape.config import ScrapeConfig

    cfg = ScrapeConfig(disk_cache_path=str(tmp_path / "cache.db"))
    comps = await build_scrape_components(cfg)
    try:
        assert comps.http is not None
        assert comps.cache is not None
        assert comps.policy is not None
        assert comps.disk is not None
    finally:
        await close_scrape_components(comps)


async def test_build_scrape_service_helper(tmp_path):
    from ujin.scrape.build import build_scrape_service
    from ujin.scrape.config import ScrapeConfig

    service, comps, aclose = await build_scrape_service(ScrapeConfig())
    try:
        assert hasattr(service, "scrape")
        assert comps.http is not None
    finally:
        await aclose()
