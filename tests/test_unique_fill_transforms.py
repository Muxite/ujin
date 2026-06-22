"""unique / fill transforms — dedup and field-defaulting in the pipeline.

Covers: list/non-list, key/no-key, missing/present fields, list-of-dicts,
empty payloads, dotted paths, and registry wiring.
"""
from __future__ import annotations

import pytest

from ujin.jobs.transforms import build_transform
from ujin.registry import register


def _t(kind, cfg):
    return build_transform(kind, cfg)


# ── unique ────────────────────────────────────────────────────────────────────

async def test_unique_dedupes_by_key():
    items = [{"id": 1, "v": "a"}, {"id": 2, "v": "b"}, {"id": 1, "v": "c"}]
    out = await _t("unique", {"key": "id"}).apply({"payload": items})
    assert [i["id"] for i in out["payload"]] == [1, 2]


async def test_unique_preserves_first_occurrence():
    items = [{"id": "x", "rank": 1}, {"id": "x", "rank": 2}]
    out = await _t("unique", {"key": "id"}).apply({"payload": items})
    assert out["payload"][0]["rank"] == 1


async def test_unique_no_key_primitives():
    items = [1, 2, 1, 3, 2]
    out = await _t("unique", {}).apply({"payload": items})
    assert out["payload"] == [1, 2, 3]


async def test_unique_no_key_dedupes_dicts_by_repr():
    items = [{"a": 1}, {"b": 2}, {"a": 1}]
    out = await _t("unique", {}).apply({"payload": items})
    assert len(out["payload"]) == 2


async def test_unique_non_list_passes_through():
    ev = {"payload": "not a list"}
    assert await _t("unique", {"key": "id"}).apply(ev) is ev


async def test_unique_non_list_dict_payload_passes_through():
    ev = {"payload": {"a": 1}}
    assert await _t("unique", {}).apply(ev) is ev


async def test_unique_empty_list():
    out = await _t("unique", {"key": "id"}).apply({"payload": []})
    assert out["payload"] == []


async def test_unique_custom_path():
    ev = {"data": [1, 1, 2], "meta": "keep"}
    out = await _t("unique", {"path": "data"}).apply(ev)
    assert out["data"] == [1, 2]
    assert out["meta"] == "keep"


async def test_unique_key_missing_on_some_items():
    # Items without the key all map to None; only the first None-key item kept.
    items = [{"id": 1}, {"id": 1}, {"other": "x"}, {"other": "y"}]
    out = await _t("unique", {"key": "id"}).apply({"payload": items})
    assert len(out["payload"]) == 2
    assert out["payload"][0] == {"id": 1}
    assert out["payload"][1] == {"other": "x"}


async def test_unique_order_preserved():
    items = [{"id": "c"}, {"id": "a"}, {"id": "b"}, {"id": "a"}]
    out = await _t("unique", {"key": "id"}).apply({"payload": items})
    assert [i["id"] for i in out["payload"]] == ["c", "a", "b"]


async def test_unique_registry():
    assert "unique" in register.available("transform")


async def test_unique_dotted_key():
    items = [{"meta": {"id": 1}}, {"meta": {"id": 2}}, {"meta": {"id": 1}}]
    out = await _t("unique", {"key": "meta.id"}).apply({"payload": items})
    assert len(out["payload"]) == 2


# ── fill ──────────────────────────────────────────────────────────────────────

async def test_fill_adds_missing_fields_to_dict():
    ev = {"payload": {"title": "hello"}}
    out = await _t("fill", {"fields": {"title": "untitled", "score": 0}}).apply(ev)
    assert out["payload"]["title"] == "hello"  # not overwritten
    assert out["payload"]["score"] == 0


async def test_fill_adds_missing_fields_to_list_of_dicts():
    items = [{"a": 1}, {"a": 2, "b": "x"}]
    out = await _t("fill", {"fields": {"b": "default"}}).apply({"payload": items})
    assert out["payload"][0]["b"] == "default"
    assert out["payload"][1]["b"] == "x"  # not overwritten


async def test_fill_paths_and_value_form():
    ev = {"payload": {"x": 10}}
    out = await _t("fill", {"paths": ["x", "y", "z"], "value": -1}).apply(ev)
    assert out["payload"]["x"] == 10  # not overwritten
    assert out["payload"]["y"] == -1
    assert out["payload"]["z"] == -1


async def test_fill_non_dict_payload_passes_through():
    ev = {"payload": "not a dict"}
    assert await _t("fill", {"fields": {"k": "v"}}).apply(ev) is ev


async def test_fill_non_dict_item_in_list_passes_through():
    items = [{"a": 1}, "skip_me", {"a": 2}]
    out = await _t("fill", {"fields": {"b": 0}}).apply({"payload": items})
    assert out["payload"][1] == "skip_me"
    assert out["payload"][0]["b"] == 0
    assert out["payload"][2]["b"] == 0


async def test_fill_empty_dict():
    ev = {"payload": {}}
    out = await _t("fill", {"fields": {"k": "v"}}).apply(ev)
    assert out["payload"]["k"] == "v"


async def test_fill_empty_list():
    ev = {"payload": []}
    out = await _t("fill", {"fields": {"k": "v"}}).apply(ev)
    assert out["payload"] == []


async def test_fill_dotted_field_path():
    ev = {"payload": {"meta": {}}}
    out = await _t("fill", {"fields": {"meta.score": 0}}).apply(ev)
    assert out["payload"]["meta"]["score"] == 0


async def test_fill_custom_event_path():
    ev = {"data": {"x": 1}, "meta": "keep"}
    out = await _t("fill", {"path": "data", "fields": {"y": 2}}).apply(ev)
    assert out["data"]["y"] == 2
    assert out["meta"] == "keep"


async def test_fill_registry():
    assert "fill" in register.available("transform")


async def test_fill_no_fields_noop():
    ev = {"payload": {"a": 1}}
    out = await _t("fill", {}).apply(ev)
    assert out["payload"] == {"a": 1}


async def test_fill_does_not_overwrite_falsy_values():
    ev = {"payload": {"count": 0, "flag": False, "name": ""}}
    out = await _t("fill", {"fields": {"count": 99, "flag": True, "name": "x"}}).apply(ev)
    # 0, False, "" are non-None so they must be left alone
    assert out["payload"]["count"] == 0
    assert out["payload"]["flag"] is False
    assert out["payload"]["name"] == ""


async def test_fill_list_of_dicts_independent_copies():
    items = [{"a": 1}, {"a": 2}]
    out = await _t("fill", {"fields": {"b": []}}).apply({"payload": items})
    out["payload"][0]["b"].append("x")
    assert out["payload"][1]["b"] == []  # each item got its own default
