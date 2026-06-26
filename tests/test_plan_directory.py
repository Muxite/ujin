"""INGEST-PLAN directory support — a plan path may be a *directory* of plan files.

When ``--plan`` / ``$UJIN_INGEST_PLAN`` (or any consumer of the served plan) points
at a directory, every ``*.y*ml`` file in it is loaded — in sorted filename order —
through the **identical** per-file pipeline (`include:`/`use:`, `defaults:` merge,
`matrix:`/`for_each:` fan-out, `vars:` defaults, `profiles:` overlay) and the jobs
are aggregated into one set. A single-file path keeps the existing behaviour
byte-for-byte. These tests exercise the loader directly and every served consumer
(startup, ``POST /plan/reload`` with prune + dry_run, ``GET /plan``, and the
opt-in ``--watch-plan`` watcher) — all offline and deterministic, no network.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from ujin.jobs.app import (
    _aggregate_plan_dir,
    _apply_ingest_plan,
    _dryrun_ingest_plan,
    _introspect_active_plan,
    _load_ingest_plan,
    _PlanWatcher,
)


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _cmd_job(job_id, arg="x"):
    return {"id": job_id, "name": job_id, "schedule": {"mode": "once"},
            "source": {"kind": "command", "config": {"argv": ["printf", arg]}}}


# ── core loader: a directory aggregates the union of its plan files ──────────── #
def test_directory_loads_union_of_jobs(tmp_path):
    """A directory loads the union of jobs declared across every `*.y*ml` file."""
    d = tmp_path / "plan.d"
    # Mix a list-form file (.yaml) and a mapping-form file (.yml) to prove both
    # per-file shapes resolve through the same pipeline and aggregate.
    _write(d / "a.yaml", [_cmd_job("alpha", "a")])
    (d / "b.yml").write_text(
        "defaults: {schedule: {mode: once}}\n"
        "jobs:\n"
        "  - {id: beta, source: {kind: command, config: {argv: [printf, b]}}}\n"
        "  - {id: gamma, source: {kind: command, config: {argv: [printf, g]}}}\n",
        encoding="utf-8",
    )
    specs = _load_ingest_plan(d)
    assert sorted(s.id for s in specs) == ["alpha", "beta", "gamma"]


def test_single_file_matches_directory_of_one(tmp_path):
    """A directory containing one file resolves to the same jobs as that file alone."""
    f = tmp_path / "standalone.yaml"
    _write(f, [_cmd_job("alpha", "a"), _cmd_job("beta", "b")])
    d = tmp_path / "plan.d"
    _write(d / "standalone.yaml", [_cmd_job("alpha", "a"), _cmd_job("beta", "b")])
    file_ids = sorted(s.id for s in _load_ingest_plan(f))
    dir_ids = sorted(s.id for s in _load_ingest_plan(d))
    assert file_ids == dir_ids == ["alpha", "beta"]


def test_single_file_path_is_unchanged(tmp_path):
    """A plain file path still resolves exactly as before (matrix + defaults)."""
    f = tmp_path / "plan.yaml"
    (f).write_text(
        "defaults: {schedule: {mode: once}}\n"
        "jobs:\n"
        "  - id: 'feed-{{ slug }}'\n"
        "    matrix: [{slug: tech}, {slug: science}]\n"
        "    source: {kind: command, config: {argv: [printf, '{{ slug }}']}}\n",
        encoding="utf-8",
    )
    assert sorted(s.id for s in _load_ingest_plan(f)) == ["feed-science", "feed-tech"]


def test_directory_merge_order_is_sorted(tmp_path):
    """Files load in sorted filename order, so the merged order is deterministic."""
    d = tmp_path / "plan.d"
    _write(d / "20-second.yaml", [_cmd_job("two", "2")])
    _write(d / "10-first.yaml", [_cmd_job("one", "1")])
    ids = [s.id for s in _load_ingest_plan(d)]
    assert ids == ["one", "two"]


def test_directory_skips_non_yaml_and_subdir_fragments(tmp_path):
    """Only top-level `*.y*ml` files load; other files and subdirs are ignored."""
    d = tmp_path / "plan.d"
    _write(d / "a.yaml", [_cmd_job("alpha", "a")])
    (d / "notes.txt").write_text("ignored\n", encoding="utf-8")
    # A fragment kept in a subdirectory must never load as a standalone plan.
    frag = d / "fragments" / "sink.yaml"
    frag.parent.mkdir(parents=True, exist_ok=True)
    frag.write_text("kind: jsonl\nconfig: {path: /tmp/out.jsonl}\n", encoding="utf-8")
    assert [s.id for s in _load_ingest_plan(d)] == ["alpha"]


def test_empty_directory_loads_no_jobs(tmp_path):
    """A directory with no plan files resolves to zero jobs without failing."""
    d = tmp_path / "plan.d"
    d.mkdir()
    failed: list = []
    assert _load_ingest_plan(d, failed=failed) == []
    assert failed == []


# ── cross-file duplicate detection ──────────────────────────────────────────── #
def test_cross_file_duplicate_id_is_reported(tmp_path):
    """Two files resolving to the same id: keep the first, report the collision."""
    d = tmp_path / "plan.d"
    _write(d / "a.yaml", [_cmd_job("shared", "a")])
    _write(d / "b.yaml", [_cmd_job("shared", "b"), _cmd_job("bonly", "x")])
    failed: list = []
    specs = _load_ingest_plan(d, failed=failed)
    # The first file's `shared` is kept; `bonly` still loads; nothing silently lost.
    assert sorted(s.id for s in specs) == ["bonly", "shared"]
    assert len(failed) == 1
    msg = failed[0]["error"]
    assert failed[0]["id"] == "shared"
    assert "duplicate job id 'shared'" in msg
    # The message names BOTH involved files so the collision is actionable.
    assert "a.yaml" in msg and "b.yaml" in msg


def test_cross_file_duplicate_keeps_first_file_spec(tmp_path):
    """On a cross-file collision the first file's spec wins (no silent overwrite)."""
    d = tmp_path / "plan.d"
    _write(d / "a.yaml", [_cmd_job("shared", "from-a")])
    _write(d / "b.yaml", [_cmd_job("shared", "from-b")])
    specs = _load_ingest_plan(d)
    (kept,) = [s for s in specs if s.id == "shared"]
    assert kept.source.config["argv"] == ["printf", "from-a"]


