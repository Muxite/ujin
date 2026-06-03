"""Pipeline: event normalization, transforms, and sink fan-out semantics."""
from __future__ import annotations

from dataclasses import dataclass

from ujin.jobs.pipeline import Pipeline, Sink, build_event
from ujin.jobs.transforms import build_transform
from ujin.poll.base import PollResult


@dataclass
class _Dummy:
    a: int
    b: str


def test_build_event_normalizes_dataclass_payload():
    r = PollResult(ok=True, changed=True, fingerprint="fp", payload=_Dummy(1, "x"))
    ev = build_event("job1", r)
    assert ev["job_id"] == "job1"
    assert ev["fingerprint"] == "fp"
    assert ev["payload"] == {"a": 1, "b": "x"}  # asdict-normalized


async def _run(transforms, event):
    for spec_kind, cfg in transforms:
        t = build_transform(spec_kind, cfg)
        event = await t.apply(event)
        if event is None:
            return None
    return event


async def test_select_filters_and_projects_list():
    event = {"job_id": "j", "payload": [
        {"type": "journal-article", "DOI": "1", "title": "A", "x": 9},
        {"type": "posted-content", "DOI": "2", "title": "B"},
    ]}
    out = await _run([
        ("select", {"path": "payload", "where": {"type": "journal-article"}}),
        ("select", {"path": "payload", "fields": ["DOI", "title"]}),
    ], event)
    assert out["payload"] == [{"DOI": "1", "title": "A"}]


async def test_dedupe_by_key_drops_seen_items_and_empty_events():
    t = build_transform("dedupe", {"key": "DOI"})
    e1 = await t.apply({"payload": [{"DOI": "1"}, {"DOI": "2"}]})
    assert {d["DOI"] for d in e1["payload"]} == {"1", "2"}
    # second time, only a new DOI survives
    e2 = await t.apply({"payload": [{"DOI": "2"}, {"DOI": "3"}]})
    assert [d["DOI"] for d in e2["payload"]] == ["3"]
    # nothing new -> event dropped
    e3 = await t.apply({"payload": [{"DOI": "1"}, {"DOI": "3"}]})
    assert e3 is None


async def test_template_and_regex():
    e = await build_transform("template", {"template": "{job_id}:{fingerprint}"}).apply(
        {"job_id": "j", "fingerprint": "abc"}
    )
    assert e["message"] == "j:abc"
    e = await build_transform("regex", {"field": "payload", "pattern": r"\d+"}).apply(
        {"payload": "a12 b34"}
    )
    assert e["extracted"] == ["12", "34"]


class _RecordingSink:
    def __init__(self):
        self.seen = []

    async def emit(self, event: dict) -> None:
        self.seen.append(event)


class _BoomSink:
    async def emit(self, event: dict) -> None:
        raise RuntimeError("boom")


async def test_pipeline_fanout_one_failing_sink_does_not_block_others():
    good = _RecordingSink()
    pipe = Pipeline(transforms=[], sinks=[_BoomSink(), good])
    r = PollResult(ok=True, changed=True, fingerprint="fp", payload={"k": 1})
    await pipe("job1", r)
    # the good sink still received the event despite the failing one
    assert len(good.seen) == 1
    assert good.seen[0]["payload"] == {"k": 1}


async def test_pipeline_transform_drop_short_circuits_sinks():
    good = _RecordingSink()
    pipe = Pipeline(
        transforms=[build_transform("dedupe", {})],  # fingerprint dedupe
        sinks=[good],
    )
    r = PollResult(ok=True, changed=True, fingerprint="same", payload=1)
    await pipe("j", r)
    await pipe("j", r)  # same fingerprint -> dropped
    assert len(good.seen) == 1


class _FanOutTransform:
    """Returns N copies of the event — exercises list fan-out."""

    def __init__(self, n: int):
        self.n = n

    async def apply(self, event: dict):
        return [dict(event, copy_index=i) for i in range(self.n)]


async def test_pipeline_fans_out_list_returns_to_each_sink():
    good = _RecordingSink()
    pipe = Pipeline(transforms=[_FanOutTransform(3)], sinks=[good])
    r = PollResult(ok=True, changed=True, fingerprint="fp", payload={"k": 1})
    await pipe("j", r)
    assert len(good.seen) == 3
    assert [e["copy_index"] for e in good.seen] == [0, 1, 2]


async def test_pipeline_fanout_one_failing_sink_isolated_across_chunks():
    good = _RecordingSink()
    pipe = Pipeline(transforms=[_FanOutTransform(2)], sinks=[_BoomSink(), good])
    r = PollResult(ok=True, changed=True, fingerprint="fp", payload={"k": 1})
    await pipe("j", r)
    # the good sink still receives both fanned-out events
    assert len(good.seen) == 2


async def test_chunk_transform_in_pipeline_emits_per_chunk():
    good = _RecordingSink()
    pipe = Pipeline(transforms=[build_transform("chunk", {"size": 2})], sinks=[good])
    r = PollResult(ok=True, changed=True, fingerprint="fp",
                   payload=[{"i": 1}, {"i": 2}, {"i": 3}])
    await pipe("j", r)
    assert len(good.seen) == 2                       # 3 items / size 2 -> 2 chunks
    assert good.seen[0]["payload"] == [{"i": 1}, {"i": 2}]
    assert good.seen[1]["payload"] == [{"i": 3}]
    assert [e["chunk_index"] for e in good.seen] == [0, 1]
    assert all(e["chunk_total"] == 2 for e in good.seen)
