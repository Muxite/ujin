"""Targeted coverage tests for jobs subsystem gaps.

Covers term-missing branches in:
  ujin/jobs/app.py        84-91, 105-106, 149-150, 162-200, 250-256, 281-313
  ujin/jobs/pipeline.py   53-61, 93-96
  ujin/jobs/cron.py       35-36, 64-66, 86, 94-95
  ujin/jobs/transforms.py 25-29, 39-48, 80-83, 102-121, 148-167, 406-407
"""
from __future__ import annotations

import dataclasses
import sys
import types

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from ujin.jobs.app import _load_workflows_dir, _specs_from_workflow_file, create_jobs_app  # noqa: E402
from ujin.jobs.cron import CronExpr, _parse_field, next_fire  # noqa: E402
from ujin.jobs.pipeline import Pipeline, _jsonable  # noqa: E402
from ujin.jobs.transforms import (  # noqa: E402
    DedupeTransform,
    RegexTransform,
    SelectTransform,
    TemplateTransform,
    build_transform,
    dotted_get,
    dotted_set,
)
from ujin.poll.base import PollResult  # noqa: E402


# ── shared fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "jobs.db"
    monkeypatch.setenv("UJIN_JOBS_DB", str(path))
    return path


def _simple_job(out_path) -> dict:
    return {
        "name": "runner",
        "source": {"kind": "command", "config": {"argv": ["echo", "hi"]}},
        "sinks": [{"kind": "jsonl", "config": {"path": str(out_path)}}],
        "schedule": {"mode": "once"},
    }


# ── app.py: _specs_from_workflow_file (lines 84-91) ──────────────────────────


def test_specs_from_workflow_file_single_in_jobs_list(tmp_path):
    # Single entry under `jobs:` key → stem used as id
    f = tmp_path / "myjob.yaml"
    f.write_text(
        "jobs:\n"
        "  - name: j1\n"
        "    source:\n"
        "      kind: command\n"
        "      config:\n"
        "        argv: [echo, a]\n"
    )
    specs = _specs_from_workflow_file(f)
    assert len(specs) == 1
    assert specs[0].id == "myjob"


def test_specs_from_workflow_file_multi_in_jobs_list(tmp_path):
    # Multiple entries → stem-0 / stem-1 ids
    f = tmp_path / "multi.yaml"
    f.write_text(
        "jobs:\n"
        "  - name: job_a\n"
        "    source:\n"
        "      kind: command\n"
        "      config:\n"
        "        argv: [echo, a]\n"
        "  - name: job_b\n"
        "    source:\n"
        "      kind: command\n"
        "      config:\n"
        "        argv: [echo, b]\n"
    )
    specs = _specs_from_workflow_file(f)
    assert len(specs) == 2
    assert specs[0].id == "multi-0"
    assert specs[1].id == "multi-1"


# ── app.py: _load_workflows_dir bad-file warning (lines 105-106) ─────────────


def test_load_workflows_dir_bad_file_logs_warning(tmp_path, caplog):
    import logging

    (tmp_path / "bad.yaml").write_text("[unclosed")  # invalid YAML
    with caplog.at_level(logging.WARNING, logger="ujin.jobs.app"):
        result = _load_workflows_dir(str(tmp_path))
    assert result == []
    assert any("skipping workflow file" in r.message for r in caplog.records)


# ── app.py: scrape backend unavailable (lines 149-150) ───────────────────────


def test_scrape_backend_unavailable_continues(db, monkeypatch):
    try:
        import ujin.scrape.build as _sb  # noqa: F401
    except ImportError:
        pytest.skip("scrape extra not installed")

    import ujin.scrape.build as _sb

    async def _fail(cfg):
        raise RuntimeError("forced failure for test")

    monkeypatch.setattr(_sb, "build_scrape_service", _fail)
    app = create_jobs_app(run_engine=False)
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200


# ── app.py: config_path preload (lines 162-166) ───────────────────────────────


