"""Durable per-host SiteStore (ujin/adapt/site_store.py).

Offline + deterministic: SQLite on tmp_path or :memory:, with an injected clock
so ``last_seen`` timestamping needs no real wall clock.
"""
from __future__ import annotations

import threading

import ujin.adapt as adapt
from ujin.adapt import HostRecord, SiteStore


# ── exports (additive surface) ───────────────────────────────────────────────

def test_names_exported():
    assert "SiteStore" in adapt.__all__
    assert "HostRecord" in adapt.__all__
    # pre-existing exports must all survive (additive-only)
    for name in ("AdaptiveInterval", "Backoff", "CircuitBreaker",
                 "TokenBucket", "AIMDLimiter", "jitter"):
        assert name in adapt.__all__


# ── hosts() enumeration (additive read surface) ──────────────────────────────

def test_hosts_enumerates_persisted_hosts():
    s = SiteStore()  # :memory:
    assert s.hosts() == []                       # never-written store
    s.record("b.test", status=200)
    s.record("a.test", latency=0.5)
    assert set(s.hosts()) == {"a.test", "b.test"}
    assert s.hosts() == ["a.test", "b.test"]     # sorted for stable output
    s.close()


# ── defaults ─────────────────────────────────────────────────────────────────

def test_unknown_host_returns_zero_record():
    s = SiteStore()  # :memory:
    rec = s.get("never.seen")
    assert isinstance(rec, HostRecord)
    assert rec.host == "never.seen"
    assert rec.last_status == 0
    assert rec.last_latency == 0.0
    assert rec.p50_latency == 0.0
    assert rec.error_count == 0
    assert rec.rate_limit_count == 0
    assert rec.crawl_delay == 0.0
    assert rec.interval == 0.0
    assert rec.last_seen == 0.0
    s.close()


# ── record() merge semantics ─────────────────────────────────────────────────

def test_record_sets_gauges_and_returns_row():
    s = SiteStore()
    rec = s.record("a.test", status=200, crawl_delay=2.5, interval=42.0)
    assert rec.last_status == 200
    assert rec.crawl_delay == 2.5
    assert rec.interval == 42.0
    # persisted: a fresh get sees the same values
    assert s.get("a.test").last_status == 200
    s.close()


def test_record_gauges_overwrite_not_accumulate():
    s = SiteStore()
    s.record("a.test", status=200, interval=10.0)
    s.record("a.test", status=503, interval=20.0)
    rec = s.get("a.test")
    assert rec.last_status == 503
    assert rec.interval == 20.0
    s.close()


def test_record_counters_accumulate():
    s = SiteStore()
    s.record("a.test", error=True)
    s.record("a.test", error=True, rate_limited=True)
    s.record("a.test", rate_limited=2)  # explicit delta
    rec = s.get("a.test")
    assert rec.error_count == 2
    assert rec.rate_limit_count == 3
    s.close()


def test_record_partial_signals_preserve_other_fields():
    s = SiteStore()
    s.record("a.test", status=200, crawl_delay=5.0)
    s.record("a.test", error=True)  # only touches error_count
    rec = s.get("a.test")
    assert rec.last_status == 200      # preserved
    assert rec.crawl_delay == 5.0      # preserved
    assert rec.error_count == 1
    s.close()


def test_p50_and_last_latency():
    s = SiteStore()
    for lat in (0.1, 0.5, 0.3):
        s.record("a.test", latency=lat)
    rec = s.get("a.test")
    assert rec.last_latency == 0.3       # most recent sample
    assert rec.p50_latency == 0.3        # median of {0.1, 0.3, 0.5}
    s.close()


def test_unknown_signal_raises():
    s = SiteStore()
    try:
        s.record("a.test", bogus=1)
    except ValueError as exc:
        assert "bogus" in str(exc)
    else:  # pragma: no cover - must raise
        raise AssertionError("expected ValueError for unknown signal")
    s.close()


# ── injected clock ───────────────────────────────────────────────────────────

def test_injected_clock_stamps_last_seen(fake_clock):
    fake_clock.advance(1_000.0)
    s = SiteStore(clock=fake_clock)
    rec = s.record("a.test", status=200)
    assert rec.last_seen == 1_000.0
    fake_clock.advance(50.0)
    rec2 = s.record("a.test", status=200)
    assert rec2.last_seen == 1_050.0
    assert s.get("a.test").last_seen == 1_050.0
    s.close()


# ── durability ───────────────────────────────────────────────────────────────

def test_durable_across_close_and_reopen(tmp_path, fake_clock):
    path = tmp_path / "site.db"
    fake_clock.advance(123.0)
    s1 = SiteStore(path, clock=fake_clock)
    s1.record("a.test", status=200, latency=0.4, crawl_delay=10.0,
              interval=60.0, error=True, rate_limited=True)
    s1.close()

    s2 = SiteStore(path, clock=fake_clock)
    rec = s2.get("a.test")
    assert rec.last_status == 200
    assert rec.last_latency == 0.4
    assert rec.p50_latency == 0.4
    assert rec.crawl_delay == 10.0
    assert rec.interval == 60.0
    assert rec.error_count == 1
    assert rec.rate_limit_count == 1
    assert rec.last_seen == 123.0
    s2.close()


def test_durable_with_wal_sidecars_removed(tmp_path):
    """The truncating checkpoint on close folds the WAL into the main file."""
    path = tmp_path / "site.db"
    s = SiteStore(path)
    s.record("a.test", status=204)
    s.close()
    for suffix in ("-wal", "-shm"):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.exists():
            sidecar.unlink()
    reopened = SiteStore(path)
    assert reopened.get("a.test").last_status == 204
    reopened.close()


def test_uses_wal_journal_mode(tmp_path):
    path = tmp_path / "site.db"
    s = SiteStore(path)
    mode = s._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    sync = s._conn.execute("PRAGMA synchronous").fetchone()[0]
    assert sync == 1  # NORMAL
    s.close()


def test_creates_parent_dirs(tmp_path):
    s = SiteStore(tmp_path / "deep" / "nested" / "site.db")
    s.record("a.test", status=200)
    assert s.get("a.test").last_status == 200
    s.close()


# ── concurrency ──────────────────────────────────────────────────────────────

def test_threaded_record_upserts_dont_lose_increments(tmp_path):
    path = tmp_path / "site.db"
    s = SiteStore(path)
    threads_n, per_thread = 8, 50

    def worker():
        for _ in range(per_thread):
            s.record("a.test", error=True, rate_limited=True)

    threads = [threading.Thread(target=worker) for _ in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    rec = s.get("a.test")
    assert rec.error_count == threads_n * per_thread
    assert rec.rate_limit_count == threads_n * per_thread
    s.close()
