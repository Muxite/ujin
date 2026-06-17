"""Synchronous hot paths via pytest-benchmark.

Run:    pytest benchmarks/ --benchmark-only
Compare: medians land in .benchmarks/; the async suite keeps its own
         baseline.json (see _aio.py).
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from ujin.cache import CachedEntry, ScrapeCache
from ujin.cache.disk import DiskCache
from ujin.extract.links import extract_headline_links, fingerprint_links
from ujin.poll.base import fingerprint
from ujin.scrape.routes import _decode_cursor, _encode_cursor

FIXTURES = Path(__file__).parent.parent / "tests" / "fixtures" / "html"

PAYLOAD_10KB = "x" * 10_240
PAYLOAD_1MB = "x" * 1_048_576
PAYLOAD_JSON = {"items": [{"id": i, "title": f"item {i}"} for i in range(500)]}


@pytest.fixture(scope="module")
def news_html() -> str:
    return (FIXTURES / "news_index.html").read_text()


def test_fingerprint_10kb(benchmark):
    benchmark(fingerprint, PAYLOAD_10KB)


def test_fingerprint_1mb(benchmark):
    benchmark(fingerprint, PAYLOAD_1MB)


def test_fingerprint_json_payload(benchmark):
    benchmark(fingerprint, PAYLOAD_JSON)


def test_extract_links_news_index(benchmark, news_html):
    links = benchmark(extract_headline_links, news_html,
                      base_url="https://news.example.com/")
    assert len(links) >= 20  # sanity: the benchmark measured real work


def test_fingerprint_links(benchmark, news_html):
    links = extract_headline_links(news_html, base_url="https://news.example.com/")
    benchmark(fingerprint_links, links)


def test_cursor_roundtrip(benchmark):
    def roundtrip():
        c = _encode_cursor(40, "fingerprint-value")
        return _decode_cursor(c)

    offset, fp = benchmark(roundtrip)
    assert offset == 40


def test_disk_cache_put(benchmark, tmp_path):
    """Raw per-put commit throughput — isolates the SQLite commit path that
    WAL mode accelerates (the async roundtrip bench is dominated by
    ``asyncio.to_thread`` overhead and hides this win)."""
    db = DiskCache(tmp_path / "bench.db")
    entry = CachedEntry(url="u", fingerprint="f",
                        payload={"links": [{"u": i} for i in range(100)]},
                        fetched_at=time.monotonic())
    counter = {"n": 0}

    def put_one():
        counter["n"] += 1
        # vary the key so we exercise both INSERT and UPDATE branches
        db.put(f"k{counter['n'] % 64}", entry)

    benchmark(put_one)
    db.close()


def test_memory_cache_put_get(benchmark):
    cache = ScrapeCache(max_entries=1024)
    entry = CachedEntry(url="u", fingerprint="f", payload={"links": list(range(50))},
                        fetched_at=time.monotonic())

    def put_get():
        cache.put("k", entry)
        return cache.get("k")

    assert benchmark(put_get) is not None
