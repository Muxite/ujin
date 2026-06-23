"""Matrix / for_each fan-out for workflow files (offline, deterministic).

A workflow mapping carrying ``matrix:`` (alias ``for_each:``) — a list of variable
maps — loads as one JobSpec per entry, with each entry's variables substituted
into every ``{{ var }}`` placeholder across source/transforms/sinks/schedule. The
generated jobs get stable, distinct ids so reloads upsert rather than duplicate.

These tests exercise the pure loader helpers in :mod:`ujin.jobs.app` directly (no
network, no fastapi needed) plus one end-to-end reload check.
"""
from __future__ import annotations

import pytest
import yaml

from ujin.jobs.app import (
    _expand_matrix_entries,
    _has_matrix,
    _specs_from_workflow_file,
    _subst_vars,
)


def _write(path, data) -> None:
    path.write_text(yaml.safe_dump(data))


# ── expansion ──────────────────────────────────────────────────────────────── #
def test_matrix_expands_to_n_specs(tmp_path):
    f = tmp_path / "feeds.yaml"
    _write(f, {
        "matrix": [
            {"slug": "tech"},
            {"slug": "bio"},
            {"slug": "art"},
        ],
        "source": {"kind": "http", "config": {"url": "https://x/{{ slug }}"}},
    })
    specs = _specs_from_workflow_file(f)
    assert len(specs) == 3
    assert [s.source.config["url"] for s in specs] == [
        "https://x/tech", "https://x/bio", "https://x/art",
    ]


def test_for_each_is_an_alias(tmp_path):
    f = tmp_path / "feeds.yaml"
    _write(f, {
        "for_each": [{"n": "a"}, {"n": "b"}],
        "source": {"kind": "http", "config": {"url": "https://x/{{ n }}"}},
    })
    specs = _specs_from_workflow_file(f)
    assert [s.source.config["url"] for s in specs] == ["https://x/a", "https://x/b"]


def test_per_entry_substitution_reaches_every_section(tmp_path):
    f = tmp_path / "search.yaml"
    _write(f, {
        "id": "search-{{ slug }}",
        "matrix": [{"slug": "gpu", "q": "rtx", "floor": 800}],
        "source": {"kind": "api", "config": {"url": "https://api/?q={{ q }}"}},
        "transforms": [{"kind": "select", "config": {"where": {"tag": "{{ slug }}"}}}],
        "sinks": [{"kind": "jsonl", "config": {"path": "/data/{{ slug }}.jsonl"}}],
        "schedule": {"mode": "cron", "cron": "*/{{ floor }} * * * *"},
    })
    (spec,) = _specs_from_workflow_file(f)
    assert spec.id == "search-gpu"
    assert spec.source.config["url"] == "https://api/?q=rtx"
    assert spec.transforms[0].config["where"]["tag"] == "gpu"
    assert spec.sinks[0].config["path"] == "/data/gpu.jsonl"
    assert spec.schedule.cron == "*/800 * * * *"


def test_whole_value_placeholder_preserves_type(tmp_path):
    f = tmp_path / "typed.yaml"
    _write(f, {
        "matrix": [{"floor": 500, "hdrs": {"X": "1"}}],
        "source": {"kind": "api", "config": {"min_price": "{{ floor }}",
                                             "headers": "{{ hdrs }}"}},
        "schedule": {"mode": "adaptive", "base": "{{ floor }}"},
    })
    (spec,) = _specs_from_workflow_file(f)
    # exact-placeholder strings keep the variable's native type
    assert spec.source.config["min_price"] == 500
    assert spec.source.config["headers"] == {"X": "1"}
    assert spec.schedule.base == 500.0  # coerced to float by ScheduleSpec


def test_unknown_placeholder_left_verbatim(tmp_path):
    f = tmp_path / "feeds.yaml"
    _write(f, {
        "matrix": [{"slug": "tech"}],
        "source": {"kind": "http", "config": {"url": "https://x/{{ slug }}/{{ nope }}"}},
    })
    (spec,) = _specs_from_workflow_file(f)
    assert spec.source.config["url"] == "https://x/tech/{{ nope }}"


