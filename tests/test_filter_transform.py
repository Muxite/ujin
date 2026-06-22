"""filter transform — predicate-based keep/drop for list and dict payloads.

Covers: all operators (eq/ne/gt/lt/ge/le/in/contains/exists/regex/matches),
negate/exclude flag, list vs dict payloads, dotted key paths, pass-through for
non-list/non-dict payloads, empty list, missing keys, and registry wiring.
"""
from __future__ import annotations

import pytest

from ujin.jobs.transforms import build_transform
from ujin.registry import register


def _t(cfg):
    return build_transform("filter", cfg)


# ── operator tests on list payloads ───────────────────────────────────────────

async def test_eq_keeps_matching_items():
    items = [{"v": 1}, {"v": 2}, {"v": 1}]
    out = await _t({"key": "v", "op": "eq", "value": 1}).apply({"payload": items})
    assert out["payload"] == [{"v": 1}, {"v": 1}]


async def test_ne_drops_matching_items():
    items = [{"v": 1}, {"v": 2}, {"v": 3}]
    out = await _t({"key": "v", "op": "ne", "value": 2}).apply({"payload": items})
    assert [i["v"] for i in out["payload"]] == [1, 3]


async def test_gt_keeps_greater():
    items = [{"n": 5}, {"n": 10}, {"n": 3}]
    out = await _t({"key": "n", "op": "gt", "value": 4}).apply({"payload": items})
    assert [i["n"] for i in out["payload"]] == [5, 10]


async def test_lt_keeps_lesser():
    items = [{"n": 5}, {"n": 2}, {"n": 8}]
    out = await _t({"key": "n", "op": "lt", "value": 5}).apply({"payload": items})
    assert [i["n"] for i in out["payload"]] == [2]


async def test_ge_keeps_greater_or_equal():
    items = [{"n": 3}, {"n": 5}, {"n": 7}]
    out = await _t({"key": "n", "op": "ge", "value": 5}).apply({"payload": items})
    assert [i["n"] for i in out["payload"]] == [5, 7]


async def test_le_keeps_lesser_or_equal():
    items = [{"n": 3}, {"n": 5}, {"n": 7}]
    out = await _t({"key": "n", "op": "le", "value": 5}).apply({"payload": items})
    assert [i["n"] for i in out["payload"]] == [3, 5]


async def test_in_keeps_items_whose_key_value_is_in_set():
    items = [{"tag": "a"}, {"tag": "b"}, {"tag": "c"}]
    out = await _t({"key": "tag", "op": "in", "value": ["a", "c"]}).apply({"payload": items})
    assert [i["tag"] for i in out["payload"]] == ["a", "c"]


async def test_contains_keeps_items_whose_key_value_contains_rhs():
    items = [{"title": "hello world"}, {"title": "foo"}, {"title": "world news"}]
    out = await _t({"key": "title", "op": "contains", "value": "world"}).apply({"payload": items})
    assert len(out["payload"]) == 2


async def test_exists_keeps_items_with_non_none_key():
    items = [{"score": 1}, {"other": 2}, {"score": None}]
    out = await _t({"key": "score", "op": "exists"}).apply({"payload": items})
    assert out["payload"] == [{"score": 1}]


async def test_exists_is_default_op():
    items = [{"x": 1}, {"y": 2}]
    out = await _t({"key": "x"}).apply({"payload": items})
    assert out["payload"] == [{"x": 1}]


async def test_regex_keeps_matching():
    items = [{"url": "https://a.com"}, {"url": "http://b.org"}, {"url": "ftp://c.net"}]
    out = await _t({"key": "url", "op": "regex", "value": r"^https?"}).apply({"payload": items})
    assert len(out["payload"]) == 2


async def test_matches_is_alias_for_regex():
    items = [{"s": "abc123"}, {"s": "xyz"}]
    out = await _t({"key": "s", "op": "matches", "value": r"\d+"}).apply({"payload": items})
    assert out["payload"] == [{"s": "abc123"}]


# ── negate / exclude ──────────────────────────────────────────────────────────

async def test_negate_inverts_predicate():
    items = [{"v": 1}, {"v": 2}, {"v": 3}]
    out = await _t({"key": "v", "op": "eq", "value": 2, "negate": True}).apply({"payload": items})
    assert [i["v"] for i in out["payload"]] == [1, 3]


