"""Tiny async benchmark runner — pytest-benchmark is sync-only.

`abench(fn, iterations=N)` runs an async callable N times after a warmup and
returns per-iteration stats in seconds. Used with plain asserts against the
committed baseline (see test_async_paths.py) with a generous tolerance, so CI
noise doesn't flake the job while order-of-magnitude regressions still fail.
"""
from __future__ import annotations

import asyncio
import json
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path

BASELINE_PATH = Path(__file__).parent / "baseline.json"
# CI boxes vary; only fail on >4x the committed baseline (an order-of-magnitude
# guard, not a microbenchmark gate).
TOLERANCE = 4.0


@dataclass
class BenchResult:
    name: str
    iterations: int
    mean_s: float
    median_s: float
    p95_s: float
    total_s: float


async def abench(name: str, fn, *, iterations: int = 20, warmup: int = 2) -> BenchResult:
    for _ in range(warmup):
        await fn()
    samples: list[float] = []
    t0 = time.perf_counter()
    for _ in range(iterations):
        s = time.perf_counter()
        await fn()
        samples.append(time.perf_counter() - s)
    total = time.perf_counter() - t0
    samples.sort()
    return BenchResult(
        name=name,
        iterations=iterations,
        mean_s=statistics.fmean(samples),
        median_s=statistics.median(samples),
        p95_s=samples[min(len(samples) - 1, int(len(samples) * 0.95))],
        total_s=total,
    )


def load_baseline() -> dict:
    if BASELINE_PATH.exists():
        return json.loads(BASELINE_PATH.read_text())
    return {}


def record(results: list[BenchResult]) -> None:
    """Print results and, when UJIN_BENCH_RECORD=1, rewrite the baseline."""
    import os

    print()
    for r in results:
        print(f"  {r.name:40s} median {r.median_s * 1000:8.3f} ms  "
              f"p95 {r.p95_s * 1000:8.3f} ms  ({r.iterations} iters)")
    if os.environ.get("UJIN_BENCH_RECORD") == "1":
        data = load_baseline()
        data.update({r.name: asdict(r) for r in results})
        BASELINE_PATH.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        print(f"  baseline rewritten: {BASELINE_PATH}")


def check_against_baseline(result: BenchResult) -> None:
    """Assert the median hasn't regressed past TOLERANCE x the baseline."""
    base = load_baseline().get(result.name)
    if base is None:
        return  # no baseline yet — record mode will create one
    limit = base["median_s"] * TOLERANCE
    assert result.median_s <= limit, (
        f"{result.name} regressed: median {result.median_s * 1000:.2f}ms "
        f"> {TOLERANCE}x baseline {base['median_s'] * 1000:.2f}ms"
    )
