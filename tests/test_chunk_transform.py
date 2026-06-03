"""ChunkTransform: list slicing, token-budget packing, str windows, metadata."""
from __future__ import annotations

from ujin.jobs.transforms import build_transform


def _chunk(cfg):
    return build_transform("chunk", cfg)


async def test_list_split_by_size():
    t = _chunk({"size": 2})
    out = await t.apply({"payload": [1, 2, 3, 4, 5]})
    assert isinstance(out, list) and len(out) == 3
    assert [e["payload"] for e in out] == [[1, 2], [3, 4], [5]]
    assert [e["chunk_index"] for e in out] == [0, 1, 2]
    assert all(e["chunk_total"] == 3 for e in out)


async def test_default_size_when_unspecified():
    t = _chunk({})
    out = await t.apply({"payload": list(range(250))})
    # default size 100 -> 3 chunks
    assert len(out) == 3
    assert len(out[0]["payload"]) == 100 and len(out[2]["payload"]) == 50


async def test_token_budget_packs_list_items():
    # each item ~ len/4 tokens; "xxxx" -> 1 token. budget 2 -> 2 items per chunk.
    t = _chunk({"token_budget": 2})
    out = await t.apply({"payload": ["xxxx", "xxxx", "xxxx"]})
    assert [len(e["payload"]) for e in out] == [2, 1]


async def test_string_payload_windows():
    t = _chunk({"size": 4})
    out = await t.apply({"payload": "abcdefghij"})
    assert [e["payload"] for e in out] == ["abcd", "efgh", "ij"]


async def test_custom_path():
    t = _chunk({"path": "payload.items", "size": 1})
    out = await t.apply({"payload": {"items": [1, 2]}, "meta": "keep"})
    assert len(out) == 2
    assert out[0]["payload"]["items"] == [1]
    assert out[0]["meta"] == "keep"          # rest of the event is preserved


async def test_non_chunkable_passes_through():
    t = _chunk({"size": 2})
    ev = {"payload": 42}
    out = await t.apply(ev)
    assert out == ev                          # single dict, unchanged
    out2 = await t.apply({"payload": []})
    assert out2["payload"] == []              # empty list -> pass through


async def test_deep_copy_isolation():
    t = _chunk({"size": 1})
    out = await t.apply({"payload": [{"n": 1}, {"n": 2}]})
    out[0]["payload"][0]["n"] = 999
    assert out[1]["payload"][0]["n"] == 2     # chunks don't share references