async def test_exclude_is_alias_for_negate():
    items = [{"v": "a"}, {"v": "b"}]
    out = await _t({"key": "v", "op": "eq", "value": "a", "exclude": True}).apply({"payload": items})
    assert [i["v"] for i in out["payload"]] == ["b"]


# ── order preservation ────────────────────────────────────────────────────────

async def test_original_order_preserved():
    items = [{"n": 5}, {"n": 1}, {"n": 9}, {"n": 3}]
    out = await _t({"key": "n", "op": "gt", "value": 2}).apply({"payload": items})
    assert [i["n"] for i in out["payload"]] == [5, 9, 3]


# ── dict payload — keep or drop whole event ───────────────────────────────────

async def test_dict_payload_kept_when_predicate_holds():
    ev = {"payload": {"score": 10}}
    out = await _t({"key": "score", "op": "gt", "value": 5}).apply(ev)
    assert out is ev


async def test_dict_payload_dropped_when_predicate_fails():
    ev = {"payload": {"score": 3}}
    out = await _t({"key": "score", "op": "gt", "value": 5}).apply(ev)
    assert out is None


async def test_dict_payload_with_negate():
    ev = {"payload": {"status": "inactive"}}
    out = await _t({"key": "status", "op": "eq", "value": "active", "negate": True}).apply(ev)
    assert out is ev


# ── dotted key path ───────────────────────────────────────────────────────────

async def test_dotted_key_on_list():
    items = [{"meta": {"score": 8}}, {"meta": {"score": 2}}, {"meta": {}}]
    out = await _t({"key": "meta.score", "op": "ge", "value": 5}).apply({"payload": items})
    assert len(out["payload"]) == 1
    assert out["payload"][0]["meta"]["score"] == 8


# ── dotted path to list ───────────────────────────────────────────────────────

async def test_dotted_path_to_list():
    ev = {"data": {"items": [{"v": 1}, {"v": 2}]}}
    out = await _t({"path": "data.items", "key": "v", "op": "eq", "value": 1}).apply(ev)
    assert out["data"]["items"] == [{"v": 1}]


# ── pass-through / edge cases ─────────────────────────────────────────────────

async def test_non_list_non_dict_passes_through():
    ev = {"payload": "just a string"}
    out = await _t({"key": "x", "op": "exists"}).apply(ev)
    assert out is ev


async def test_none_payload_passes_through():
    ev = {"payload": None}
    out = await _t({"key": "x", "op": "exists"}).apply(ev)
    assert out is ev


async def test_integer_payload_passes_through():
    ev = {"payload": 42}
    out = await _t({"key": "x"}).apply(ev)
    assert out is ev


async def test_empty_list_passes_through_as_empty():
    ev = {"payload": []}
    out = await _t({"key": "v", "op": "eq", "value": 1}).apply(ev)
    assert out["payload"] == []


async def test_missing_key_field_treated_as_none_not_raises():
    items = [{"a": 1}, {"b": 2}]  # neither has key "c"
    out = await _t({"key": "c", "op": "gt", "value": 0}).apply({"payload": items})
    assert out["payload"] == []  # none pass gt(None, 0)


async def test_type_error_in_comparison_does_not_raise():
    # comparing string to int — TypeError caught, item excluded
    items = [{"v": "text"}, {"v": 5}]
    out = await _t({"key": "v", "op": "gt", "value": 3}).apply({"payload": items})
    assert out["payload"] == [{"v": 5}]


# ── registry wiring ───────────────────────────────────────────────────────────

def test_filter_registered_as_builtin():
    assert "filter" in register.available("transform")


async def test_filter_buildable_through_registry():
    from ujin.registry import BuildContext
    t = register.build_transform("filter", {"key": "v", "op": "eq", "value": 1}, BuildContext())
    out = await t.apply({"payload": [{"v": 1}, {"v": 2}]})
    assert out["payload"] == [{"v": 1}]


# ── config validation ─────────────────────────────────────────────────────────

def test_missing_key_raises():
    with pytest.raises(ValueError, match="requires 'key'"):
        build_transform("filter", {})


def test_unknown_op_raises():
    with pytest.raises(ValueError, match="unknown op"):
        build_transform("filter", {"key": "x", "op": "startswith"})
