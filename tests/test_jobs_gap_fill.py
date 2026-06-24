"""Gap-fill tests for external-config loader and job machinery.

Closes remaining uncovered branches/lines in:
  ujin/jobs/transforms.py — _matches non-dict (53), SelectTransform no-fields
                            else branch (82->84), ChunkTransform non-chunkable
                            (219), AggregateTransform unhashable key (425-426),
                            non-dict item in group (442->441), unknown op
                            (452->436)
  ujin/jobs/model.py      — JobState.summary() (132)
  ujin/jobs/sinks.py      — _post_once asyncio.TimeoutError (112-113), OSError
                            in _spool (169-170), OSError in _drain_spool (192-193),
                            OSError in _remove (213-214)
  ujin/jobs/store.py      — corrupt JSON in get_events (220-221) and results
                            (264-265)
  ujin/jobs/app.py        — _dryrun_ingest_plan file_failed branch (1457->1459),
                            _introspect_active_plan wf_dir=None (1487->1491),
                            POST /plan/reload prune from query, dry_run from body
                            (1941->1943)

All tests are offline and deterministic — no real network, no wall-clock sleeps.
"""
from __future__ import annotations

import asyncio
import builtins
import json
import os
import time

import pytest


# ── helpers ───────────────────────────────────────────────────────────────────


def _try_import_client():
    """Skip tests that need FastAPI if it's not installed."""
    try:
        from fastapi.testclient import TestClient  # noqa: F401
    except ImportError:
        pytest.skip("fastapi not installed")
    return TestClient


# ── ujin/jobs/transforms.py ───────────────────────────────────────────────────


async def test_matches_non_dict_returns_false():
    """Line 53: _matches returns False immediately for non-dict items.

    SelectTransform uses _matches for list filtering; a plain scalar (like an
    int) in the payload list must be silently excluded, not raised.
    """
    from ujin.jobs.transforms import SelectTransform

    t = SelectTransform({"where": {"type": "good"}})
    event = {"payload": [42, "plain_string", {"type": "good"}]}
    out = await t.apply(event)
    # The two non-dict items fail _matches (line 53) and are dropped; only the
    # matching dict survives.
    assert out["payload"] == [{"type": "good"}]


async def test_select_no_fields_no_where_dict_passthrough():
    """Branch 82->84: inside the else block, if self.fields is falsy we skip
    the _project call and fall through directly to dotted_set (line 84).

    Trigger: non-list target + no 'fields' + no 'where' configured.
    """
    from ujin.jobs.transforms import SelectTransform

    t = SelectTransform({})  # fields=None, where=None
    event = {"payload": {"key": "value", "extra": 1}}
    out = await t.apply(event)
    assert out is event  # passed through unchanged


async def test_chunk_empty_string_returns_empty_pass_through():
    """Line 219: _split returns [] for an empty string payload; the event passes
    through unchanged because there is nothing to chunk.
    """
    from ujin.jobs.transforms import ChunkTransform

    t = ChunkTransform({"size": 2})
    event = {"payload": ""}
    out = await t.apply(event)
    assert out is event  # empty string → no chunks → pass-through


async def test_chunk_non_chunkable_target_passes_through():
    """Line 222: _split returns [] when target is neither list nor str.

    A non-chunkable payload (integer) passes the event through unchanged.
    """
    from ujin.jobs.transforms import ChunkTransform

    t = ChunkTransform({"size": 2})
    event = {"payload": 42}
    out = await t.apply(event)
    assert out is event  # pass-through: chunks==[] means unchanged


async def test_aggregate_unhashable_group_key():
    """Lines 425-426: when the by-field value is unhashable (e.g. a list),
    str(gval) is used as the hash key instead.
    """
    from ujin.jobs.transforms import build_transform

    t = build_transform("aggregate", {"by": "tags"})
    # Two items share the same list value for 'tags'; they must land in one group.
    items = [{"tags": ["a", "b"]}, {"tags": ["a", "b"]}, {"tags": ["c"]}]
    out = await t.apply({"payload": items})
    assert len(out["payload"]) == 2  # two distinct tag-lists → two groups
    counts = {r["count"] for r in out["payload"]}
    assert counts == {2, 1}  # ["a","b"] group has 2, ["c"] group has 1


async def test_aggregate_non_dict_items_in_group():
    """Branch 442->441: isinstance(it, dict) is False for non-dict list items.

    Non-dict items (scalars) end up in the None-key group; when the aggregate
    processes that group's field values the inner `isinstance(it, dict)` check
    is False, covering the skip-to-next-iteration branch.
    """
    from ujin.jobs.transforms import build_transform

    t = build_transform("aggregate", {
        "by": "g",
        "fields": [{"field": "v", "op": "sum"}],
    })
    # The scalar 99 is not a dict, so dotted_get(item, "g") → gval=None →
    # lands in the None group, where isinstance(it, dict) is False.
    items = [{"g": "A", "v": 10}, 99, {"g": "A", "v": 5}]
    out = await t.apply({"payload": items})
    rows = {str(r["g"]): r for r in out["payload"]}
    # Two groups: "A" (two dicts) and "None" (the scalar 99)
    assert rows["A"]["count"] == 2
    assert rows["A"]["v_sum"] == 15
    assert rows["None"]["count"] == 1
    assert rows["None"]["v_sum"] == 0  # scalar contributes nothing to the sum


