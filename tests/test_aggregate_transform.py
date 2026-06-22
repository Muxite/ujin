"""aggregate transform — group-by with per-group count, sum, min, max, collect."""
from __future__ import annotations

import pytest

from ujin.jobs.transforms import build_transform
from ujin.registry import BuildContext, register


def _t(cfg):
    return build_transform("aggregate", cfg)


# ── basic grouping ────────────────────────────────────────────────────────────

async def test_aggregate_counts_groups():
    items = [{"cat": "A"}, {"cat": "B"}, {"cat": "A"}, {"cat": "A"}]
    out = await _t({"by": "cat"}).apply({"payload": items})
    groups = {r["cat"]: r["count"] for r in out["payload"]}
    assert groups == {"A": 3, "B": 1}


async def test_aggregate_preserves_insertion_order():
    items = [{"x": 1}, {"x": 2}, {"x": 1}, {"x": 3}]
    out = await _t({"by": "x"}).apply({"payload": items})
    assert [r["x"] for r in out["payload"]] == [1, 2, 3]


async def test_aggregate_sum():
    items = [
        {"cat": "A", "score": 10},
        {"cat": "B", "score": 5},
        {"cat": "A", "score": 7},
    ]
    out = await _t({"by": "cat", "fields": [{"field": "score", "op": "sum"}]}).apply(
        {"payload": items}
    )
    rows = {r["cat"]: r for r in out["payload"]}
    assert rows["A"]["score_sum"] == 17
    assert rows["B"]["score_sum"] == 5


async def test_aggregate_min_max():
    items = [{"g": "X", "v": 3}, {"g": "X", "v": 1}, {"g": "X", "v": 2}]
    out = await _t({"by": "g", "fields": [
        {"field": "v", "op": "min"},
        {"field": "v", "op": "max"},
    ]}).apply({"payload": items})
    row = out["payload"][0]
    assert row["v_min"] == 1
    assert row["v_max"] == 3


async def test_aggregate_collect():
    items = [{"g": "A", "v": 1}, {"g": "A", "v": 2}, {"g": "B", "v": 9}]
    out = await _t({"by": "g", "fields": [{"field": "v", "op": "collect"}]}).apply(
        {"payload": items}
    )
    rows = {r["g"]: r for r in out["payload"]}
    assert rows["A"]["v_collect"] == [1, 2]
    assert rows["B"]["v_collect"] == [9]


async def test_aggregate_dotted_by_key():
    items = [{"meta": {"type": "A"}}, {"meta": {"type": "B"}}, {"meta": {"type": "A"}}]
    out = await _t({"by": "meta.type"}).apply({"payload": items})
    # output key is the last segment of the dotted path
    groups = {r["type"]: r["count"] for r in out["payload"]}
    assert groups == {"A": 2, "B": 1}


async def test_aggregate_dotted_field_path():
    items = [{"g": "A", "stats": {"val": 4}}, {"g": "A", "stats": {"val": 6}}]
    out = await _t({"by": "g", "fields": [{"field": "stats.val", "op": "sum"}]}).apply(
        {"payload": items}
    )
    assert out["payload"][0]["val_sum"] == 10


async def test_aggregate_missing_field_values_excluded_from_ops():
    items = [
        {"g": "A", "v": 5},
        {"g": "A"},           # no "v" — excluded from sum
        {"g": "A", "v": 3},
    ]
    out = await _t({"by": "g", "fields": [{"field": "v", "op": "sum"}]}).apply(
        {"payload": items}
    )
    assert out["payload"][0]["v_sum"] == 8
    assert out["payload"][0]["count"] == 3  # count includes all items


async def test_aggregate_empty_group_for_sum_yields_zero():
    # all items lack the aggregated field → sum is 0
    items = [{"g": "A"}, {"g": "A"}]
    out = await _t({"by": "g", "fields": [{"field": "v", "op": "sum"}]}).apply(
        {"payload": items}
    )
    assert out["payload"][0]["v_sum"] == 0


async def test_aggregate_empty_group_for_min_yields_none():
    items = [{"g": "A"}, {"g": "A"}]
    out = await _t({"by": "g", "fields": [{"field": "v", "op": "min"}]}).apply(
        {"payload": items}
    )
    assert out["payload"][0]["v_min"] is None


async def test_aggregate_none_group_key():
    # items missing the by key all land in the same group (key=None)
    items = [{"cat": "A"}, {"other": 1}, {"other": 2}]
    out = await _t({"by": "cat"}).apply({"payload": items})
    rows = {r["cat"]: r["count"] for r in out["payload"]}
    assert rows["A"] == 1
    assert rows[None] == 2


# ── pass-through cases ────────────────────────────────────────────────────────

async def test_aggregate_non_list_passes_through():
    ev = {"payload": "scalar"}
    assert await _t({"by": "x"}).apply(ev) is ev


async def test_aggregate_empty_list_passes_through():
    ev = {"payload": []}
    assert await _t({"by": "x"}).apply(ev) is ev


async def test_aggregate_none_payload_passes_through():
    ev = {"payload": None}
    assert await _t({"by": "x"}).apply(ev) is ev


async def test_aggregate_dict_payload_passes_through():
    ev = {"payload": {"key": "value"}}
    assert await _t({"by": "key"}).apply(ev) is ev


# ── config ────────────────────────────────────────────────────────────────────

def test_aggregate_requires_by():
    with pytest.raises(ValueError, match="by"):
        build_transform("aggregate", {})


async def test_aggregate_custom_out_path():
    ev = {"data": [{"g": "A"}, {"g": "B"}, {"g": "A"}], "meta": "keep"}
    out = await _t({"by": "g", "path": "data", "out": "summary"}).apply(ev)
    assert out["meta"] == "keep"
    assert len(out["summary"]) == 2
    groups = {r["g"]: r["count"] for r in out["summary"]}
    assert groups == {"A": 2, "B": 1}


async def test_aggregate_custom_path():
    ev = {"items": [{"g": "X"}, {"g": "X"}, {"g": "Y"}]}
    out = await _t({"by": "g", "path": "items"}).apply(ev)
    groups = {r["g"]: r["count"] for r in out["items"]}
    assert groups == {"X": 2, "Y": 1}


# ── registry / kinds discovery ────────────────────────────────────────────────

def test_aggregate_builds_through_registry():
    t = register.build_transform("aggregate", {"by": "x"}, BuildContext())
    assert t is not None


def test_aggregate_appears_in_kinds():
    assert "aggregate" in register.available("transform")