# ── per-file error isolation ────────────────────────────────────────────────── #
def test_bad_file_fails_only_its_jobs(tmp_path):
    """A file-level error (bad YAML shape) fails only that file; others still load."""
    d = tmp_path / "plan.d"
    _write(d / "good.yaml", [_cmd_job("alpha", "a")])
    (d / "bad.yaml").write_text("just a bare string\n", encoding="utf-8")
    failed: list = []
    specs = _load_ingest_plan(d, failed=failed)
    assert [s.id for s in specs] == ["alpha"]
    assert len(failed) == 1
    assert "bad.yaml" in failed[0]["error"]


def test_per_job_error_isolated_within_directory(tmp_path):
    """A per-job error in one file fails just that job; sibling files still load."""
    d = tmp_path / "plan.d"
    (d / "a.yaml").parent.mkdir(parents=True, exist_ok=True)
    # `matrix:` as a scalar is a per-job error — only this entry fails.
    (d / "a.yaml").write_text(
        "- {id: broken, matrix: 42, source: {kind: command, config: {argv: [printf, x]}}, "
        "schedule: {mode: once}}\n"
        "- {id: ok-a, source: {kind: command, config: {argv: [printf, a]}}, "
        "schedule: {mode: once}}\n",
        encoding="utf-8",
    )
    _write(d / "b.yaml", [_cmd_job("ok-b", "b")])
    failed: list = []
    specs = _load_ingest_plan(d, failed=failed)
    assert sorted(s.id for s in specs) == ["ok-a", "ok-b"]
    assert [f["id"] for f in failed] == ["broken"]


