"""Cross-product matrix / for_each: axis-name -> value-list (offline, deterministic).

The third matrix shape: instead of an inline list of variable maps, or a
``matrix: {from: <path>}`` data file, the value is a **mapping of axis-name to a
list of values**, fanned into one job per cartesian-product COMBINATION. Two axes
of sizes M and N produce M*N jobs, each with its axis vars substituted into the
resolved spec and a stable, deterministic, axis-order-independent id.

The change is strictly additive — the inline and file-backed forms still expand to
the same JobSpecs (see ``test_workflow_matrix.py`` / ``test_matrix_from_file.py``,
plus the regression guard below). These tests cover the cross-product happy path,
id determinism, provenance, and the error paths that route the offending file into
the loader's ``failed`` list.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ujin.jobs.app import (
    _cross_product_rows,
    _expand_matrix_entries,
    _load_ingest_plan,
    _load_workflows_dir,
    _slug,
    _specs_from_workflow_file,
)

_ROOT = Path(__file__).resolve().parents[1]
_EXAMPLE = _ROOT / "examples" / "matrix-cross-product.yaml"


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data))


# ── happy path: M*N jobs, one per combination ──────────────────────────────── #
def test_two_axes_produce_m_times_n_jobs(tmp_path):
    f = tmp_path / "status.yaml"
    _write(f, {
        "id": "s-{{ region }}-{{ tier }}",
        "matrix": {"region": ["us", "eu", "ap"], "tier": ["free", "pro"]},
        "source": {"kind": "http",
                   "config": {"url": "https://x/{{ region }}/{{ tier }}"}},
    })
    specs = _specs_from_workflow_file(f)
    # 3 regions * 2 tiers = 6 combinations
    assert len(specs) == 6
    urls = {s.source.config["url"] for s in specs}
    assert urls == {
        "https://x/us/free", "https://x/us/pro",
        "https://x/eu/free", "https://x/eu/pro",
        "https://x/ap/free", "https://x/ap/pro",
    }
    # one id per combination, all distinct
    assert len(set(s.id for s in specs)) == 6


def test_three_axes_full_product(tmp_path):
    f = tmp_path / "build.yaml"
    _write(f, {
        "matrix": {"os": ["linux", "mac"], "py": ["3.11", "3.12"],
                   "arch": ["x86", "arm"]},
        "source": {"kind": "http", "config": {"url": "https://x/{{ os }}"}},
    })
    specs = _specs_from_workflow_file(f)
    assert len(specs) == 2 * 2 * 2  # 8


def test_for_each_alias_accepts_cross_product(tmp_path):
    f = tmp_path / "f.yaml"
    _write(f, {
        "for_each": {"a": [1, 2], "b": ["x"]},
        "source": {"kind": "http", "config": {"url": "https://x/{{ a }}/{{ b }}"}},
    })
    specs = _specs_from_workflow_file(f)
    assert [s.source.config["url"] for s in specs] == ["https://x/1/x", "https://x/2/x"]


def test_whole_value_placeholder_preserves_type(tmp_path):
    f = tmp_path / "typed.yaml"
    _write(f, {
        "matrix": {"floor": [500, 800]},
        "source": {"kind": "api", "config": {"min_price": "{{ floor }}"}},
    })
    specs = _specs_from_workflow_file(f)
    # an exact-placeholder value keeps the native int type, like the inline form
    assert [s.source.config["min_price"] for s in specs] == [500, 800]


# ── stable, deterministic, axis-order-independent ids ──────────────────────── #
def test_default_ids_derive_from_values_axis_sorted(tmp_path):
    f = tmp_path / "m.yaml"
    _write(f, {
        "matrix": {"region": ["us", "eu"], "size": ["s", "l"]},
        "source": {"kind": "http", "config": {"url": "https://x/{{ region }}"}},
    })
    specs = _specs_from_workflow_file(f)
    # suffix is built with axis NAMES sorted (region, size), VALUES kept in order
    assert [s.id for s in specs] == ["m-us-s", "m-us-l", "m-eu-s", "m-eu-l"]


def test_ids_independent_of_axis_declaration_order(tmp_path):
    base = {"source": {"kind": "http", "config": {"url": "https://x/{{ a }}/{{ b }}"}}}
    f1 = tmp_path / "f1.yaml"
    _write(f1, {**base, "id": "j-{{ a }}-{{ b }}",
                "matrix": {"a": ["1", "2"], "b": ["x", "y"]}})
    f2 = tmp_path / "f2.yaml"
    _write(f2, {**base, "id": "j-{{ a }}-{{ b }}",
                "matrix": {"b": ["x", "y"], "a": ["1", "2"]}})
    ids1 = sorted(s.id for s in _specs_from_workflow_file(f1))
    ids2 = sorted(s.id for s in _specs_from_workflow_file(f2))
    assert ids1 == ids2  # reordering the axes leaves the id set unchanged


def test_ids_stable_across_repeated_resolution(tmp_path):
    f = tmp_path / "m.yaml"
    _write(f, {
        "matrix": {"region": ["us", "eu"], "tier": ["free", "pro"]},
        "source": {"kind": "http", "config": {"url": "https://x/{{ region }}"}},
    })
    first = [s.id for s in _specs_from_workflow_file(f)]
    again = [s.id for s in _specs_from_workflow_file(f)]
    assert first == again  # deterministic order + ids across runs


def test_explicit_id_template_honored(tmp_path):
    f = tmp_path / "m.yaml"
    _write(f, {
        "id": "feed-{{ region }}-{{ tier }}",
        "name": "Feed {{ region }}/{{ tier }}",
        "matrix": {"region": ["us"], "tier": ["free", "pro"]},
        "source": {"kind": "http", "config": {"url": "https://x"}},
    })
    specs = _specs_from_workflow_file(f)
    assert [s.id for s in specs] == ["feed-us-free", "feed-us-pro"]
    assert [s.name for s in specs] == ["Feed us/free", "Feed us/pro"]


def test_slug_collapses_non_alphanumerics():
    assert _slug("4k monitor") == "4k-monitor"
    assert _slug("us-east-1") == "us-east-1"
    assert _slug("a/b.c") == "a-b-c"
    assert _slug("!!!") == "x"  # all-symbol value falls back


# ── provenance: same shape as the inline form (key/index/vars) ─────────────── #
def test_provenance_records_the_combination(tmp_path):
    f = tmp_path / "m.yaml"
    _write(f, {
        "id": "j-{{ region }}-{{ tier }}",
        "matrix": {"region": ["us", "eu"], "tier": ["free"]},
        "source": {"kind": "http", "config": {"url": "https://x"}},
    })
    prov: list = []
    specs = _specs_from_workflow_file(f, provenance=prov)
    assert [p["id"] for p in prov] == [s.id for s in specs]
    # consistent with inline-matrix provenance: {key, index, vars}
    assert prov[0]["matrix"] == {
        "key": "matrix", "index": 0, "vars": {"region": "us", "tier": "free"},
    }
    assert prov[1]["matrix"] == {
        "key": "matrix", "index": 1, "vars": {"region": "eu", "tier": "free"},
    }


# ── regression guard: inline + file-backed forms are unchanged ─────────────── #
def test_inline_and_file_backed_forms_unchanged(tmp_path):
    """The two pre-existing matrix shapes still resolve to the same JobSpecs."""
    template = {
        "id": "feed-{{ slug }}",
        "source": {"kind": "http", "config": {"url": "https://x/{{ slug }}"}},
    }
    rows = [{"slug": "tech"}, {"slug": "bio"}]
    inline = tmp_path / "inline.yaml"
    _write(inline, {**template, "matrix": rows})
    _write(tmp_path / "data" / "rows.yaml", rows)
    from_file = tmp_path / "from_file.yaml"
    _write(from_file, {**template, "matrix": {"from": "data/rows.yaml"}})

    inline_specs = _specs_from_workflow_file(inline)
    file_specs = _specs_from_workflow_file(from_file)
    assert [s.id for s in inline_specs] == ["feed-tech", "feed-bio"]
    assert [s.id for s in inline_specs] == [s.id for s in file_specs]
    assert [s.source.config for s in inline_specs] == \
        [s.source.config for s in file_specs]


def test_from_is_reserved_routes_to_file_backed(tmp_path):
    """A mapping with a `from:` key is the file-backed form, never cross-product."""
    f = tmp_path / "m.yaml"
    _write(f, {
        "matrix": {"from": [1, 2]},  # `from` reserved -> file-backed path
        "source": {"kind": "http", "config": {}},
    })
    # routed to the file-backed loader, which rejects a non-string `from:`
    with pytest.raises(ValueError, match="needs a `from:` data-file path"):
        _specs_from_workflow_file(f)


# ── error paths: each raises ValueError -> routed into `failed` ────────────── #
@pytest.mark.parametrize("axes, match", [
    ({}, "at least one axis"),
    ({"region": []}, "non-empty list"),          # empty axis
    ({"region": "us"}, "non-empty list"),        # non-list axis value
    ({"region": ["us"], "tier": {}}, "non-empty list"),  # mapping, not a list
])
def test_invalid_axes_raise(tmp_path, axes, match):
    f = tmp_path / "m.yaml"
    _write(f, {"matrix": axes, "source": {"kind": "http", "config": {}}})
    with pytest.raises(ValueError, match=match):
        _specs_from_workflow_file(f)


def test_cross_product_rows_names_the_offending_axis():
    with pytest.raises(ValueError, match="axis 'tier'"):
        _cross_product_rows({"region": ["us"], "tier": "free"}, "matrix")


def test_colliding_generated_ids_rejected(tmp_path):
    f = tmp_path / "m.yaml"
    _write(f, {
        "id": "j-{{ region }}",     # `tier` not in the id -> combinations collide
        "matrix": {"region": ["us"], "tier": ["free", "pro"]},
        "source": {"kind": "http", "config": {}},
    })
    with pytest.raises(ValueError, match="duplicate id"):
        _specs_from_workflow_file(f)


def test_slug_collision_rejected(tmp_path):
    f = tmp_path / "m.yaml"
    _write(f, {
        # "a/b" and "a-b" slug to the same token -> default-id collision
        "matrix": {"k": ["a/b", "a-b"]},
        "source": {"kind": "http", "config": {}},
    })
    with pytest.raises(ValueError, match="duplicate id"):
        _specs_from_workflow_file(f)


# ── isolation: one bad cross-product file does not abort the others ────────── #
def test_bad_axes_routes_into_failed_workflows_dir(tmp_path):
    wf = tmp_path / "wf"
    _write(wf / "good.yaml",
           {"source": {"kind": "http", "config": {"url": "https://ok/"}}})
    _write(wf / "bad.yaml",
           {"matrix": {"region": "us"},  # non-list axis value
            "source": {"kind": "http", "config": {}}})
    failed: list = []
    specs = _load_workflows_dir(str(wf), failed=failed)
    assert [s.id for s in specs] == ["good"]
    assert failed and failed[0]["id"] == "bad"
    assert "non-empty list" in failed[0]["error"]


def test_bad_axes_fails_one_plan_job_only(tmp_path):
    plan = tmp_path / "plan.yaml"
    _write(plan, {"jobs": [
        {"id": "ok-job",
         "source": {"kind": "http", "config": {"url": "https://ok/"}}},
        {"id": "bad-{{ region }}",
         "matrix": {"region": []},  # empty axis
         "source": {"kind": "http", "config": {}}},
    ]})
    failed: list = []
    specs = _load_ingest_plan(plan, failed=failed)
    # the valid job still resolves; only the bad entry is reported
    assert [s.id for s in specs] == ["ok-job"]
    assert failed and failed[0]["id"] == "bad-{{ region }}"
    assert "ujin: plan job" in failed[0]["error"]
    assert "non-empty list" in failed[0]["error"]


# ── _expand_matrix_entries composes within a jobs: list ────────────────────── #
def test_cross_product_entry_within_jobs_list(tmp_path):
    f = tmp_path / "mix.yaml"
    _write(f, {"jobs": [
        {"source": {"kind": "http", "config": {"url": "https://plain/"}}},
        {"matrix": {"n": [1, 2]},
         "source": {"kind": "http", "config": {"url": "https://m/{{ n }}"}}},
    ]})
    specs = _specs_from_workflow_file(f)
    # plain entry keeps its <stem>-<index> id; cross-product fans out under its base
    assert [s.id for s in specs] == ["mix-0", "mix-1-1", "mix-1-2"]


# ── CLI: invalid cross-product surfaces cleanly (non-zero, no traceback) ───── #
def test_plan_validate_reports_clean_error(tmp_path, capsys):
    from ujin.cli import main

    plan = tmp_path / "plan.yaml"
    _write(plan, {"jobs": [
        {"id": "good",
         "source": {"kind": "http", "config": {"url": "https://ok/"}}},
        {"id": "bad-{{ region }}",
         "matrix": {"region": "nope"},
         "source": {"kind": "http", "config": {}}},
    ]})
    rc = main(["plan", "validate", str(plan)])
    out = capsys.readouterr().out
    assert rc == 1                          # non-zero exit
    assert "ok  good" in out                # the valid job still resolves
    assert "FAIL  bad-{{ region }}" in out
    assert "ujin:" in out and "non-empty list" in out


# ── the shipped example resolves/renders as documented ─────────────────────── #
def test_shipped_example_resolves_to_six_jobs():
    failed: list = []
    specs = _load_ingest_plan(_EXAMPLE, failed=failed)
    assert failed == []
    assert [s.id for s in specs] == [
        "status-us-east-free", "status-us-east-pro",
        "status-us-west-free", "status-us-west-pro",
        "status-eu-central-free", "status-eu-central-pro",
    ]
    first = specs[0]
    assert first.source.config["url"] == \
        "https://api.example.com/us-east/status?tier=free"
    assert first.sinks[-1].config["path"] == "/data/status-us-east-free.jsonl"


def test_shipped_example_render_ids_are_deterministic(capsys):
    import json

    from ujin.cli import main

    def _render_ids():
        main(["plan", "render", str(_EXAMPLE), "--json"])
        data = json.loads(capsys.readouterr().out)
        # ids + resolved configs are stable; only `created_at` is stamped per run
        return [(s["id"], s["source"]) for s in data]

    assert _render_ids() == _render_ids()  # stable across repeated renders


# ── GET /plan surfaces per-combination provenance ──────────────────────────── #
def test_get_plan_reports_cross_product_provenance(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ujin.jobs.app import create_jobs_app

    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    empty_wf = tmp_path / "wf"
    empty_wf.mkdir()
    plan = tmp_path / "plan.yaml"
    _write(plan, {"jobs": [
        {"id": "j-{{ region }}-{{ tier }}",
         "matrix": {"region": ["us", "eu"], "tier": ["free"]},
         "source": {"kind": "command", "config": {"argv": ["printf", "{{ region }}"]}},
         "schedule": {"mode": "once"}},
    ]})
    app = create_jobs_app(
        run_engine=False, plan_path=str(plan), workflows_dir=str(empty_wf)
    )
    with TestClient(app) as c:
        body = c.get("/plan").json()
    by_id = {j["id"]: j for j in body["jobs"]}
    assert set(by_id) == {"j-us-free", "j-eu-free"}
    assert by_id["j-us-free"]["provenance"]["matrix"] == {
        "key": "matrix", "index": 0, "vars": {"region": "us", "tier": "free"},
    }
    assert by_id["j-eu-free"]["provenance"]["matrix"] == {
        "key": "matrix", "index": 1, "vars": {"region": "eu", "tier": "free"},
    }
    assert body["failed"] == []
