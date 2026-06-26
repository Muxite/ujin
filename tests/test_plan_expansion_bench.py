"""Correctness + wall-clock tests for large-plan expansion.

Verifies that expanding ~100-entry plans produces the expected job count and
completes well within a generous wall-clock bound (< 2 s on CI hardware).
Also regression-guards that small single-file workflows and plans that share
an include: fragment expand to byte-for-byte identical resolved jobs after the
fragment-caching refactor.
"""
from __future__ import annotations

import time

import pytest
import yaml

from ujin.jobs.app import _load_ingest_plan, _specs_from_workflow_file

N = 100


# ── fixtures ──────────────────────────────────────────────────────────────── #

@pytest.fixture
def matrix_plan(tmp_path):
    plan = {
        "defaults": {"schedule": {"mode": "once"}},
        "jobs": [
            {
                "matrix": [{"slug": f"t-{i:03d}"} for i in range(N)],
                "id": "feed-{{ slug }}",
                "source": {
                    "kind": "http",
                    "config": {"url": "https://api.example.com/{{ slug }}"},
                },
            }
        ],
    }
    p = tmp_path / "big_matrix.yaml"
    p.write_text(yaml.safe_dump(plan), encoding="utf-8")
    return p


@pytest.fixture
def shared_include_plan(tmp_path):
    frags = tmp_path / "frags"
    frags.mkdir()
    (frags / "source.yaml").write_text(
        yaml.safe_dump({
            "schedule": {"mode": "once"},
            "source": {"kind": "http", "config": {"url": "https://example.com"}},
        }),
        encoding="utf-8",
    )
    plan = {
        "jobs": [
            {
                "include": "frags/source.yaml",
                "id": f"job-{i:03d}",
                "name": f"job-{i:03d}",
            }
            for i in range(N)
        ]
    }
    p = tmp_path / "big_include.yaml"
    p.write_text(yaml.safe_dump(plan), encoding="utf-8")
    return p


# ── correctness: count and ids ─────────────────────────────────────────────── #

def test_matrix_100_count_and_ids(matrix_plan):
    specs = _load_ingest_plan(matrix_plan)
    assert len(specs) == N
    assert [s.id for s in specs] == [f"feed-t-{i:03d}" for i in range(N)]


def test_matrix_100_source_url_substituted(matrix_plan):
    specs = _load_ingest_plan(matrix_plan)
    assert specs[0].source.config["url"] == "https://api.example.com/t-000"
    assert specs[99].source.config["url"] == "https://api.example.com/t-099"


def test_shared_include_100_count(shared_include_plan):
    specs = _load_ingest_plan(shared_include_plan)
    assert len(specs) == N
    assert [s.id for s in specs] == [f"job-{i:03d}" for i in range(N)]


def test_shared_include_source_resolved(shared_include_plan):
    specs = _load_ingest_plan(shared_include_plan)
    for spec in specs:
        assert spec.source.kind == "http"
        assert spec.source.config["url"] == "https://example.com"


# ── wall-clock bounds (generous — must pass on slow CI) ───────────────────── #

def test_matrix_100_wall_clock(matrix_plan):
    t0 = time.perf_counter()
    specs = _load_ingest_plan(matrix_plan)
    elapsed = time.perf_counter() - t0
    assert len(specs) == N
    assert elapsed < 2.0, f"matrix expansion took {elapsed:.3f}s, expected < 2s"


def test_shared_include_100_wall_clock(shared_include_plan):
    t0 = time.perf_counter()
    specs = _load_ingest_plan(shared_include_plan)
    elapsed = time.perf_counter() - t0
    assert len(specs) == N
    assert elapsed < 2.0, f"shared-include expansion took {elapsed:.3f}s, expected < 2s"


# ── regression: small workflows must expand identically after the refactor ─── #

def test_small_plan_unchanged(tmp_path):
    plan = {
        "defaults": {"schedule": {"mode": "once"}},
        "jobs": [
            {"id": "alpha", "source": {"kind": "http", "config": {"url": "https://a.example/"}}},
            {"id": "beta", "source": {"kind": "http", "config": {"url": "https://b.example/"}}},
        ],
    }
    p = tmp_path / "small.yaml"
    p.write_text(yaml.safe_dump(plan), encoding="utf-8")
    specs = _load_ingest_plan(p)
    assert [s.id for s in specs] == ["alpha", "beta"]
    assert specs[0].source.config["url"] == "https://a.example/"
    assert specs[1].source.config["url"] == "https://b.example/"
    assert specs[0].schedule.mode == "once"


def test_single_include_fragment_unchanged(tmp_path):
    frags = tmp_path / "frags"
    frags.mkdir()
    (frags / "base.yaml").write_text(
        yaml.safe_dump({
            "schedule": {"mode": "once"},
            "source": {"kind": "http", "config": {"url": "https://base.example/"}},
        }),
        encoding="utf-8",
    )
    plan = {
        "jobs": [{"include": "frags/base.yaml", "id": "job-a", "name": "job-a"}]
    }
    p = tmp_path / "single_inc.yaml"
    p.write_text(yaml.safe_dump(plan), encoding="utf-8")
    specs = _load_ingest_plan(p)
    assert len(specs) == 1
    assert specs[0].id == "job-a"
    assert specs[0].source.kind == "http"
    assert specs[0].source.config["url"] == "https://base.example/"


def test_workflow_file_matrix_unchanged(tmp_path):
    """_specs_from_workflow_file must also expand correctly (it passes _cache={} now)."""
    plan = {
        "jobs": [
            {
                "matrix": [{"slug": "a"}, {"slug": "b"}, {"slug": "c"}],
                "id": "feed-{{ slug }}",
                "source": {
                    "kind": "http",
                    "config": {"url": "https://example.com/{{ slug }}"},
                },
                "schedule": {"mode": "once"},
            }
        ]
    }
    p = tmp_path / "wf.yaml"
    p.write_text(yaml.safe_dump(plan), encoding="utf-8")
    specs = _specs_from_workflow_file(p)
    assert [s.id for s in specs] == ["feed-a", "feed-b", "feed-c"]
    assert specs[1].source.config["url"] == "https://example.com/b"