# ── stable, distinct ids ───────────────────────────────────────────────────── #
def test_explicit_id_template_yields_stable_distinct_ids(tmp_path):
    f = tmp_path / "feeds.yaml"
    _write(f, {
        "id": "feed-{{ slug }}",
        "name": "Feed {{ slug }}",
        "matrix": [{"slug": "tech"}, {"slug": "bio"}],
        "source": {"kind": "http", "config": {"url": "https://x/{{ slug }}"}},
    })
    specs = _specs_from_workflow_file(f)
    assert [s.id for s in specs] == ["feed-tech", "feed-bio"]
    assert [s.name for s in specs] == ["Feed tech", "Feed bio"]
    # reloading the same file gives the same ids -> upsert, not duplicate
    again = _specs_from_workflow_file(f)
    assert [s.id for s in again] == [s.id for s in specs]


def test_default_ids_use_stem_index(tmp_path):
    f = tmp_path / "feeds.yaml"
    _write(f, {
        "matrix": [{"slug": "tech"}, {"slug": "bio"}],
        "source": {"kind": "http", "config": {"url": "https://x/{{ slug }}"}},
    })
    specs = _specs_from_workflow_file(f)
    assert [s.id for s in specs] == ["feeds-0", "feeds-1"]
    assert [s.name for s in specs] == ["feeds-0", "feeds-1"]


def test_static_id_collision_is_rejected(tmp_path):
    f = tmp_path / "feeds.yaml"
    _write(f, {
        "id": "constant",  # no varying var -> all entries collide
        "matrix": [{"slug": "tech"}, {"slug": "bio"}],
        "source": {"kind": "http", "config": {"url": "https://x/{{ slug }}"}},
    })
    with pytest.raises(ValueError, match="duplicate id"):
        _specs_from_workflow_file(f)


@pytest.mark.parametrize("bad", [
    {"matrix": [], "source": {"kind": "http", "config": {}}},
    {"matrix": "nope", "source": {"kind": "http", "config": {}}},
    {"matrix": ["nope"], "source": {"kind": "http", "config": {}}},
    {"matrix": [{}], "for_each": [{}], "source": {"kind": "http", "config": {}}},
])
def test_malformed_matrix_raises(tmp_path, bad):
    f = tmp_path / "feeds.yaml"
    _write(f, bad)
    with pytest.raises(ValueError):
        _specs_from_workflow_file(f)


# ── additive: matrix-free files are unchanged ──────────────────────────────── #
def test_no_matrix_single_job_unchanged(tmp_path):
    spec_in = {
        "source": {"kind": "http", "config": {"url": "https://example.com/"}},
        "sinks": [{"kind": "sqlite", "config": {}}],
        "schedule": {"mode": "once"},
    }
    f = tmp_path / "page.yaml"
    _write(f, spec_in)
    (spec,) = _specs_from_workflow_file(f)
    assert spec.id == "page" and spec.name == "page"
    assert spec.source.config["url"] == "https://example.com/"
    assert not _has_matrix(spec_in)


def test_no_matrix_jobs_list_unchanged(tmp_path):
    f = tmp_path / "multi.yaml"
    _write(f, {"jobs": [
        {"source": {"kind": "http", "config": {"url": "https://a/"}}},
        {"source": {"kind": "http", "config": {"url": "https://b/"}}},
    ]})
    specs = _specs_from_workflow_file(f)
    assert [s.id for s in specs] == ["multi-0", "multi-1"]
    # entries pass through object-identical (no copy/transform when no matrix)
    raw = [{"a": 1}, {"b": 2}]
    assert _expand_matrix_entries(raw, "stem") is raw


# ── matrix inside a jobs: list, mixed with plain entries ───────────────────── #
def test_matrix_entry_within_jobs_list(tmp_path):
    f = tmp_path / "mix.yaml"
    _write(f, {"jobs": [
        {"source": {"kind": "http", "config": {"url": "https://plain/"}}},
        {
            "matrix": [{"slug": "x"}, {"slug": "y"}],
            "source": {"kind": "http", "config": {"url": "https://m/{{ slug }}"}},
        },
    ]})
    specs = _specs_from_workflow_file(f)
    ids = [s.id for s in specs]
    # plain entry keeps its <stem>-<index> id; matrix entry fans out under its own
    assert ids == ["mix-0", "mix-1-0", "mix-1-1"]
    urls = [s.source.config["url"] for s in specs]
    assert urls == ["https://plain/", "https://m/x", "https://m/y"]


