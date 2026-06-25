"""Plan-expansion benchmarks — the many-job hot path.

Measures three shapes that all produce 120 resolved JobSpecs:
  * one job template expanded by a 120-entry inline matrix (the typical
    large-plan shape: shared defaults resolved once, then 120x _subst_vars)
  * 120 separate job entries that all share the same include: fragment
    (exercises the per-call fragment cache)
  * a combined plan that exercises every resolution layer in one pass:
    top-level defaults: deep-merge, include: splicing, matrix: fan-out,
    and per-entry {{ var }} substitution — the end-to-end throughput number
    reported in docs/PERFORMANCE.md

Run with:  make bench   (or  pytest benchmarks/ --benchmark-only)
Compare:   medians land in .benchmarks/; the async suite has its own
           baseline.json (see _aio.py).
"""
from __future__ import annotations

import pytest
import yaml

from ujin.jobs.app import _load_ingest_plan
from _aio import abench, check_against_baseline, record

N = 120  # clearly above the "100+" requirement


# ── fixtures (module-scoped: files created once per session) ──────────────── #

@pytest.fixture(scope="module")
def plan_matrix_inline(tmp_path_factory):
    """Single job template with a 120-entry inline matrix."""
    tmp = tmp_path_factory.mktemp("bench_matrix")
    plan = {
        "defaults": {"schedule": {"mode": "once"}},
        "jobs": [
            {
                "matrix": [
                    {"slug": f"topic-{i:03d}", "limit": i + 10}
                    for i in range(N)
                ],
                "id": "feed-{{ slug }}",
                "source": {
                    "kind": "http",
                    "config": {
                        "url": "https://api.example.com/feed/{{ slug }}?limit={{ limit }}"
                    },
                },
            }
        ],
    }
    p = tmp / "matrix_inline.yaml"
    p.write_text(yaml.safe_dump(plan), encoding="utf-8")
    return p


@pytest.fixture(scope="module")
def plan_shared_include(tmp_path_factory):
    """120 separate job entries that all include: the same fragment."""
    tmp = tmp_path_factory.mktemp("bench_include")
    frags = tmp / "frags"
    frags.mkdir()
    (frags / "source.yaml").write_text(
        yaml.safe_dump({
            "schedule": {"mode": "once"},
            "source": {
                "kind": "http",
                "config": {"url": "https://api.example.com/feed"},
            },
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
    p = tmp / "shared_include.yaml"
    p.write_text(yaml.safe_dump(plan), encoding="utf-8")
    return p


@pytest.fixture(scope="module")
def plan_full_expansion(tmp_path_factory):
    """Plan exercising every resolution layer -> 120 JobSpecs.

    Layers exercised:
      - top-level defaults: (schedule + enabled deep-merged into every job)
      - include: splicing (a shared fragment merged before matrix substitution)
      - matrix: fan-out (120 inline variable maps)
      - {{ var }} substitution in id, name, and source URL (per entry)
      - JobSpec.from_dict construction (once per fan-out entry)
    """
    tmp = tmp_path_factory.mktemp("bench_full")
    frags = tmp / "frags"
    frags.mkdir()
    (frags / "common.yaml").write_text(
        yaml.safe_dump({
            "transforms": [{"kind": "select", "config": {"key": "items"}}],
        }),
        encoding="utf-8",
    )
    plan = {
        "defaults": {
            "schedule": {"mode": "once"},
            "enabled": True,
        },
        "jobs": [
            {
                "include": "frags/common.yaml",
                "matrix": [
                    {"slug": f"topic-{i:03d}", "limit": i + 10}
                    for i in range(N)
                ],
                "id": "feed-{{ slug }}",
                "name": "Feed {{ slug }} (limit={{ limit }})",
                "source": {
                    "kind": "http",
                    "config": {
                        "url": "https://api.example.com/{{ slug }}?limit={{ limit }}"
                    },
                },
            }
        ],
    }
    p = tmp / "full_expansion.yaml"
    p.write_text(yaml.safe_dump(plan), encoding="utf-8")
    return p


# ── benchmarks ────────────────────────────────────────────────────────────── #

def test_expand_matrix_inline_120(benchmark, plan_matrix_inline):
    """120-entry inline matrix -> 120 JobSpecs.

    Hot path: _resolve_includes once, then 120x _subst_vars + JobSpec.from_dict.
    """
    specs = benchmark(_load_ingest_plan, plan_matrix_inline)
    assert len(specs) == N


def test_expand_shared_include_120(benchmark, plan_shared_include):
    """120 jobs sharing one include: fragment -> 120 JobSpecs.

    Hot path: fragment read once (cache hit for entries 1-119), then
    120x _deep_merge(defaults, entry) + JobSpec.from_dict.
    """
    specs = benchmark(_load_ingest_plan, plan_shared_include)
    assert len(specs) == N


async def test_expand_full_plan_120(plan_full_expansion):
    """End-to-end expansion through every resolution layer -> 120 JobSpecs.

    Exercises defaults: deep-merge, include: splicing, matrix: fan-out,
    {{ var }} substitution per entry, and JobSpec.from_dict construction —
    all in a single _load_ingest_plan call.  Reports jobs/sec and ms/job
    following the extraction-benchmark methodology (see test_extract_throughput.py).
    """
    assert len(_load_ingest_plan(plan_full_expansion)) == N  # correctness

    async def fn():
        _load_ingest_plan(plan_full_expansion)

    r = await abench("plan_expand_full_120", fn, iterations=50, warmup=5)
    print(
        f"\n  plan_expand_full_120  "
        f"{N / r.mean_s:>10,.0f} jobs/sec  "
        f"{r.median_s / N * 1000:.3f} ms/job"
    )
    record([r])
    check_against_baseline(r)
