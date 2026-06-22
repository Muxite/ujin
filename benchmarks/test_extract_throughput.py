"""CPU-bound extraction throughput — per-extractor and per-poll cost.

Measures each extraction function individually on representative fixture HTML,
then a combined per-poll pass (all four extractors, fetch excluded) to establish
the single-process ceiling for multiprocessing Track 3 analysis.

Run:        pytest benchmarks/ -q --no-cov --benchmark-disable-gc
Re-record:  UJIN_BENCH_RECORD=1 pytest benchmarks/ -q --no-cov --benchmark-disable-gc

All results land in benchmarks/baseline.json and are guarded against
regression at 4x tolerance (order-of-magnitude tripwire).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ujin.extract.article import extract_article
from ujin.extract.links import extract_headline_links
from ujin.extract.structured import extract_structured
from ujin.extract.tables import extract_tables

from _aio import abench, check_against_baseline, record

FIXTURES = Path(__file__).parent.parent / "tests" / "fixtures" / "html"

_ARTICLE_URL = (
    "https://news.example.com/2026/06/08/quantum-error-correction-milestone"
)


@pytest.fixture(scope="module")
def article_html() -> str:
    return (FIXTURES / "article.html").read_text()


@pytest.fixture(scope="module")
def news_html() -> str:
    return (FIXTURES / "news_index.html").read_text()


@pytest.fixture(scope="module")
def tables_html() -> str:
    return (FIXTURES / "tables.html").read_text()


async def test_extract_headline_links_throughput(news_html):
    """extract_headline_links on a representative news front page."""
    base_url = "https://news.example.com/"

    async def fn():
        extract_headline_links(news_html, base_url=base_url)

    r = await abench("extract_headline_links", fn, iterations=500, warmup=10)
    print(
        f"\n  extract_headline_links  "
        f"{1.0 / r.mean_s:>10,.0f} events/sec  "
        f"{r.median_s * 1000:.3f} ms/page"
    )
    record([r])
    check_against_baseline(r)


async def test_extract_article_throughput(article_html):
    """extract_article (trafilatura) on an article page."""

    async def fn():
        extract_article(article_html, url=_ARTICLE_URL)

    r = await abench("extract_article", fn, iterations=100, warmup=5)
    print(
        f"\n  extract_article         "
        f"{1.0 / r.mean_s:>10,.0f} events/sec  "
        f"{r.median_s * 1000:.3f} ms/page"
    )
    record([r])
    check_against_baseline(r)


async def test_extract_structured_throughput(article_html):
    """extract_structured (selectolax JSON-LD/OG/microdata) on an article page."""

    async def fn():
        extract_structured(article_html)

    r = await abench("extract_structured", fn, iterations=500, warmup=10)
    print(
        f"\n  extract_structured      "
        f"{1.0 / r.mean_s:>10,.0f} events/sec  "
        f"{r.median_s * 1000:.3f} ms/page"
    )
    record([r])
    check_against_baseline(r)


async def test_extract_tables_throughput(tables_html):
    """extract_tables (selectolax) on a page with representative tables."""

    async def fn():
        extract_tables(tables_html)

    r = await abench("extract_tables", fn, iterations=500, warmup=10)
    print(
        f"\n  extract_tables          "
        f"{1.0 / r.mean_s:>10,.0f} events/sec  "
        f"{r.median_s * 1000:.3f} ms/page"
    )
    record([r])
    check_against_baseline(r)


async def test_per_poll_extract_cost(article_html):
    """All four extractors in sequence on one page — fetch excluded.

    This is the CPU work a multiprocessing worker would do per fetched page:
    the single-process extraction ceiling, i.e. the event rate above which
    one core's extraction becomes the bottleneck. Used in the Track 3
    (multiprocessing) go/no-go analysis in docs/PERFORMANCE.md.
    """

    async def fn():
        extract_headline_links(article_html, base_url=_ARTICLE_URL)
        extract_article(article_html, url=_ARTICLE_URL)
        extract_structured(article_html)
        extract_tables(article_html)

    r = await abench("per_poll_extract_cost", fn, iterations=100, warmup=5)
    print(
        f"\n  per_poll_extract_cost   "
        f"{1.0 / r.mean_s:>10,.0f} events/sec  "
        f"{r.median_s * 1000:.3f} ms/page"
    )
    record([r])
    check_against_baseline(r)
