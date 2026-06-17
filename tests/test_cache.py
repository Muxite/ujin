"""Memory LRU+TTL cache, SQLite disk cache, and per-host cooldown policy."""
from __future__ import annotations

import sqlite3
import time

from ujin.cache import CachedEntry, HostPolicy, ScrapeCache
from ujin.cache.disk import DiskCache


def _entry(url="https://x.test/", fp="fp", payload=None) -> CachedEntry:
    return CachedEntry(url=url, fingerprint=fp,
                       payload=payload if payload is not None else {"links": []},
                       fetched_at=time.monotonic())


# ── memory cache ─────────────────────────────────────────────────────────────

def test_memory_roundtrip_and_hits():
    c = ScrapeCache()
    c.put("k", _entry())
    got = c.get("k")
    assert got is not None and got.hits == 1
    c.get("k")
    assert c.get("k").hits == 3


def test_memory_miss_returns_none():
    assert ScrapeCache().get("nope") is None


def test_memory_ttl_eviction():
    c = ScrapeCache(ttl_secs=10)
    stale = CachedEntry(url="u", fingerprint="f", payload={},
                        fetched_at=time.monotonic() - 11)
    c.put("k", stale)
    assert c.get("k") is None
    assert c.stats()["entries"] == 0  # evicted on read


def test_memory_lru_eviction_order():
    c = ScrapeCache(max_entries=2)
    c.put("a", _entry(fp="a"))
    c.put("b", _entry(fp="b"))
    c.get("a")              # touch "a" so "b" is the LRU victim
    c.put("c", _entry(fp="c"))
    assert c.get("a") is not None
    assert c.get("b") is None
    assert c.get("c") is not None


def test_memory_invalidate():
    c = ScrapeCache()
    c.put("k", _entry())
    c.invalidate("k")
    assert c.get("k") is None
    c.invalidate("k")  # idempotent


def test_memory_items_snapshot():
    c = ScrapeCache()
    c.put("k1", _entry(fp="1"))
    c.put("k2", _entry(fp="2"))
    assert {k for k, _ in c.items()} == {"k1", "k2"}


# ── disk cache ───────────────────────────────────────────────────────────────

def test_disk_roundtrip(tmp_path):
    db = DiskCache(tmp_path / "cache.db")
    db.put("k", _entry(payload={"links": [1, 2, 3]}, fp="v1"))
    got = db.get("k")
    assert got.fingerprint == "v1"
    assert got.payload == {"links": [1, 2, 3]}
    assert db.contains("k") and not db.contains("other")
    db.close()


def test_disk_upsert_overwrites(tmp_path):
    db = DiskCache(tmp_path / "cache.db")
    db.put("k", _entry(fp="v1"))
    db.put("k", _entry(fp="v2", payload={"n": 2}))
    got = db.get("k")
    assert got.fingerprint == "v2" and got.payload == {"n": 2}
    db.close()


def test_disk_persists_across_reopen(tmp_path):
    path = tmp_path / "cache.db"
    db1 = DiskCache(path)
    db1.put("k", _entry(fp="kept"))
    db1.close()
    db2 = DiskCache(path)
    assert db2.get("k").fingerprint == "kept"
    db2.close()


def test_disk_corrupt_payload_dropped_not_raised(tmp_path):
    path = tmp_path / "cache.db"
    db = DiskCache(path)
    db.put("good", _entry(fp="g"))
    # corrupt one row's pickle blob directly
    conn = sqlite3.connect(str(path))
    conn.execute(
        "INSERT INTO cache (key, url, fingerprint, fetched_wall, payload) "
        "VALUES ('bad', 'u', 'f', 0, X'DEADBEEF')"
    )
    conn.commit()
    conn.close()
    assert db.get("bad") is None
    loaded = dict(db.load_all())
    assert "good" in loaded and "bad" not in loaded
    db.close()


def test_disk_unpicklable_payload_skipped(tmp_path):
    db = DiskCache(tmp_path / "cache.db")
    db.put("k", _entry(payload=lambda: None))  # lambdas don't pickle
    assert db.get("k") is None
    db.close()


def test_disk_flush_from_bulk(tmp_path):
    db = DiskCache(tmp_path / "cache.db")
    mem = ScrapeCache()
    mem.put("a", _entry(fp="a"))
    mem.put("b", _entry(fp="b"))
    db.flush_from(mem.items())
    assert db.get("a").fingerprint == "a"
    assert db.get("b").fingerprint == "b"
    db.close()


def test_disk_creates_parent_dirs(tmp_path):
    db = DiskCache(tmp_path / "deep" / "nested" / "cache.db")
    db.put("k", _entry())
    assert db.get("k") is not None
    db.close()


def test_disk_uses_wal_journal_mode(tmp_path):
    """WAL mode is what gives the fast per-put commit; assert it's active."""
    path = tmp_path / "cache.db"
    db = DiskCache(path)
    mode = db._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    sync = db._conn.execute("PRAGMA synchronous").fetchone()[0]
    assert sync == 1  # NORMAL
    db.close()


def test_disk_close_checkpoints_wal(tmp_path):
    """After close, committed data must be readable from the main DB file
    alone (WAL folded back), even with the sidecar files removed."""
    path = tmp_path / "cache.db"
    db = DiskCache(path)
    db.put("k", _entry(fp="durable"))
    db.close()
    # Remove WAL sidecars; the truncating checkpoint on close should have
    # consolidated everything into the main file.
    for suffix in ("-wal", "-shm"):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.exists():
            sidecar.unlink()
    reopened = DiskCache(path)
    assert reopened.get("k").fingerprint == "durable"
    reopened.close()


def test_disk_durable_across_reopen_without_clean_close(tmp_path):
    """Committed puts survive even if the process never calls close()
    (simulated by abandoning the connection)."""
    path = tmp_path / "cache.db"
    db = DiskCache(path)
    db.put("k", _entry(fp="committed"))
    # Drop the reference without close() — the WAL holds the committed row.
    del db
    reopened = DiskCache(path)
    assert reopened.get("k").fingerprint == "committed"
    reopened.close()


# ── host policy ──────────────────────────────────────────────────────────────

def test_policy_no_cooldown_initially():
    assert HostPolicy().cooldown_remaining("https://x.test/a") == 0.0


def test_policy_failure_sets_cooldown_per_host():
    p = HostPolicy(cooldown_secs=60)
    p.record_failure("https://x.test/a")
    assert 0 < p.cooldown_remaining("https://x.test/other-path") <= 60
    # different host unaffected
    assert p.cooldown_remaining("https://y.test/") == 0.0


def test_policy_exponential_growth_capped():
    p = HostPolicy(cooldown_secs=10)
    url = "https://x.test/"
    for _ in range(2):
        p.record_failure(url)
    assert p.cooldown_remaining(url) <= 20
    for _ in range(10):
        p.record_failure(url)
    # capped at 8x base
    assert p.cooldown_remaining(url) <= 80


def test_policy_success_resets():
    p = HostPolicy(cooldown_secs=60)
    url = "https://x.test/"
    p.record_failure(url)
    p.record_success(url)
    assert p.cooldown_remaining(url) == 0.0


def test_policy_host_matching_case_insensitive():
    p = HostPolicy(cooldown_secs=60)
    p.record_failure("https://X.TEST/a")
    assert p.cooldown_remaining("https://x.test/b") > 0