# ── present (prune keep-set) + provenance threading aggregate across files ───── #
def test_present_keepset_is_union_across_files(tmp_path):
    """`present` (the prune keep-set) collects declared ids from every file."""
    d = tmp_path / "plan.d"
    _write(d / "a.yaml", [_cmd_job("alpha", "a")])
    _write(d / "b.yaml", [_cmd_job("beta", "b"), _cmd_job("gamma", "g")])
    present: list = []
    _load_ingest_plan(d, present=present)
    assert sorted(present) == ["alpha", "beta", "gamma"]


def test_provenance_names_each_files_source(tmp_path):
    """Provenance is collected in lock-step and names each job's originating file."""
    d = tmp_path / "plan.d"
    _write(d / "a.yaml", [_cmd_job("alpha", "a")])
    _write(d / "b.yaml", [_cmd_job("beta", "b")])
    prov: list = []
    specs = _load_ingest_plan(d, provenance=prov)
    assert len(prov) == len(specs)
    by_id = {p["id"]: Path(p["source"]).name for p in prov}
    assert by_id == {"alpha": "a.yaml", "beta": "b.yaml"}


def test_provenance_skips_cross_file_duplicate(tmp_path):
    """A skipped cross-file duplicate is not double-counted in provenance."""
    d = tmp_path / "plan.d"
    _write(d / "a.yaml", [_cmd_job("shared", "a")])
    _write(d / "b.yaml", [_cmd_job("shared", "b")])
    prov: list = []
    specs = _load_ingest_plan(d, provenance=prov)
    assert len(prov) == len(specs) == 1
    assert Path(prov[0]["source"]).name == "a.yaml"


def test_directory_threads_advisory_warnings(tmp_path):
    """Advisory warnings (a misspelled key) aggregate across the directory's files."""
    d = tmp_path / "plan.d"
    (d / "a.yaml").parent.mkdir(parents=True, exist_ok=True)
    # `defaluts:` is a misspelled top-level key -> an advisory warning, jobs still load.
    (d / "a.yaml").write_text(
        "defaluts: {schedule: {mode: once}}\n"
        "jobs:\n"
        "  - {id: alpha, source: {kind: command, config: {argv: [printf, a]}}, "
        "schedule: {mode: once}}\n",
        encoding="utf-8",
    )
    warnings: list = []
    specs = _load_ingest_plan(d, warnings=warnings)
    assert [s.id for s in specs] == ["alpha"]
    assert any("defaluts" in w for w in warnings)


def test_aggregate_helper_is_used_for_directories(tmp_path):
    """The directory branch delegates to `_aggregate_plan_dir` with the same result."""
    d = tmp_path / "plan.d"
    _write(d / "a.yaml", [_cmd_job("alpha", "a")])
    direct = sorted(s.id for s in _aggregate_plan_dir(d))
    via_loader = sorted(s.id for s in _load_ingest_plan(d))
    assert direct == via_loader == ["alpha"]


# ── served consumers: apply / dry_run / introspect resolve a directory ──────── #
def _bare_manager(tmp_path):
    from ujin.engine import PollEngine
    from ujin.jobs.manager import JobManager
    from ujin.jobs.store import JobStore

    return JobManager(PollEngine(), JobStore(tmp_path / "jobs.db"))


def test_apply_directory_and_prune_across_files(tmp_path):
    """`_apply_ingest_plan` upserts the union and prunes across files on reload."""
    mgr = _bare_manager(tmp_path)
    d = tmp_path / "plan.d"
    _write(d / "a.yaml", [_cmd_job("alpha", "a")])
    _write(d / "b.yaml", [_cmd_job("beta", "b")])
    status = _apply_ingest_plan(mgr, str(d), prune=True)
    assert sorted(status["loaded"]) == ["alpha", "beta"]
    assert set(mgr.jobs) == {"alpha", "beta"}

    # Drop b.yaml entirely: a reload-with-prune removes beta but keeps alpha.
    (d / "b.yaml").unlink()
    status = _apply_ingest_plan(mgr, str(d), prune=True)
    assert status["loaded"] == ["alpha"]
    assert status["pruned"] == ["beta"]
    assert set(mgr.jobs) == {"alpha"}


