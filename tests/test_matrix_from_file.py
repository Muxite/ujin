"""File-backed matrix / for_each: ``matrix: {from: <path>}`` (offline, deterministic).

The matrix value may be an external **data file** that supplies the list of
variable maps, instead of an inline list. The path resolves exactly the way an
``include:``/``use:`` fragment resolves — relative to the containing file's
directory, then ``$UJIN_WORKFLOWS_DIR``. The fan-out is otherwise identical to the
inline form: one job per row, ``{{ var }}`` substituted in, stable ids.

The change is strictly additive — an inline ``matrix:`` list still expands to the
same JobSpecs (see ``test_workflow_matrix.py``); these tests cover the file-backed
happy path, path resolution, and the error paths that route the offending file
into the loader's ``failed`` list.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ujin.jobs.app import (
    _expand_matrix_entries,
    _load_ingest_plan,
    _specs_from_workflow_file,
)

_ROOT = Path(__file__).resolve().parents[1]


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data))


# ── happy path: workflow file ──────────────────────────────────────────────── #
def test_file_backed_matrix_workflow_fans_out(tmp_path):
    _write(tmp_path / "data" / "feeds.yaml", [
        {"slug": "tech"}, {"slug": "bio"}, {"slug": "art"},
    ])
    f = tmp_path / "feeds.yaml"
    _write(f, {
        "id": "feed-{{ slug }}",
        "matrix": {"from": "data/feeds.yaml"},
        "source": {"kind": "http", "config": {"url": "https://x/{{ slug }}"}},
    })
    specs = _specs_from_workflow_file(f)
    assert [s.id for s in specs] == ["feed-tech", "feed-bio", "feed-art"]
    assert [s.source.config["url"] for s in specs] == [
        "https://x/tech", "https://x/bio", "https://x/art",
    ]


def test_for_each_from_is_an_alias(tmp_path):
    _write(tmp_path / "rows.yaml", [{"n": "a"}, {"n": "b"}])
    f = tmp_path / "feeds.yaml"
    _write(f, {
        "for_each": {"from": "rows.yaml"},
        "source": {"kind": "http", "config": {"url": "https://x/{{ n }}"}},
    })
    specs = _specs_from_workflow_file(f)
    assert [s.source.config["url"] for s in specs] == ["https://x/a", "https://x/b"]


def test_whole_value_placeholder_from_file_preserves_type(tmp_path):
    # rows from a data file behave exactly like inline rows: a whole-value
    # placeholder keeps the variable's native type.
    _write(tmp_path / "rows.yaml", [{"floor": 500, "hdrs": {"X": "1"}}])
    f = tmp_path / "typed.yaml"
    _write(f, {
        "matrix": {"from": "rows.yaml"},
        "source": {"kind": "api", "config": {"min_price": "{{ floor }}",
                                             "headers": "{{ hdrs }}"}},
    })
    (spec,) = _specs_from_workflow_file(f)
    assert spec.source.config["min_price"] == 500
    assert spec.source.config["headers"] == {"X": "1"}


# ── equivalence: file-backed expands to the same JobSpecs as inline ────────── #
def test_file_backed_equals_inline(tmp_path):
    rows = [{"slug": "tech", "floor": 1}, {"slug": "bio", "floor": 2}]
    template = {
        "id": "feed-{{ slug }}",
        "source": {"kind": "http", "config": {"url": "https://x/{{ slug }}/{{ floor }}"}},
    }
    _write(tmp_path / "data" / "rows.yaml", rows)
    inline = tmp_path / "inline.yaml"
    _write(inline, {**template, "matrix": rows})
    from_file = tmp_path / "from_file.yaml"
    _write(from_file, {**template, "matrix": {"from": "data/rows.yaml"}})

    inline_specs = _specs_from_workflow_file(inline)
    file_specs = _specs_from_workflow_file(from_file)
    # ids differ only by stem fallback; here both set an explicit id template, so
    # the produced ids and substituted configs match exactly.
    assert [s.id for s in inline_specs] == [s.id for s in file_specs]
    assert [s.source.config for s in inline_specs] == \
        [s.source.config for s in file_specs]


# ── happy path: ingest plan, with a matrix entry inside a jobs list ────────── #
def test_file_backed_matrix_plan_fans_out(tmp_path):
    _write(tmp_path / "data" / "feeds.yaml", [{"slug": "tech"}, {"slug": "science"}])
    plan = tmp_path / "plan.yaml"
    _write(plan, {"jobs": [
        {"id": "crossref",
         "source": {"kind": "http", "config": {"url": "https://api/works"}}},
        {"id": "feed-{{ slug }}",
         "matrix": {"from": "data/feeds.yaml"},
         "source": {"kind": "http", "config": {"url": "https://feeds/{{ slug }}"}}},
    ]})
    failed: list = []
    specs = _load_ingest_plan(plan, failed=failed)
    assert failed == []
    assert [s.id for s in specs] == ["crossref", "feed-tech", "feed-science"]
    assert specs[-1].source.config["url"] == "https://feeds/science"


# ── path resolution: relative to source dir, and via $UJIN_WORKFLOWS_DIR ───── #
def test_path_resolves_relative_to_source_file(tmp_path):
    # The data file sits NEXT TO the workflow in a nested directory; the `from:`
    # path is relative to that directory, not the process cwd.
    nested = tmp_path / "sub" / "dir"
    _write(nested / "data" / "rows.yaml", [{"slug": "ok"}])
    f = nested / "wf.yaml"
    _write(f, {
        "matrix": {"from": "data/rows.yaml"},
        "source": {"kind": "http", "config": {"url": "https://x/{{ slug }}"}},
    })
    (spec,) = _specs_from_workflow_file(f)
    assert spec.source.config["url"] == "https://x/ok"


def test_path_falls_back_to_workflows_dir_env(tmp_path, monkeypatch):
    # Not found next to the file -> tried under $UJIN_WORKFLOWS_DIR, like includes.
    shared = tmp_path / "shared"
    _write(shared / "rows.yaml", [{"slug": "env"}])
    monkeypatch.setenv("UJIN_WORKFLOWS_DIR", str(shared))
    wf_dir = tmp_path / "wf"
    f = wf_dir / "feeds.yaml"
    _write(f, {
        "matrix": {"from": "rows.yaml"},   # only exists under UJIN_WORKFLOWS_DIR
        "source": {"kind": "http", "config": {"url": "https://x/{{ slug }}"}},
    })
    (spec,) = _specs_from_workflow_file(f)
    assert spec.source.config["url"] == "https://x/env"


# ── error paths: each must raise ValueError -> routed into `failed` ────────── #
def test_missing_data_file_raises(tmp_path):
    f = tmp_path / "feeds.yaml"
    _write(f, {
        "matrix": {"from": "data/nope.yaml"},
        "source": {"kind": "http", "config": {}},
    })
    with pytest.raises(ValueError, match="data file not found"):
        _specs_from_workflow_file(f)


def test_empty_data_file_raises(tmp_path):
    _write(tmp_path / "rows.yaml", [])
    f = tmp_path / "feeds.yaml"
    _write(f, {"matrix": {"from": "rows.yaml"},
               "source": {"kind": "http", "config": {}}})
    with pytest.raises(ValueError, match="non-empty list"):
        _specs_from_workflow_file(f)


def test_data_file_not_a_list_raises(tmp_path):
    _write(tmp_path / "rows.yaml", {"slug": "tech"})  # a mapping, not a list
    f = tmp_path / "feeds.yaml"
    _write(f, {"matrix": {"from": "rows.yaml"},
               "source": {"kind": "http", "config": {}}})
    with pytest.raises(ValueError, match="non-empty list"):
        _specs_from_workflow_file(f)


def test_data_file_row_not_a_map_raises(tmp_path):
    _write(tmp_path / "rows.yaml", ["nope"])  # a list, but not of maps
    f = tmp_path / "feeds.yaml"
    _write(f, {"matrix": {"from": "rows.yaml"},
               "source": {"kind": "http", "config": {}}})
    with pytest.raises(ValueError, match="must be a variable map"):
        _specs_from_workflow_file(f)


@pytest.mark.parametrize("bad_from", [
    {"from": ""},                       # empty path
    {"from": 5},                        # not a string
    {"path": "rows.yaml"},              # wrong key
])
def test_malformed_from_mapping_raises(tmp_path, bad_from):
    f = tmp_path / "feeds.yaml"
    _write(f, {"matrix": bad_from, "source": {"kind": "http", "config": {}}})
    with pytest.raises(ValueError):
        _specs_from_workflow_file(f)


def test_extra_keys_beside_from_rejected(tmp_path):
    _write(tmp_path / "rows.yaml", [{"slug": "tech"}])
    f = tmp_path / "feeds.yaml"
    _write(f, {"matrix": {"from": "rows.yaml", "limit": 1},
               "source": {"kind": "http", "config": {}}})
    with pytest.raises(ValueError, match="accepts only `from:`"):
        _specs_from_workflow_file(f)


def test_invalid_yaml_data_file_raises(tmp_path):
    (tmp_path / "rows.yaml").write_text("- {unterminated: [")
    f = tmp_path / "feeds.yaml"
    _write(f, {"matrix": {"from": "rows.yaml"},
               "source": {"kind": "http", "config": {}}})
    with pytest.raises(ValueError):
        _specs_from_workflow_file(f)


def test_bad_data_file_routes_into_failed_list(tmp_path):
    # A whole workflows dir: the bad file lands in `failed`, the good one still loads.
    from ujin.jobs.app import _load_workflows_dir

    wf = tmp_path / "wf"
    _write(wf / "good.yaml",
           {"source": {"kind": "http", "config": {"url": "https://ok/"}}})
    _write(wf / "bad.yaml", {"matrix": {"from": "missing.yaml"},
                             "source": {"kind": "http", "config": {}}})
    failed: list = []
    specs = _load_workflows_dir(str(wf), failed=failed)
    assert [s.id for s in specs] == ["good"]
    assert failed and failed[0]["id"] == "bad"
    assert "data file not found" in failed[0]["error"]


def test_no_base_dir_raises_clear_error():
    # Direct call without a root (defensive): a `from:` cannot be resolved.
    entries = [{"matrix": {"from": "rows.yaml"},
                "source": {"kind": "http", "config": {}}}]
    with pytest.raises(ValueError, match="without a base directory"):
        _expand_matrix_entries(entries, "stem")


# ── the shipped example loads through the loader ───────────────────────────── #
def test_shipped_example_matrix_from_file_loads():
    failed: list = []
    specs = _load_ingest_plan(
        _ROOT / "examples" / "matrix-from-file.yaml", failed=failed)
    assert failed == []
    assert [s.id for s in specs] == ["search-laptop", "search-gpu", "search-monitor"]
    laptop = specs[0]
    assert laptop.source.config["url"] == \
        "https://api.example.com/search?q=gaming laptop&min_price=500"
    assert laptop.sinks[-1].config["path"] == "/data/laptop.jsonl"