def test_config_path_preloads_jobs(tmp_path, db):
    config = tmp_path / "jobs.yaml"
    config.write_text(
        "jobs:\n"
        "  - name: cfg-job\n"
        "    source:\n"
        "      kind: command\n"
        "      config:\n"
        "        argv: [echo, preloaded]\n"
        "    schedule:\n"
        "      mode: once\n"
    )
    app = create_jobs_app(config_path=str(config), run_engine=False)
    with TestClient(app) as c:
        jobs = c.get("/jobs").json()
    assert any(j["name"] == "cfg-job" for j in jobs)


def test_config_path_bad_job_logs_warning(tmp_path, db, caplog):
    import logging

    config = tmp_path / "jobs.yaml"
    config.write_text(
        "jobs:\n"
        "  - name: bad\n"
        "    source:\n"
        "      kind: _totally_unknown_kind_for_test\n"
        "    schedule:\n"
        "      mode: once\n"
    )
    with caplog.at_level(logging.WARNING, logger="ujin.jobs.app"):
        app = create_jobs_app(config_path=str(config), run_engine=False)
        with TestClient(app) as c:
            assert c.get("/health").status_code == 200


# ── app.py: workflow create failure (lines 172-178) ──────────────────────────


def test_workflow_create_failure_is_non_fatal(tmp_path, db):
    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    (wf_dir / "broken.yaml").write_text(
        "name: broken\n"
        "source:\n"
        "  kind: _nonexistent_kind_xyz\n"
    )
    app = create_jobs_app(workflows_dir=str(wf_dir), run_engine=False)
    with TestClient(app) as c:
        h = c.get("/health").json()
    assert h["ok"]
    assert any(e["id"] == "broken" for e in h["workflows"]["failed"])


# ── app.py: run_engine=True teardown (lines 186-200) ─────────────────────────


def test_run_engine_true_teardown(db):
    app = create_jobs_app(run_engine=True)
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200
    # Context exit triggers lifespan shutdown → tasks are cancelled cleanly


# ── app.py: plugins_reload endpoint (lines 250-256) ──────────────────────────


def test_plugins_reload_endpoint(db):
    app = create_jobs_app(run_engine=False)
    with TestClient(app) as c:
        r = c.post("/plugins/reload")
    assert r.status_code == 200
    data = r.json()
    assert "loaded" in data


# ── app.py: 404 paths (lines 281-313) ────────────────────────────────────────


def test_404_on_unknown_job(db):
    app = create_jobs_app(run_engine=False)
    with TestClient(app) as c:
        jid = "no-such-job-xyz"
        assert c.delete(f"/jobs/{jid}").status_code == 404
        assert c.post(f"/jobs/{jid}/run").status_code == 404
        assert c.post(f"/jobs/{jid}/pause").status_code == 404
        assert c.post(f"/jobs/{jid}/resume").status_code == 404
        assert c.get(f"/jobs/{jid}/runs").status_code == 404
        assert c.get(f"/jobs/{jid}/events").status_code == 404
        assert c.get(f"/jobs/{jid}/content").status_code == 404
        assert c.get(f"/jobs/{jid}/results").status_code == 404


# ── app.py: job_content and job_results (lines 313-344) ──────────────────────


def test_job_content_before_and_after_poll(db, tmp_path):
    out = tmp_path / "e.jsonl"
    app = create_jobs_app(run_engine=False)
    with TestClient(app) as c:
        job_id = c.post("/jobs", json=_simple_job(out)).json()["id"]

        # Before first poll: all result fields are null
        content = c.get(f"/jobs/{job_id}/content").json()
        assert content["id"] == job_id
        assert content["ok"] is None

        c.post(f"/jobs/{job_id}/run")

        # After poll: ok is true, payload populated
        content = c.get(f"/jobs/{job_id}/content").json()
        assert content["ok"] is True

        results = c.get(f"/jobs/{job_id}/results").json()
        assert isinstance(results, list)