# ── composes with Track-1 defaults (substitute into the merged result) ─────── #
def test_substitutes_into_a_defaults_merged_template(tmp_path):
    # Mirror Track 1's deep-merge: `defaults:` is merged *under* the entry before
    # matrix runs, so vars must substitute into the merged structure. We build the
    # merged entry here (Track 1 owns the merge step) and expand it.
    merged = {
        "id": "feed-{{ slug }}",
        "matrix": [{"slug": "tech", "host": "tech.example"}],
        # these fields would have come from a shared `defaults:` block
        "source": {"kind": "http", "config": {"url": "https://{{ host }}/{{ slug }}"}},
        "sinks": [{"kind": "sqlite", "config": {}}],
        "schedule": {"mode": "adaptive", "base": 600},
    }
    (entry,) = _expand_matrix_entries([merged], "feeds")
    assert entry["id"] == "feed-tech"
    assert entry["source"]["config"]["url"] == "https://tech.example/tech"
    # default-provided sink/schedule survive substitution untouched
    assert entry["sinks"] == [{"kind": "sqlite", "config": {}}]
    assert entry["schedule"] == {"mode": "adaptive", "base": 600}


def test_matrix_composes_with_defaults_block_end_to_end(tmp_path):
    # A real workflow file carrying BOTH a Track-1 top-level `defaults:` block and a
    # `matrix:` fan-out, driven through the full loader: defaults are deep-merged
    # *under* the template first, then each entry's vars substitute into the merged
    # result — including a `{{ slug }}` placeholder that lives inside the shared
    # `defaults:`, which must resolve per entry.
    f = tmp_path / "feeds.yaml"
    _write(f, {
        "defaults": {
            "source": {"kind": "http", "config": {"method": "GET"}},
            "sinks": [{"kind": "jsonl", "config": {"path": "/data/{{ slug }}.jsonl"}}],
            "schedule": {"mode": "adaptive", "base": 1800},
        },
        "id": "feed-{{ slug }}",
        "matrix": [{"slug": "tech"}, {"slug": "bio"}],
        "source": {"config": {"url": "https://x/{{ slug }}"}},
    })
    specs = _specs_from_workflow_file(f)
    assert [s.id for s in specs] == ["feed-tech", "feed-bio"]
    # default source kind/method inherited; per-entry url merged in and substituted
    assert all(s.source.kind == "http" for s in specs)
    assert specs[0].source.config == {"method": "GET", "url": "https://x/tech"}
    # a `{{ slug }}` placeholder that lived in the shared defaults resolves per entry
    assert specs[0].sinks[0].config["path"] == "/data/tech.jsonl"
    assert specs[1].sinks[0].config["path"] == "/data/bio.jsonl"
    # default schedule survives substitution untouched
    assert specs[0].schedule.base == 1800.0


def test_subst_vars_is_a_pure_deep_copy(tmp_path):
    template = {"a": {"b": ["{{ x }}", "lit"]}, "n": "{{ x }}"}
    out = _subst_vars(template, {"x": 7})
    assert out == {"a": {"b": [7, "lit"]}, "n": 7}
    # original untouched
    assert template == {"a": {"b": ["{{ x }}", "lit"]}, "n": "{{ x }}"}


# ── end-to-end: reloading the same template upserts, never duplicates ──────── #
def test_reload_upserts_matrix_jobs(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ujin.jobs.app import create_jobs_app

    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    wf = tmp_path / "workflows"
    wf.mkdir()
    _write(wf / "feeds.yaml", {
        "id": "feed-{{ slug }}",
        "matrix": [{"slug": "tech"}, {"slug": "bio"}],
        "source": {"kind": "command", "config": {"argv": ["printf", "{{ slug }}"]}},
        "sinks": [{"kind": "sqlite", "config": {}}],
        "schedule": {"mode": "once"},
    })

    with TestClient(create_jobs_app(run_engine=False, workflows_dir=str(wf))) as c:
        first = sorted(j["id"] for j in c.get("/jobs").json())
        assert c.get("/health").json()["workflows"]["loaded"] == ["feed-tech", "feed-bio"]
    # second startup: same db + same file -> same two jobs, no duplicates
    with TestClient(create_jobs_app(run_engine=False, workflows_dir=str(wf))) as c:
        second = sorted(j["id"] for j in c.get("/jobs").json())
    assert first == second == ["feed-bio", "feed-tech"]
