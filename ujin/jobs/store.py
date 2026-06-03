"""SQLite-backed durable store for job specs + run history.

Modeled on :class:`ujin.cache.disk.DiskCache` — one ``sqlite3`` connection
(``check_same_thread=False``) guarded by a single :class:`threading.Lock`, schema
applied via ``executescript``. Unlike the cache, specs are stored as **JSON**, not
pickle: a JobSpec is plain JSON-able data, and JSON avoids executing arbitrary
pickled objects from an on-disk file.

This is what makes runtime-configured jobs survive a restart — the gap the old
in-memory ``POST /targets`` left open.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .model import JobSpec

logger = logging.getLogger("ujin.jobs.store")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    enabled    INTEGER NOT NULL DEFAULT 1,
    spec_json  TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS job_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL,
    started_at  REAL NOT NULL,
    finished_at REAL,
    ok          INTEGER NOT NULL,
    changed     INTEGER NOT NULL,
    fingerprint TEXT,
    error       TEXT,
    strategy    TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_job ON job_runs(job_id, started_at DESC);
CREATE TABLE IF NOT EXISTS job_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL,
    ts          REAL NOT NULL,
    fingerprint TEXT,
    event_json  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_job ON job_events(job_id, ts DESC);
"""


class JobStore:
    """Durable job specs + run history. Thread-safe via a single lock."""

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- job specs --------------------------------------------------------- #
    def upsert(self, spec: JobSpec) -> None:
        blob = json.dumps(spec.to_dict(), sort_keys=True)
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT INTO jobs (id, name, enabled, spec_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       name=excluded.name,
                       enabled=excluded.enabled,
                       spec_json=excluded.spec_json,
                       updated_at=excluded.updated_at""",
                (spec.id, spec.name, int(spec.enabled), blob, spec.created_at, now),
            )
            self._conn.commit()

    def get(self, job_id: str) -> JobSpec | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT spec_json, enabled FROM jobs WHERE id = ?", (job_id,)
            )
            row = cur.fetchone()
        if row is None:
            return None
        return self._decode(job_id, row[0], enabled=bool(row[1]))

    def list(self) -> list[JobSpec]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT id, spec_json, enabled FROM jobs ORDER BY created_at"
            )
            rows = cur.fetchall()
        specs: list[JobSpec] = []
        for job_id, blob, enabled in rows:
            spec = self._decode(job_id, blob, enabled=bool(enabled))
            if spec is not None:
                specs.append(spec)
        return specs

    def delete(self, job_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            self._conn.execute("DELETE FROM job_runs WHERE job_id = ?", (job_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def set_enabled(self, job_id: str, enabled: bool) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET enabled = ?, updated_at = ? WHERE id = ?",
                (int(enabled), time.time(), job_id),
            )
            self._conn.commit()

    # -- run history ------------------------------------------------------- #
    def record_run(
        self,
        job_id: str,
        *,
        started_at: float,
        finished_at: float | None,
        ok: bool,
        changed: bool,
        fingerprint: str | None = None,
        error: str | None = None,
        strategy: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO job_runs
                       (job_id, started_at, finished_at, ok, changed,
                        fingerprint, error, strategy)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job_id,
                    started_at,
                    finished_at,
                    int(ok),
                    int(changed),
                    fingerprint,
                    error,
                    strategy,
                ),
            )
            self._conn.commit()

    def runs(self, job_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                """SELECT started_at, finished_at, ok, changed, fingerprint,
                          error, strategy
                   FROM job_runs WHERE job_id = ?
                   ORDER BY started_at DESC LIMIT ?""",
                (job_id, limit),
            )
            rows = cur.fetchall()
        return [
            {
                "started_at": started_at,
                "finished_at": finished_at,
                "ok": bool(ok),
                "changed": bool(changed),
                "fingerprint": fingerprint,
                "error": error,
                "strategy": strategy,
            }
            for (started_at, finished_at, ok, changed, fingerprint, error, strategy)
            in rows
        ]

    # -- events (the `sqlite` sink writes here) ---------------------------- #
    def record_event(self, job_id: str, event: dict[str, Any]) -> None:
        blob = json.dumps(event, default=str, sort_keys=True)
        with self._lock:
            self._conn.execute(
                "INSERT INTO job_events (job_id, ts, fingerprint, event_json)"
                " VALUES (?, ?, ?, ?)",
                (job_id, event.get("ts", time.time()), event.get("fingerprint"), blob),
            )
            self._conn.commit()

    def events(self, job_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT event_json FROM job_events WHERE job_id = ?"
                " ORDER BY ts DESC LIMIT ?",
                (job_id, limit),
            )
            rows = cur.fetchall()
        out: list[dict[str, Any]] = []
        for (blob,) in rows:
            try:
                out.append(json.loads(blob))
            except Exception:  # noqa: BLE001
                continue
        return out

    # -- internal ---------------------------------------------------------- #
    @staticmethod
    def _decode(job_id: str, blob: str, *, enabled: bool) -> JobSpec | None:
        try:
            spec = JobSpec.from_dict(json.loads(blob))
        except Exception:  # noqa: BLE001
            logger.warning("dropping corrupt job spec for id=%s", job_id)
            return None
        # the column is authoritative for the enabled flag (set_enabled toggles it)
        spec.enabled = enabled
        return spec