# ── pipeline.py: _jsonable edge cases (lines 53-61) ─────────────────────────


def test_jsonable_dataclass_asdict_raises(monkeypatch):
    @dataclasses.dataclass
    class _Bad:
        x: int = 1

    import ujin.jobs.pipeline as _pipe

    def _boom(obj):
        raise RuntimeError("boom")

    monkeypatch.setattr(_pipe.dataclasses, "asdict", _boom)
    result = _jsonable(_Bad())
    assert isinstance(result, str)


def test_jsonable_to_dict_success():
    class _HasToDict:
        def to_dict(self):
            return {"from": "to_dict"}

    assert _jsonable(_HasToDict()) == {"from": "to_dict"}


def test_jsonable_to_dict_raises():
    class _BadToDict:
        def to_dict(self):
            raise RuntimeError("boom")

    result = _jsonable(_BadToDict())
    assert isinstance(result, str)


def test_jsonable_fallback_returns_object():
    class _Opaque:
        pass

    obj = _Opaque()
    assert _jsonable(obj) is obj


# ── pipeline.py: transform raises exception (lines 93-96) ───────────────────


class _RaisingTransform:
    async def apply(self, event: dict):
        raise RuntimeError("transform boom")


class _RecordingSink:
    def __init__(self):
        self.seen: list[dict] = []

    async def emit(self, event: dict) -> None:
        self.seen.append(event)


async def test_pipeline_transform_exception_drops_all_events():
    good = _RecordingSink()
    pipe = Pipeline(transforms=[_RaisingTransform()], sinks=[good])
    r = PollResult(ok=True, changed=True, fingerprint="fp", payload={"k": 1})
    await pipe("j", r)
    assert good.seen == []  # transform raised → pipeline returned early


# ── cron.py: _parse_field range syntax (lines 35-36) ────────────────────────


def test_parse_field_range():
    assert _parse_field("1-5", 0, 59) == {1, 2, 3, 4, 5}


def test_parse_field_range_with_step():
    assert _parse_field("0-10/2", 0, 59) == {0, 2, 4, 6, 8, 10}


# ── cron.py: _day_ok when dom+dow both restricted (line 64) ─────────────────


def test_cron_day_ok_both_dom_and_dow():
    # "0 0 15 * 1": midnight on 15th OR any Monday — both fields restricted
    ce = CronExpr("0 0 15 * 1")
    t = ce.next_after(0.0)
    assert t > 0


# ── cron.py: _day_ok dom-only restricted (line 66) ──────────────────────────


def test_cron_day_ok_dom_restricted_only():
    # "0 0 15 * *": midnight on 15th — dom restricted, dow is *
    ce = CronExpr("0 0 15 * *")
    t = ce.next_after(0.0)
    assert t > 0


# ── cron.py: next_after horizon exceeded (line 86) ──────────────────────────


def test_cron_no_match_raises():
    # Feb 31 never exists — the 4-year scan exhausts without a hit
    ce = CronExpr("0 0 31 2 *")
    with pytest.raises(ValueError, match="no cron match within horizon"):
        ce.next_after(0.0)


# ── cron.py: next_fire croniter path (lines 94-95) ──────────────────────────


def test_next_fire_croniter_path(monkeypatch):
    class _FakeCroniter:
        def __init__(self, expr, base):
            self._base = base

        def get_next(self, t):
            return self._base + 300.0

    fake_mod = types.ModuleType("croniter")
    fake_mod.croniter = _FakeCroniter  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "croniter", fake_mod)

    result = next_fire("*/5 * * * *", now=1000.0)
    assert result == 1300.0


# ── transforms.py: dotted_get list and else branches (lines 25-29) ──────────


def test_dotted_get_list_index():
    data = [{"a": 1}, {"a": 2}]
    assert dotted_get(data, "0") == {"a": 1}
    assert dotted_get(data, "1.a") == 2