async def test_aggregate_unknown_op_skips_field():
    """Branch 452->436: elif op == 'collect' is False when op is an unrecognised
    string; the field is silently skipped and the loop continues.
    """
    from ujin.jobs.transforms import build_transform

    t = build_transform("aggregate", {
        "by": "g",
        "fields": [{"field": "v", "op": "unknown_op"}],
    })
    items = [{"g": "A", "v": 1}, {"g": "A", "v": 2}]
    out = await t.apply({"payload": items})
    row = out["payload"][0]
    # The unknown op produces no column; only 'g' and 'count' are present.
    assert row["count"] == 2
    assert "v_unknown_op" not in row


# ── ujin/jobs/model.py ────────────────────────────────────────────────────────


def test_job_state_summary():
    """Line 132: JobState.summary() returns a dict with the expected keys."""
    from ujin.jobs.model import JobSpec, JobState, ScheduleSpec, SourceSpec

    spec = JobSpec(
        name="test-job",
        source=SourceSpec(kind="command", config={"argv": ["printf", "x"]}),
        schedule=ScheduleSpec(mode="once"),
    )
    state = JobState(spec=spec, runs=3, changes=1, last_error="boom")
    s = state.summary()
    assert s["id"] == spec.id
    assert s["name"] == "test-job"
    assert s["state"] == "idle"
    assert s["enabled"] is True
    assert s["schedule"] == "once"
    assert s["runs"] == 3
    assert s["changes"] == 1
    assert s["last_error"] == "boom"
    assert s["last_fingerprint"] is None


# ── ujin/jobs/sinks.py ────────────────────────────────────────────────────────


async def test_post_once_asyncio_timeout_is_retriable(caplog):
    """Lines 112-113: asyncio.TimeoutError in _post_once returns False (retriable).

    A mock session whose post() context manager raises TimeoutError exercises the
    `except asyncio.TimeoutError:` branch without touching the network.
    """
    from ujin.jobs.sinks import build_sink

    class _TimeoutCM:
        async def __aenter__(self):
            raise asyncio.TimeoutError()

        async def __aexit__(self, *a):
            pass

    class _MockSession:
        def request(self, *args, **kwargs):
            return _TimeoutCM()

    sink = build_sink("webhook", {"url": "http://127.0.0.1:1/hook", "spool_dir": ""})
    result = await sink._post_once(_MockSession(), b'{"x": 1}')
    assert result is False
    assert any("timed out" in r.message for r in caplog.records)


async def test_spool_write_os_error_logs_dropped(tmp_path, monkeypatch, caplog):
    """Lines 169-170: OSError during _spool write is caught; an error is logged
    and the event is silently dropped (not raised).
    """
    from ujin.jobs.sinks import build_sink

    original_open = builtins.open

    def patched_open(path, *args, **kwargs):
        # Fail only on the .tmp staging file that _spool creates.
        if str(path).endswith(".tmp"):
            raise OSError("disk full")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", patched_open)

    sink = build_sink("webhook", {"url": "http://127.0.0.1:1/hook", "spool_dir": ""})
    sink.spool_dir = str(tmp_path)  # bypass the hash-subdirectory logic
    sink._spool(b'{"job_id": "j1"}')  # must not raise

    assert any("spool write failed" in r.message for r in caplog.records)
    # No partial file should remain.
    assert list(tmp_path.glob("*.tmp")) == []


async def test_drain_spool_unreadable_file_is_skipped(tmp_path, monkeypatch):
    """Lines 192-193: OSError when opening a spool file during _drain_spool is
    caught and the loop continues to the next file (rather than crashing).
    """
    from ujin.jobs.sinks import build_sink

    # Lay down a real spool file so _spool_files() finds it.
    spool_file = tmp_path / "00000000000000000000-000000.json"
    spool_file.write_bytes(b'{"job_id": "j1"}')

    original_open = builtins.open

    def patched_open(path, *args, **kwargs):
        # Raise only when the drain tries to read the spool file in binary mode.
        mode = args[0] if args else kwargs.get("mode", "r")
        if str(path).endswith(".json") and "b" in mode:
            raise OSError("file vanished")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", patched_open)

    sink = build_sink("webhook", {"url": "http://127.0.0.1:1/hook", "spool_dir": ""})
    sink.spool_dir = str(tmp_path)  # bypass the hash-subdirectory logic
    # Must not raise; the unreadable file is silently skipped.
    await sink._drain_spool()


