"""flatten / sort / limit / rename transforms — list reshaping in the pipeline.

These cover the happy paths, edge cases (non-list/non-dict pass-through, empty
inputs, missing keys), config validation, and that each builds + runs through the
registry exactly as JobManager wires them.
"""
from __future__ import annotations

import pytest

from ujin.jobs.pipeline import Pipeline
from ujin.jobs.transforms import build_transform
from ujin.poll.base import PollResult
from ujin.registry import BuildContext, register


def _t(kind, cfg):
    return build_transform(kind, cfg)


# ── flatten ──────────────────────────────────────────────────────────────────

async def test_flatten_fans_list_into_one_event_per_item():
    out = await _t("flatten", {}).apply({"job_id": "j", "payload": [1, 2, 3]})
    assert isinstance(out, list) and len(out) == 3
    assert [e["payload"] for e in out] == [1, 2, 3]
    assert all(e["job_id"] == "j" for e in out)


async def test_flatten_index_field():
    out = await _t("flatten", {"index": "i"}).apply({"payload": ["a", "b"]})
    assert [(e["payload"], e["i"]) for e in out] == [("a", 0), ("b", 1)]


async def test_flatten_custom_path_preserves_rest_of_event():
    out = await _t("flatten", {"path": "payload.items"}).apply(
        {"payload": {"items": [10, 20]}, "meta": "keep"}
    )
    assert len(out) == 2
    assert out[0]["payload"]["items"] == 10
    assert out[0]["meta"] == "keep"


async def test_flatten_non_list_passes_through():
    ev = {"payload": 42}
    assert await _t("flatten", {}).apply(ev) == ev


async def test_flatten_empty_list_drops_event():
    out = await _t("flatten", {}).apply({"payload": []})
    assert out == []  # an empty fan-out drops the event downstream


async def test_flatten_deep_copy_isolation():
    out = await _t("flatten", {}).apply({"payload": [{"n": 1}, {"n": 2}]})
    out[0]["payload"]["n"] = 999
    assert out[1]["payload"]["n"] == 2


# ── sort ─────────────────────────────────────────────────────────────────────

async def test_sort_by_key_ascending():
    ev = {"payload": [{"score": 3}, {"score": 1}, {"score": 2}]}
    out = await _t("sort", {"key": "score"}).apply(ev)
    assert [r["score"] for r in out["payload"]] == [1, 2, 3]


async def test_sort_by_key_descending():
    ev = {"payload": [{"score": 1}, {"score": 3}, {"score": 2}]}
    out = await _t("sort", {"key": "score", "reverse": True}).apply(ev)
    assert [r["score"] for r in out["payload"]] == [3, 2, 1]


async def test_sort_natural_order_no_key():
    out = await _t("sort", {}).apply({"payload": [3, 1, 2]})
    assert out["payload"] == [1, 2, 3]


async def test_sort_missing_key_sorts_last():
    ev = {"payload": [{"score": 2}, {"other": 1}, {"score": 1}]}
    out = await _t("sort", {"key": "score"}).apply(ev)
    scores = [r.get("score") for r in out["payload"]]
    assert scores == [1, 2, None]  # the missing-key row goes to the end


async def test_sort_mixed_types_does_not_raise():
    # mixed value types under the same key would raise TypeError on a naive sort
    ev = {"payload": [{"v": "b"}, {"v": 1}, {"v": "a"}]}
    out = await _t("sort", {"key": "v"}).apply(ev)
    # grouped by type then value; deterministic and exception-free
    assert [r["v"] for r in out["payload"]] == [1, "a", "b"]


async def test_sort_non_list_passes_through():
    ev = {"payload": {"a": 1}}
    assert await _t("sort", {"key": "a"}).apply(ev) == ev


async def test_sort_uncomparable_same_type_falls_back_to_str():
    # dicts are the same type but raise TypeError under `<`; the str fallback
    # keeps the transform from blowing up the pipeline.
    ev = {"payload": [{"v": {"z": 1}}, {"v": {"a": 2}}]}
    out = await _t("sort", {"key": "v"}).apply(ev)
    assert isinstance(out["payload"], list) and len(out["payload"]) == 2
    # ordered by str(value): "{'a': 2}" < "{'z': 1}"
    assert out["payload"][0]["v"] == {"a": 2}


