"""JobStore: spec round-trip, list/delete/enable, and run history.

All stdlib + a tmp sqlite file — no service extras needed.
"""
from __future__ import annotations

import time

from ujin.jobs import (
    JobSpec,
    JobStore,
    ScheduleSpec,
    SinkSpec,
    SourceSpec,
    TransformSpec,
)


def _spec(name: str = "crossref", **kw) -> JobSpec:
    return JobSpec(
        name=name,
        source=SourceSpec(kind="api", config={"url": "https://api.crossref.org/works",
                                              "json_path": "message.items"}),
        transforms=[TransformSpec(kind="select", config={"fields": ["DOI", "title"]})],
        sinks=[SinkSpec(kind="jsonl", config={"path": "/tmp/x.jsonl"})],
        schedule=ScheduleSpec(mode="adaptive", base=3600, min=600, max=86400),
        **kw,
    )


def test_spec_roundtrips_through_dict():
    spec = _spec()
    rebuilt = JobSpec.from_dict(spec.to_dict())
    assert rebuilt.id == spec.id
    assert rebuilt.name == spec.name
    assert rebuilt.source.kind == "api"
    assert rebuilt.source.config["json_path"] == "message.items"
    assert rebuilt.transforms[0].kind == "select"
    assert rebuilt.sinks[0].config["path"] == "/tmp/x.jsonl"
    assert rebuilt.schedule.mode == "adaptive"
    assert rebuilt.schedule.base == 3600


def test_upsert_get_list_delete(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    spec = _spec()
    store.upsert(spec)

    got = store.get(spec.id)
    assert got is not None and got.name == "crossref"
    assert [s.id for s in store.list()] == [spec.id]

    # upsert again updates in place (no duplicate row)
    spec.name = "crossref-renamed"
    store.upsert(spec)
    assert store.get(spec.id).name == "crossref-renamed"
    assert len(store.list()) == 1

    assert store.delete(spec.id) is True
    assert store.get(spec.id) is None
    assert store.delete(spec.id) is False
    store.close()


def test_set_enabled_persists(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    spec = _spec()
    store.upsert(spec)
    store.set_enabled(spec.id, False)
    assert store.get(spec.id).enabled is False
    store.set_enabled(spec.id, True)
    assert store.get(spec.id).enabled is True
    store.close()


def test_persistence_survives_reopen(tmp_path):
    path = tmp_path / "jobs.db"
    spec = _spec()
    JobStore(path).upsert(spec)
    # fresh handle on the same file = simulates a restart
    reopened = JobStore(path)
    assert reopened.get(spec.id) is not None
    reopened.close()


def test_record_and_query_runs(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    spec = _spec()
    store.upsert(spec)
    t0 = time.time()
    store.record_run(spec.id, started_at=t0, finished_at=t0 + 0.1,
                     ok=True, changed=True, fingerprint="abc", strategy="http")
    store.record_run(spec.id, started_at=t0 + 1, finished_at=t0 + 1.1,
                     ok=False, changed=False, error="boom")

    runs = store.runs(spec.id, limit=10)
    assert len(runs) == 2
    # newest first
    assert runs[0]["ok"] is False and runs[0]["error"] == "boom"
    assert runs[1]["ok"] is True and runs[1]["fingerprint"] == "abc"

    assert store.runs(spec.id, limit=1) == runs[:1]
    store.close()


def test_corrupt_spec_is_skipped(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    store.upsert(_spec())
    # hand-corrupt one row
    with store._lock:  # noqa: SLF001 - test reaches into the store deliberately
        store._conn.execute(
            "INSERT INTO jobs (id, name, enabled, spec_json, created_at, updated_at)"
            " VALUES ('bad', 'bad', 1, '{not json', ?, ?)",
            (time.time(), time.time()),
        )
        store._conn.commit()
    # the good one still loads; the corrupt one is dropped, not raised
    specs = store.list()
    assert len(specs) == 1
    assert store.get("bad") is None
    store.close()