def test_dryrun_directory_previews_union_without_mutating(tmp_path):
    """`_dryrun_ingest_plan` classifies the union of a directory without mutating."""
    mgr = _bare_manager(tmp_path)
    d = tmp_path / "plan.d"
    _write(d / "a.yaml", [_cmd_job("alpha", "a")])
    _apply_ingest_plan(mgr, str(d))  # alpha now running

    _write(d / "b.yaml", [_cmd_job("beta", "b")])  # newly added file
    preview = _dryrun_ingest_plan(mgr, str(d), prune=True)
    assert preview["added"] == ["beta"]
    assert preview["unchanged"] == ["alpha"]
    # Pure preview: nothing was created.
    assert set(mgr.jobs) == {"alpha"}


def test_introspect_directory_reflects_union_with_provenance(tmp_path):
    """`GET /plan` introspection (a directory) reflects the union + per-file source."""
    d = tmp_path / "plan.d"
    _write(d / "a.yaml", [_cmd_job("alpha", "a")])
    _write(d / "b.yaml", [_cmd_job("beta", "b")])
    out = _introspect_active_plan(str(d), wf_dir=None)
    by_id = {j["id"]: Path(j["provenance"]["source"]).name for j in out["jobs"]}
    assert by_id == {"alpha": "a.yaml", "beta": "b.yaml"}
    assert out["failed"] == []


# ── server end-to-end: startup + reload route over a plan directory ─────────── #
def test_server_startup_and_reload_over_directory(tmp_path, monkeypatch):
    """jobs-serve loads a plan directory at startup; reload+prune spans its files."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ujin.jobs.app import create_jobs_app

    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    d = tmp_path / "plan.d"
    _write(d / "a.yaml", [_cmd_job("alpha", "a")])
    _write(d / "b.yaml", [_cmd_job("beta", "b")])

    with TestClient(create_jobs_app(run_engine=False, plan_path=str(d))) as c:
        assert {j["id"] for j in c.get("/jobs").json()} == {"alpha", "beta"}
        # /plan introspection mirrors the same merged set.
        plan_jobs = {j["id"] for j in c.get("/plan").json()["jobs"]}
        assert plan_jobs == {"alpha", "beta"}
        # Drop a file and reload with prune: beta goes, alpha stays.
        (d / "b.yaml").unlink()
        body = c.post("/plan/reload?prune=true").json()
        assert body["loaded"] == ["alpha"]
        assert body["pruned"] == ["beta"]
        assert {j["id"] for j in c.get("/jobs").json()} == {"alpha"}


def test_server_directory_reports_cross_file_duplicate_in_health(tmp_path, monkeypatch):
    """A cross-file duplicate id surfaces in the /health plan `failed` list."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ujin.jobs.app import create_jobs_app

    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    d = tmp_path / "plan.d"
    _write(d / "a.yaml", [_cmd_job("shared", "a")])
    _write(d / "b.yaml", [_cmd_job("shared", "b"), _cmd_job("bonly", "x")])

    with TestClient(create_jobs_app(run_engine=False, plan_path=str(d))) as c:
        assert {j["id"] for j in c.get("/jobs").json()} == {"shared", "bonly"}
        failed = c.get("/health").json()["plan"]["failed"]
        assert any(
            f["id"] == "shared" and "duplicate job id 'shared'" in f["error"]
            for f in failed
        )


# ── opt-in watcher reacts to add / modify / remove in the directory ─────────── #
class _FakeApp:
    class _State:
        pass

    def __init__(self):
        self.state = self._State()


