"""Matrix / for_each ``exclude:`` pruning + ``include:`` append (offline, deterministic).

Two strictly-additive filters ride along when a ``matrix:``/``for_each:`` value is a
**mapping** (cross-product axes or ``{from: <path>}``, or an ``include:``-only mapping
standing in for the inline list):

* ``include:`` — a list of *full* variable maps appended to the base rows.
* ``exclude:`` — a list of *partial* variable maps; a row (base or include) is dropped
  when it agrees with an exclude entry on every key that entry specifies.

A matrix block carrying neither resolves byte-for-byte as before (regression-guarded in
``test_matrix_cross_product.py`` / ``test_workflow_matrix.py`` / ``test_matrix_from_file.py``
and again below). These tests cover the pruning happy path, partial-key matching, the
no-op cases (matches nothing / references an absent key), the inline + file-backed forms,
``include`` append semantics, and the malformed-filter error paths routed into ``failed``.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ujin.jobs.app import (
    _load_ingest_plan,
    _load_workflows_dir,
    _matrix_rows,
    _row_excluded,
    _specs_from_workflow_file,
)

_ROOT = Path(__file__).resolve().parents[1]
_EXAMPLE = _ROOT / "examples" / "matrix-exclude.yaml"


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data))


# ── exclude prunes a cross-product ─────────────────────────────────────────── #
def test_exclude_prunes_one_cross_product_combination(tmp_path):
    f = tmp_path / "status.yaml"
    _write(f, {
        "id": "s-{{ region }}-{{ tier }}",
        "matrix": {
            "region": ["us", "eu", "ap"],
            "tier": ["free", "pro"],
            "exclude": [{"region": "eu", "tier": "free"}],
        },
        "source": {"kind": "http", "config": {"url": "https://x/{{ region }}/{{ tier }}"}},
    })
    specs = _specs_from_workflow_file(f)
    ids = [s.id for s in specs]
    # 3*2 = 6 combinations minus the single excluded {eu, free} -> 5 jobs
    assert "s-eu-free" not in ids
    assert len(ids) == 5
    assert set(ids) == {
        "s-us-free", "s-us-pro", "s-eu-pro", "s-ap-free", "s-ap-pro",
    }
    # surviving jobs keep deterministic, collision-free ids
    assert len(set(ids)) == 5


def test_exclude_multiple_entries(tmp_path):
    f = tmp_path / "m.yaml"
    _write(f, {
        "id": "j-{{ a }}-{{ b }}",
        "matrix": {
            "a": ["1", "2"],
            "b": ["x", "y"],
            "exclude": [{"a": "1", "b": "x"}, {"a": "2", "b": "y"}],
        },
        "source": {"kind": "http", "config": {"url": "https://x/{{ a }}/{{ b }}"}},
    })
    assert [s.id for s in _specs_from_workflow_file(f)] == ["j-1-y", "j-2-x"]


# ── partial-key matching: an entry naming a subset of axes drops a whole slice ─ #
def test_partial_key_exclude_drops_a_slice(tmp_path):
    f = tmp_path / "m.yaml"
    _write(f, {
        "id": "j-{{ region }}-{{ tier }}",
        "matrix": {
            "region": ["us", "eu"],
            "tier": ["free", "pro"],
            "exclude": [{"region": "eu"}],   # drops BOTH eu combinations
        },
        "source": {"kind": "http", "config": {"url": "https://x"}},
    })
    ids = [s.id for s in _specs_from_workflow_file(f)]
    assert ids == ["j-us-free", "j-us-pro"]   # every eu-* combination removed


# ── no-op: an exclude that matches nothing leaves the fan-out unchanged ─────── #
def test_exclude_matching_nothing_is_a_noop(tmp_path):
    base = {
        "id": "j-{{ region }}-{{ tier }}",
        "source": {"kind": "http", "config": {"url": "https://x"}},
    }
    axes = {"region": ["us", "eu"], "tier": ["free", "pro"]}
    plain = tmp_path / "plain.yaml"
    _write(plain, {**base, "matrix": dict(axes)})
    filtered = tmp_path / "filtered.yaml"
    _write(filtered, {**base, "matrix": {**axes, "exclude": [{"tier": "enterprise"}]}})
    # value 'enterprise' is on no row -> identical id set to the no-exclude file
    assert [s.id for s in _specs_from_workflow_file(filtered)] == \
        [s.id for s in _specs_from_workflow_file(plain)]


# ── absent-key parity: an exclude naming a key no row has never matches ─────── #
def test_exclude_referencing_absent_key_is_parity(tmp_path):
    base = {
        "id": "j-{{ region }}",
        "source": {"kind": "http", "config": {"url": "https://x"}},
    }
    axes = {"region": ["us", "eu"]}
    plain = tmp_path / "plain.yaml"
    _write(plain, {**base, "matrix": dict(axes)})
    filtered = tmp_path / "filtered.yaml"
    # 'zone' is not an axis -> the entry can never agree on it -> no row dropped
    _write(filtered, {**base, "matrix": {**axes, "exclude": [{"zone": "us"}]}})
    assert [s.id for s in _specs_from_workflow_file(filtered)] == \
        [s.id for s in _specs_from_workflow_file(plain)]


def test_row_excluded_unit():
    # present-and-equal on every named key -> excluded
    assert _row_excluded({"a": "1", "b": "x"}, [{"a": "1"}])
    # one named key differs -> kept
    assert not _row_excluded({"a": "1", "b": "x"}, [{"a": "2"}])
    # a named key absent from the row -> never matches (parity, kept)
    assert not _row_excluded({"a": "1"}, [{"c": "1"}])
    # empty exclude list -> nothing dropped
    assert not _row_excluded({"a": "1"}, [])


# ── exclude on the file-backed form ────────────────────────────────────────── #
def test_exclude_on_from_file_form(tmp_path):
    _write(tmp_path / "data" / "rows.yaml", [
        {"slug": "laptop"}, {"slug": "gpu"}, {"slug": "monitor"},
    ])
    f = tmp_path / "search.yaml"
    _write(f, {
        "id": "search-{{ slug }}",
        "matrix": {"from": "data/rows.yaml", "exclude": [{"slug": "gpu"}]},
        "source": {"kind": "http", "config": {"url": "https://x/{{ slug }}"}},
    })
    assert [s.id for s in _specs_from_workflow_file(f)] == \
        ["search-laptop", "search-monitor"]


# ── exclude on the inline list, written as an include-only mapping ─────────── #
def test_exclude_on_inline_list_via_include_mapping(tmp_path):
    f = tmp_path / "search.yaml"
    _write(f, {
        "id": "search-{{ slug }}",
        "matrix": {
            "include": [{"slug": "laptop"}, {"slug": "gpu"}, {"slug": "monitor"}],
            "exclude": [{"slug": "gpu"}],
        },
        "source": {"kind": "http", "config": {"url": "https://x/{{ slug }}"}},
    })
    assert [s.id for s in _specs_from_workflow_file(f)] == \
        ["search-laptop", "search-monitor"]


def test_include_only_mapping_equals_inline_list(tmp_path):
    """``matrix: {include: [...]}`` resolves to the same jobs as ``matrix: [...]``."""
    rows = [{"slug": "tech"}, {"slug": "bio"}]
    template = {"source": {"kind": "http", "config": {"url": "https://x/{{ slug }}"}}}
    inline = tmp_path / "inline.yaml"
    _write(inline, {**template, "matrix": rows})
    mapping = tmp_path / "mapping.yaml"
    _write(mapping, {**template, "matrix": {"include": rows}})
    inline_specs = _specs_from_workflow_file(inline)
    mapping_specs = _specs_from_workflow_file(mapping)
    # same default <stem>-<index> ids and same resolved configs
    assert [s.source.config for s in inline_specs] == \
        [s.source.config for s in mapping_specs]
    assert [s.id.split("-")[-1] for s in inline_specs] == ["0", "1"]
    assert [s.id.split("-")[-1] for s in mapping_specs] == ["0", "1"]


# ── include appends extra combinations to a cross-product ──────────────────── #
def test_include_appends_rows_after_exclusion(tmp_path):
    f = tmp_path / "m.yaml"
    _write(f, {
        "id": "j-{{ region }}-{{ tier }}",
        "matrix": {
            "region": ["us"],
            "tier": ["free", "pro"],
            "exclude": [{"tier": "pro"}],            # base -> just us-free
            "include": [{"region": "eu", "tier": "edge"}],  # appended verbatim
        },
        "source": {"kind": "http", "config": {"url": "https://x/{{ region }}/{{ tier }}"}},
    })
    specs = _specs_from_workflow_file(f)
    assert [s.id for s in specs] == ["j-us-free", "j-eu-edge"]
    # the include row is substituted like any other
    assert specs[-1].source.config["url"] == "https://x/eu/edge"


def test_exclude_applies_to_include_rows_too(tmp_path):
    f = tmp_path / "m.yaml"
    _write(f, {
        "id": "j-{{ slug }}",
        "matrix": {
            "include": [{"slug": "keep"}, {"slug": "drop"}],
            "exclude": [{"slug": "drop"}],   # exclude runs over the WHOLE fan-out
        },
        "source": {"kind": "http", "config": {"url": "https://x/{{ slug }}"}},
    })
    # exclude prunes every matching row, base or include -> only "keep" survives
    assert [s.id for s in _specs_from_workflow_file(f)] == ["j-keep"]


# ── additive: a matrix block with neither filter is unchanged ──────────────── #
def test_neither_filter_is_byte_for_byte_unchanged(tmp_path):
    base = {
        "id": "j-{{ region }}-{{ tier }}",
        "source": {"kind": "http", "config": {"url": "https://x/{{ region }}/{{ tier }}"}},
    }
    axes = {"region": ["us", "eu"], "tier": ["free", "pro"]}
    before = tmp_path / "before.yaml"
    _write(before, {**base, "matrix": dict(axes)})
    after = tmp_path / "after.yaml"
    # explicit empty filters are also no-ops
    _write(after, {**base, "matrix": {**axes, "exclude": [], "include": []}})
    assert [s.id for s in _specs_from_workflow_file(after)] == \
        [s.id for s in _specs_from_workflow_file(before)]


# ── error paths: malformed filter -> ValueError -> routed into `failed` ────── #
@pytest.mark.parametrize("matrix, match", [
    ({"region": ["us"], "exclude": "nope"}, r"`exclude:` must be a list"),
    ({"region": ["us"], "exclude": [42]}, r"`exclude:` entry 0 must be a variable map"),
    ({"region": ["us"], "include": {"a": 1}}, r"`include:` must be a list"),
    ({"region": ["us"], "include": ["x"]}, r"`include:` entry 0 must be a variable map"),
])
def test_malformed_filter_raises(tmp_path, matrix, match):
    f = tmp_path / "m.yaml"
    _write(f, {"matrix": matrix, "source": {"kind": "http", "config": {}}})
    with pytest.raises(ValueError, match=match):
        _specs_from_workflow_file(f)


def test_exclude_pruning_everything_raises(tmp_path):
    f = tmp_path / "m.yaml"
    _write(f, {
        "matrix": {"region": ["us"], "exclude": [{"region": "us"}]},
        "source": {"kind": "http", "config": {}},
    })
    with pytest.raises(ValueError, match="zero jobs"):
        _specs_from_workflow_file(f)


def test_matrix_rows_returns_filtered_pairs():
    rows = _matrix_rows(
        {"region": ["us", "eu"], "exclude": [{"region": "eu"}]}, "matrix", None
    )
    assert rows == [({"region": "us"}, "us")]


# ── isolation: one bad filter does not abort the rest of a workflows dir ────── #
def test_bad_filter_routes_into_failed_workflows_dir(tmp_path):
    wf = tmp_path / "wf"
    _write(wf / "good.yaml",
           {"source": {"kind": "http", "config": {"url": "https://ok/"}}})
    _write(wf / "bad.yaml",
           {"matrix": {"region": ["us"], "exclude": "nope"},
            "source": {"kind": "http", "config": {}}})
    failed: list = []
    specs = _load_workflows_dir(str(wf), failed=failed)
    assert [s.id for s in specs] == ["good"]
    assert failed and failed[0]["id"] == "bad"
    assert "ujin: workflow" in failed[0]["error"]
    assert "`exclude:` must be a list" in failed[0]["error"]


def test_bad_filter_fails_one_plan_job_only(tmp_path):
    plan = tmp_path / "plan.yaml"
    _write(plan, {"jobs": [
        {"id": "ok-job",
         "source": {"kind": "http", "config": {"url": "https://ok/"}}},
        {"id": "bad-{{ region }}",
         "matrix": {"region": ["us"], "include": ["bad"]},
         "source": {"kind": "http", "config": {}}},
    ]})
    failed: list = []
    specs = _load_ingest_plan(plan, failed=failed)
    assert [s.id for s in specs] == ["ok-job"]
    assert failed and failed[0]["id"] == "bad-{{ region }}"
    assert "ujin: plan job" in failed[0]["error"]
    assert "`include:` entry 0 must be a variable map" in failed[0]["error"]


# ── CLI: a malformed filter surfaces cleanly (non-zero, no traceback) ──────── #
def test_plan_validate_reports_clean_filter_error(tmp_path, capsys):
    from ujin.cli import main

    plan = tmp_path / "plan.yaml"
    _write(plan, {"jobs": [
        {"id": "good",
         "source": {"kind": "http", "config": {"url": "https://ok/"}}},
        {"id": "bad-{{ region }}",
         "matrix": {"region": ["us"], "exclude": "nope"},
         "source": {"kind": "http", "config": {}}},
    ]})
    rc = main(["plan", "validate", str(plan)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "ok  good" in out
    assert "FAIL  bad-{{ region }}" in out
    assert "ujin:" in out and "`exclude:` must be a list" in out


# ── the shipped example resolves as documented ─────────────────────────────── #
def test_shipped_example_resolves(tmp_path):
    failed: list = []
    specs = _load_ingest_plan(_EXAMPLE, failed=failed)
    assert failed == []
    ids = [s.id for s in specs]
    # 3 regions * 2 tiers = 6, minus the excluded {eu-central, pro}, plus one
    # explicit include row -> 6
    assert "status-eu-central-pro" not in ids
    assert "status-ap-south-canary" in ids   # the appended include row
    assert len(ids) == len(set(ids)) == 6


def test_shipped_example_provenance(tmp_path, monkeypatch):
    prov: list = []
    _load_ingest_plan(_EXAMPLE, provenance=prov)
    by_id = {p["id"]: p for p in prov}
    # the include row records its own variable map as provenance
    assert by_id["status-ap-south-canary"]["matrix"]["vars"] == {
        "region": "ap-south", "tier": "canary",
    }