def test_dotted_get_list_out_of_bounds():
    assert dotted_get([1, 2], "9") is None


def test_dotted_get_non_dict_non_list_returns_none():
    assert dotted_get("string", "any") is None
    assert dotted_get(42, "x") is None


# ── transforms.py: dotted_set missing intermediate (lines 39-40) ────────────


def test_dotted_set_creates_missing_intermediates():
    obj: dict = {}
    dotted_set(obj, "a.b.c", 42)
    assert obj == {"a": {"b": {"c": 42}}}


def test_dotted_set_overwrites_non_dict_intermediate():
    obj: dict = {"a": "not_a_dict"}
    dotted_set(obj, "a.b", 99)
    assert obj == {"a": {"b": 99}}


# ── transforms.py: SelectTransform non-list target (lines 80-83) ─────────────


async def test_select_scalar_target_where_drops_event():
    t = SelectTransform({"where": {"type": "good"}})
    event = {"job_id": "j", "payload": {"type": "bad"}}
    assert await t.apply(event) is None


async def test_select_scalar_target_fields_project():
    t = SelectTransform({"fields": ["type"]})
    event = {"job_id": "j", "payload": {"type": "good", "extra": 9}}
    out = await t.apply(event)
    assert out["payload"] == {"type": "good"}


async def test_select_list_project_non_dict_item():
    # _project on a non-dict item returns the item as-is (line 48)
    t = SelectTransform({"fields": ["x"]})
    event = {"payload": ["plain_string", {"x": 1}]}
    out = await t.apply(event)
    assert out["payload"] == ["plain_string", {"x": 1}]


# ── transforms.py: RegexTransform None field (lines 102-103) ────────────────


async def test_regex_none_field_returns_empty_extracted():
    t = RegexTransform({"field": "missing_field", "pattern": r"\d+"})
    event = {"payload": "abc123"}  # "missing_field" is absent → None
    out = await t.apply(event)
    assert out["extracted"] == []


# ── transforms.py: TemplateTransform format exception (lines 120-121) ────────


async def test_template_format_exception_uses_raw_template():
    t = TemplateTransform({"template": "{nonexistent_key}"})
    event = {"job_id": "j"}
    out = await t.apply(event)
    assert out["message"] == "{nonexistent_key}"


# ── transforms.py: DedupeTransform LRU eviction (line 148) ──────────────────


def test_dedupe_lru_evicts_oldest():
    t = DedupeTransform({"max": 2})
    t._mark("a")
    t._mark("b")
    t._mark("c")  # "a" evicted
    assert t._mark("a") is True  # "a" is new again


# ── transforms.py: DedupeTransform no fingerprint (lines 154-155) ────────────


async def test_dedupe_no_key_no_fingerprint_passes_through():
    t = DedupeTransform({})
    event = {"payload": "data"}  # fingerprint key absent
    out = await t.apply(event)
    assert out is event


# ── transforms.py: DedupeTransform scalar/dict payload (lines 165-167) ───────


async def test_dedupe_with_key_scalar_payload():
    t = DedupeTransform({"key": "id"})
    ev = {"payload": "scalar_value"}
    r1 = await t.apply(ev)
    assert r1 is ev
    r2 = await t.apply({"payload": "scalar_value"})
    assert r2 is None  # same scalar → duplicate


async def test_dedupe_with_key_dict_payload():
    t = DedupeTransform({"key": "id"})
    r1 = await t.apply({"payload": {"id": "abc", "v": 1}})
    assert r1 is not None
    r2 = await t.apply({"payload": {"id": "abc", "v": 2}})
    assert r2 is None  # same id → duplicate


# ── transforms.py: build_transform unknown kind (lines 406-407) ──────────────


def test_build_transform_unknown_kind_raises():
    with pytest.raises(ValueError, match="unknown transform kind"):
        build_transform("totally_bogus_kind", {})