def test_watch_directory_reacts_to_add_modify_remove(tmp_path):
    """The watcher reloads on add/modify/remove of any plan file in the directory."""
    mgr = _bare_manager(tmp_path)
    app = _FakeApp()
    d = tmp_path / "plan.d"
    _write(d / "a.yaml", [_cmd_job("alpha", "a")])
    _write(d / "b.yaml", [_cmd_job("beta", "b")])
    app.state.plan = _apply_ingest_plan(mgr, str(d), prune=True)

    async def _noop(_delay):
        return None

    w = _PlanWatcher(app, mgr, str(d), None, None, 0.01, sleep=_noop)
    assert set(mgr.jobs) == {"alpha", "beta"}
    # Unchanged directory -> no reload.
    assert w.poll_once() is False

    # ADD a file -> the new id appears.
    _write(d / "c.yaml", [_cmd_job("gamma", "g")])
    assert w.poll_once() is True
    assert set(mgr.jobs) == {"alpha", "beta", "gamma"}

    # MODIFY a file (size changes) -> reload picks it up, no spurious churn.
    _write(d / "a.yaml", [_cmd_job("alpha", "alpha-changed")])
    assert w.poll_once() is True
    (alpha,) = [h.spec for jid, h in mgr.jobs.items() if jid == "alpha"]
    assert alpha.source.config["argv"] == ["printf", "alpha-changed"]

    # REMOVE a file -> its job is pruned, the rest stay.
    (d / "b.yaml").unlink()
    assert w.poll_once() is True
    assert set(mgr.jobs) == {"alpha", "gamma"}


def test_watch_directory_removed_is_non_destructive(tmp_path):
    """If the watched directory vanishes, poll_once is a no-op (jobs are kept)."""
    import shutil

    mgr = _bare_manager(tmp_path)
    app = _FakeApp()
    d = tmp_path / "plan.d"
    _write(d / "a.yaml", [_cmd_job("alpha", "a")])
    app.state.plan = _apply_ingest_plan(mgr, str(d), prune=True)

    async def _noop(_delay):
        return None

    w = _PlanWatcher(app, mgr, str(d), None, None, 0.01, sleep=_noop)
    shutil.rmtree(d)
    assert w.poll_once() is False
    assert set(mgr.jobs) == {"alpha"}


def test_watch_directory_signature_skips_unstatable_file(tmp_path):
    """A plan file that can't be stat-ed (a broken symlink) is skipped in the
    directory signature rather than crashing the watcher."""
    mgr = _bare_manager(tmp_path)
    app = _FakeApp()
    d = tmp_path / "plan.d"
    _write(d / "a.yaml", [_cmd_job("alpha", "a")])
    # Matches `*.y*ml` so glob lists it, but `.stat()` raises -> skipped, not fatal.
    (d / "broken.yaml").symlink_to(d / "does-not-exist")
    app.state.plan = _apply_ingest_plan(mgr, str(d), prune=True)

    async def _noop(_delay):
        return None

    w = _PlanWatcher(app, mgr, str(d), None, None, 0.01, sleep=_noop)
    # The signature is computed without error; an unchanged dir is still a no-op,
    # and the broken symlink never loads as a job.
    assert w.poll_once() is False
    assert set(mgr.jobs) == {"alpha"}


def test_watch_directory_run_loop_applies_then_stops(tmp_path):
    """The background run() loop applies a directory edit with an injected sleep."""
    mgr = _bare_manager(tmp_path)
    app = _FakeApp()
    d = tmp_path / "plan.d"
    _write(d / "a.yaml", [_cmd_job("alpha", "a")])
    app.state.plan = _apply_ingest_plan(mgr, str(d), prune=True)

    async def _noop(_delay):
        return None

    w = _PlanWatcher(app, mgr, str(d), None, None, 0.01, sleep=_noop)
    _write(d / "b.yaml", [_cmd_job("beta", "b")])
    asyncio.run(w.run(max_ticks=1))
    assert set(mgr.jobs) == {"alpha", "beta"}
    assert sorted(app.state.plan["loaded"]) == ["alpha", "beta"]
