"""Plan-expansion benchmarks — the many-job hot path.

Measures two shapes that both produce 120+ resolved JobSpecs:
  • one job template expanded by a 120-entry inline matrix (the typical
    large-plan shape: shared defaults resolved once, then 120x _subst_vars)
  • 120 separate job entries that all share the same include: fragment
    (exercises the per-call fragment cache added in the same change)

Run with:  make bench   (or  pytest benchmarks/ --benchmark-only)
Compare:   medians land in .benchmarks/; the async suite has its own
           baseline.json (see _aio.py).
"""
from __future__ import annotations

import pytest
import yaml

from ujin.jobs.app import _load_ingest_plan

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