# ── limit ────────────────────────────────────────────────────────────────────

async def test_limit_head():
    out = await _t("limit", {"count": 2}).apply({"payload": [1, 2, 3, 4]})
    assert out["payload"] == [1, 2]


async def test_limit_tail():
    out = await _t("limit", {"count": 2, "from": "tail"}).apply(
        {"payload": [1, 2, 3, 4]}
    )
    assert out["payload"] == [3, 4]


async def test_limit_zero_count_empties_list():
    assert (await _t("limit", {"count": 0}).apply({"payload": [1, 2]}))["payload"] == []
    out = await _t("limit", {"count": 0, "from": "tail"}).apply({"payload": [1, 2]})
    assert out["payload"] == []


async def test_limit_count_larger_than_list():
    out = await _t("limit", {"count": 10}).apply({"payload": [1, 2]})
    assert out["payload"] == [1, 2]


async def test_limit_non_list_passes_through():
    ev = {"payload": "scalar"}
    assert await _t("limit", {"count": 1}).apply(ev) == ev


def test_limit_requires_count():
    with pytest.raises(ValueError, match="count"):
        _t("limit", {})


def test_limit_rejects_bad_from():
    with pytest.raises(ValueError, match="from"):
        _t("limit", {"count": 1, "from": "middle"})


# ── rename ───────────────────────────────────────────────────────────────────

async def test_rename_dict_payload():
    out = await _t("rename", {"mapping": {"DOI": "id", "title": "name"}}).apply(
        {"payload": {"DOI": "10.1", "title": "Paper", "year": 2026}}
    )
    assert out["payload"] == {"id": "10.1", "name": "Paper", "year": 2026}


async def test_rename_list_of_dicts():
    out = await _t("rename", {"mapping": {"u": "url"}}).apply(
        {"payload": [{"u": "a"}, {"u": "b"}]}
    )
    assert [r["url"] for r in out["payload"]] == ["a", "b"]


async def test_rename_missing_key_left_untouched_by_default():
    out = await _t("rename", {"mapping": {"absent": "x"}}).apply(
        {"payload": {"keep": 1}}
    )
    assert out["payload"] == {"keep": 1}


async def test_rename_drop_missing_materializes_none():
    out = await _t("rename", {"mapping": {"absent": "x"}, "drop_missing": True}).apply(
        {"payload": {"keep": 1}}
    )
    assert out["payload"] == {"keep": 1, "x": None}


async def test_rename_non_dict_items_skipped():
    out = await _t("rename", {"mapping": {"a": "b"}}).apply(
        {"payload": [{"a": 1}, "scalar", 5]}
    )
    assert out["payload"] == [{"b": 1}, "scalar", 5]


async def test_rename_scalar_payload_passes_through():
    out = await _t("rename", {"mapping": {"a": "b"}}).apply({"payload": 7})
    assert out["payload"] == 7


def test_rename_requires_mapping():
    with pytest.raises(ValueError, match="mapping"):
        _t("rename", {})


# ── registry wiring (the JobManager path) ────────────────────────────────────

@pytest.mark.parametrize("kind,cfg", [
    ("flatten", {}),
    ("sort", {"key": "x"}),
    ("limit", {"count": 5}),
    ("rename", {"mapping": {"a": "b"}}),
])
def test_builds_through_registry_with_context(kind, cfg):
    t = register.build_transform(kind, cfg, BuildContext())
    assert t is not None


async def test_new_transforms_compose_in_a_pipeline():
    """sort -> limit -> rename -> flatten, end to end through Pipeline."""
    class _Recorder:
        def __init__(self):
            self.seen = []

        async def emit(self, event):
            self.seen.append(event)

    rec = _Recorder()
    pipe = Pipeline(
        transforms=[
            build_transform("sort", {"key": "score", "reverse": True}),
            build_transform("limit", {"count": 2}),
            build_transform("rename", {"mapping": {"score": "rank"}}),
            build_transform("flatten", {"index": "i"}),
        ],
        sinks=[rec],
    )
    r = PollResult(ok=True, changed=True, fingerprint="fp", payload=[
        {"score": 1}, {"score": 5}, {"score": 3},
    ])
    await pipe("j", r)
    # top-2 by score, renamed, one event each
    assert [(e["payload"]["rank"], e["i"]) for e in rec.seen] == [(5, 0), (3, 1)]