def test_remove_swallows_os_error(monkeypatch):
    """Lines 213-214: OSError in _remove is silently swallowed (the file may have
    already been removed by a concurrent drain or OS cleanup).
    """
    from ujin.jobs.sinks import WebhookSink

    def _fail_remove(path):
        raise OSError("permission denied")

    monkeypatch.setattr(os, "remove", _fail_remove)
    WebhookSink._remove("/nonexistent/path.json")  # must not raise


# ── ujin/jobs/store.py ────────────────────────────────────────────────────────


def test_events_skips_corrupt_json(tmp_path):
    """Lines 220-221: a corrupt event_json row in job_events is silently skipped
    so the caller receives only the valid rows (none here).
    """
    from ujin.jobs.store import JobStore

    store = JobStore(tmp_path / "jobs.db")
    with store._lock:  # noqa: SLF001
        store._conn.execute(
            "INSERT INTO job_events (job_id, ts, fingerprint, event_json)"
            " VALUES (?, ?, ?, ?)",
            ("j1", time.time(), None, "{not valid json"),
        )
        store._conn.commit()

    events = store.events("j1", limit=10)
    assert events == []  # corrupt row silently dropped
    store.close()


def test_results_skips_corrupt_json(tmp_path):
    """Lines 264-265: a corrupt payload_json row in job_results is silently
    skipped so the caller receives only valid rows (none here).
    """
    from ujin.jobs.store import JobStore

    store = JobStore(tmp_path / "jobs.db")
    with store._lock:  # noqa: SLF001
        store._conn.execute(
            "INSERT INTO job_results (job_id, ts, fingerprint, payload_json)"
            " VALUES (?, ?, ?, ?)",
            ("j1", time.time(), None, "{not valid json"),
        )
        store._conn.commit()

    results = store.results("j1", limit=10)
    assert results == []  # corrupt row silently dropped
    store.close()


# ── ujin/jobs/app.py ─────────────────────────────────────────────────────────


def test_dryrun_plan_file_failed_skips_prune_removed_list(tmp_path, monkeypatch):
    """Branch 1457->1459: when the plan file cannot be read (file-level failure),
    present stays empty and file_failed=True, so line 1458 is skipped and
    'removed' is reported as [] regardless of what plan_owned_ids() returns.

    Verified by calling _dryrun_ingest_plan directly with a non-existent path.
    """
    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    from ujin.jobs.app import _dryrun_ingest_plan

    class _StubManager:
        def get(self, _):
            return None

        def plan_owned_ids(self):
            # Would be called if file_failed were False; must not be reached.
            raise AssertionError("plan_owned_ids must not be called when file_failed")

    result = _dryrun_ingest_plan(
        _StubManager(),
        tmp_path / "no-such-plan.yaml",  # does not exist → file-level failure
        prune=True,
    )
    # file_failed=True → removed stays [] (line 1458 skipped)
    assert result["removed"] == []
    assert len(result["failed"]) >= 1


def test_introspect_active_plan_no_wf_dir(tmp_path):
    """Branch 1487->1491: when wf_dir is None, the workflows-dir scan is skipped
    and only the INGEST-PLAN is introspected.

    Exercises the False branch of `if wf_dir:` at line 1487.
    """
    from ujin.jobs.app import _introspect_active_plan

    plan = tmp_path / "plan.yaml"
    plan.write_text(
        "jobs:\n"
        "  - id: solo-job\n"
        "    source: {kind: command, config: {argv: [printf, x]}}\n"
        "    schedule: {mode: once}\n",
        encoding="utf-8",
    )
    result = _introspect_active_plan(str(plan), None)  # wf_dir=None
    assert result["workflows_dir"] is None
    assert len(result["jobs"]) == 1
    assert result["jobs"][0]["id"] == "solo-job"


def test_reload_prune_from_query_dry_run_from_body(tmp_path, monkeypatch):
    """Branch 1941->1943: when prune=True comes from query params, the
    `if not prune:` at line 1941 is False, so line 1942 is skipped and we jump
    directly to `if not dry_run:` at line 1943.

    Exercised by POST /plan/reload?prune=true with a JSON body {dry_run: true}.
    """
    TestClient = _try_import_client()
    from ujin.jobs.app import create_jobs_app

    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        "jobs:\n"
        "  - id: alpha\n"
        "    source: {kind: command, config: {argv: [printf, x]}}\n"
        "    schedule: {mode: once}\n",
        encoding="utf-8",
    )

    app = create_jobs_app(run_engine=False, plan_path=str(plan))
    with TestClient(app) as c:
        # prune=True from query param; dry_run not in query → body is parsed
        # body has dry_run=True → line 1941 is False (skip 1942), then 1943
        # sets dry_run=True from body → preview response
        body = c.post("/plan/reload?prune=true", json={"dry_run": True}).json()

    assert body.get("reloaded") is False
    assert body.get("dry_run") is True
    # Plan resolved; alpha would be unchanged (already loaded on startup)
    assert "unchanged" in body
