"""`tags:` job key + `--tag`/`--exclude-tag` selectors.

Covers the plan-job-tags spec:
- recognition: a `tags:` key (list or scalar) loads with no unknown-key warning
- additive: a plan with no `tags:` resolves to the same JobSpecs / output as before
- selection: --tag / --exclude-tag across plan validate, render, eject, and the
  jobs-serve preload, composing with --only/--exclude
- clean error: selecting by a tag no job carries fails non-zero with a `ujin: …`
  message and no traceback
- fan-out: a `{{ var }}`-templated tag substitutes per matrix entry
- merge: a per-job `tags:` overrides the `defaults:` `tags:` (same precedence as
  every other key)
- introspection: GET /plan surfaces each resolved job's tags

All hermetic — once/command sources, run_engine=False, no network.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from ujin.cli import main
from ujin.jobs.model import JobSpec


# ── shared helpers ───────────────────────────────────────────────────────────── #

def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _job(url: str = "https://example.com/") -> dict:
    return {
        "source": {"kind": "http", "config": {"url": url}},
        "schedule": {"mode": "once"},
    }


def _tagged_plan(tmp_path: Path) -> Path:
    """A 3-job plan: alpha (prod, api), beta (prod, web), gamma (staging, web)."""
    plan = tmp_path / "plan.yaml"
    _write(plan, [
        {"id": "alpha", "tags": ["prod", "api"], **_job("https://a/")},
        {"id": "beta",  "tags": ["prod", "web"], **_job("https://b/")},
        {"id": "gamma", "tags": ["staging", "web"], **_job("https://c/")},
    ])
    return plan


# ══════════════════════════════════════════════════════════════════════════════ #
#  recognition + additive model layer
# ══════════════════════════════════════════════════════════════════════════════ #

def test_tags_key_no_unknown_key_warning(tmp_path, capsys):
    plan = _tagged_plan(tmp_path)
    rc = main(["plan", "validate", str(plan), "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["warnings"] == []  # `tags:` is a recognized key, not advisory noise


def test_scalar_tag_coerced_to_singleton_list():
    spec = JobSpec.from_dict({"id": "x", "name": "x", "tags": "prod",
                              "source": {"kind": "http", "config": {}}})
    assert spec.tags == ["prod"]


def test_list_tags_preserved():
    spec = JobSpec.from_dict({"id": "x", "name": "x", "tags": ["prod", "eu"],
                              "source": {"kind": "http", "config": {}}})
    assert spec.tags == ["prod", "eu"]


def test_no_tags_key_resolves_to_empty_list():
    spec = JobSpec.from_dict({"id": "x", "name": "x", "source": {"kind": "http", "config": {}}})
    assert spec.tags == []


def test_no_tags_spec_to_dict_omits_tags_key():
    """Additive: a tag-less spec serializes byte-identically to before the key."""
    spec = JobSpec.from_dict({"id": "x", "name": "x", "source": {"kind": "http", "config": {}}})
    assert "tags" not in spec.to_dict()


def test_tagged_spec_to_dict_includes_tags_key():
    spec = JobSpec.from_dict({"id": "x", "name": "x", "tags": ["prod"],
                              "source": {"kind": "http", "config": {}}})
    assert spec.to_dict()["tags"] == ["prod"]


# ══════════════════════════════════════════════════════════════════════════════ #
#  additive regression — a no-tags plan renders exactly as before
# ══════════════════════════════════════════════════════════════════════════════ #

def test_no_tags_plan_render_byte_identical(tmp_path, capsys):
    """The same plan with no `tags:` key renders to the identical JSON as before."""
    plan = tmp_path / "plan.yaml"
    _write(plan, [
        {"id": "alpha", **_job("https://a/")},
        {"id": "beta", **_job("https://b/")},
    ])
    main(["plan", "render", str(plan), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert {d["id"] for d in data} == {"alpha", "beta"}
    # No spec carries a `tags` field when none was declared (strictly additive).
    assert all("tags" not in d for d in data)


# ══════════════════════════════════════════════════════════════════════════════ #
#  plan render: --tag / --exclude-tag
# ══════════════════════════════════════════════════════════════════════════════ #

def test_render_tag_keeps_matching_jobs(tmp_path, capsys):
    plan = _tagged_plan(tmp_path)
    main(["plan", "render", str(plan), "--tag", "prod", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert {d["id"] for d in data} == {"alpha", "beta"}


def test_render_tag_glob(tmp_path, capsys):
    plan = _tagged_plan(tmp_path)
    main(["plan", "render", str(plan), "--tag", "stag*", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert {d["id"] for d in data} == {"gamma"}


def test_render_exclude_tag_drops_matching_jobs(tmp_path, capsys):
    plan = _tagged_plan(tmp_path)
    main(["plan", "render", str(plan), "--exclude-tag", "web", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert {d["id"] for d in data} == {"alpha"}


def test_render_tag_comma_separated(tmp_path, capsys):
    plan = _tagged_plan(tmp_path)
    main(["plan", "render", str(plan), "--tag", "api,staging", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert {d["id"] for d in data} == {"alpha", "gamma"}


def test_render_tag_composes_with_only(tmp_path, capsys):
    """--only and --tag AND together: alpha is the only prod job among {alpha,beta}."""
    plan = _tagged_plan(tmp_path)
    main(["plan", "render", str(plan),
          "--only", "alpha,gamma", "--tag", "prod", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert {d["id"] for d in data} == {"alpha"}


def test_render_tag_no_match_exits_clean(tmp_path, capsys):
    plan = _tagged_plan(tmp_path)
    rc = main(["plan", "render", str(plan), "--tag", "no-such-tag"])
    err = capsys.readouterr().err
    assert rc != 0
    assert "ujin:" in err
    assert "no-such-tag" in err
    assert "Traceback" not in err


def test_render_tags_surfaced_in_json(tmp_path, capsys):
    plan = _tagged_plan(tmp_path)
    main(["plan", "render", str(plan), "--only", "alpha", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data[0]["tags"] == ["prod", "api"]


# ══════════════════════════════════════════════════════════════════════════════ #
#  plan validate: --tag / --exclude-tag
# ══════════════════════════════════════════════════════════════════════════════ #

def test_validate_tag_keeps_matching(tmp_path, capsys):
    plan = _tagged_plan(tmp_path)
    main(["plan", "validate", str(plan), "--tag", "prod", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert set(data["resolved"]) == {"alpha", "beta"}


def test_validate_exclude_tag(tmp_path, capsys):
    plan = _tagged_plan(tmp_path)
    main(["plan", "validate", str(plan), "--exclude-tag", "staging", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert set(data["resolved"]) == {"alpha", "beta"}


def test_validate_tag_no_match_exits_clean(tmp_path, capsys):
    plan = _tagged_plan(tmp_path)
    rc = main(["plan", "validate", str(plan), "--tag", "ghost", "--json"])
    assert rc != 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False
    assert "ujin:" in data["error"]
    assert "ghost" in data["error"]


def test_validate_tag_composes_with_exclude(tmp_path, capsys):
    plan = _tagged_plan(tmp_path)
    main(["plan", "validate", str(plan),
          "--tag", "web", "--exclude", "gamma", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert set(data["resolved"]) == {"beta"}


# ══════════════════════════════════════════════════════════════════════════════ #
#  plan eject: --tag / --exclude-tag
# ══════════════════════════════════════════════════════════════════════════════ #

def test_eject_tag_writes_only_selected(tmp_path, capsys):
    plan = _tagged_plan(tmp_path)
    out = tmp_path / "ejected"
    rc = main(["plan", "eject", str(plan), "--out", str(out), "--tag", "prod"])
    assert rc == 0
    written = {p.stem for p in out.glob("*.yaml")}
    assert written == {"alpha", "beta"}


def test_eject_preserves_tags_in_file(tmp_path, capsys):
    plan = _tagged_plan(tmp_path)
    out = tmp_path / "ejected"
    main(["plan", "eject", str(plan), "--out", str(out), "--only", "alpha"])
    doc = yaml.safe_load((out / "alpha.yaml").read_text())
    assert doc["tags"] == ["prod", "api"]


def test_eject_tag_no_match_exits_clean(tmp_path, capsys):
    plan = _tagged_plan(tmp_path)
    out = tmp_path / "ejected"
    rc = main(["plan", "eject", str(plan), "--out", str(out), "--tag", "ghost"])
    err = capsys.readouterr().err
    assert rc != 0
    assert "ujin:" in err
    assert "Traceback" not in err


# ══════════════════════════════════════════════════════════════════════════════ #
#  matrix-templated tags + defaults merge precedence
# ══════════════════════════════════════════════════════════════════════════════ #

def test_matrix_templated_tag_substitutes_per_entry(tmp_path, capsys):
    plan = tmp_path / "feeds.yaml"
    _write(plan, {
        "jobs": [{
            "id": "feed-{{ region }}",
            "matrix": [{"region": "eu"}, {"region": "us"}],
            "tags": ["feeds", "region-{{ region }}"],
            **_job("https://feeds.example.com/{{ region }}"),
        }],
    })
    main(["plan", "render", str(plan), "--json"])
    data = json.loads(capsys.readouterr().out)
    by_id = {d["id"]: d["tags"] for d in data}
    assert by_id["feed-eu"] == ["feeds", "region-eu"]
    assert by_id["feed-us"] == ["feeds", "region-us"]


def test_matrix_templated_tag_is_selectable(tmp_path, capsys):
    plan = tmp_path / "feeds.yaml"
    _write(plan, {
        "jobs": [{
            "id": "feed-{{ region }}",
            "matrix": [{"region": "eu"}, {"region": "us"}],
            "tags": ["region-{{ region }}"],
            **_job("https://feeds.example.com/{{ region }}"),
        }],
    })
    main(["plan", "render", str(plan), "--tag", "region-eu", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert {d["id"] for d in data} == {"feed-eu"}


def test_defaults_tags_inherited_when_job_has_none(tmp_path, capsys):
    plan = tmp_path / "plan.yaml"
    _write(plan, {
        "defaults": {"tags": ["prod"]},
        "jobs": [{"id": "alpha", **_job()}],
    })
    main(["plan", "render", str(plan), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data[0]["tags"] == ["prod"]


def test_per_job_tags_override_defaults(tmp_path, capsys):
    """A per-job `tags:` replaces the defaults' list (no concat — same as other keys)."""
    plan = tmp_path / "plan.yaml"
    _write(plan, {
        "defaults": {"tags": ["prod"]},
        "jobs": [{"id": "alpha", "tags": ["staging"], **_job()}],
    })
    main(["plan", "render", str(plan), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data[0]["tags"] == ["staging"]


# ══════════════════════════════════════════════════════════════════════════════ #
#  jobs-serve preload + GET /plan introspection
# ══════════════════════════════════════════════════════════════════════════════ #

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402
from ujin.jobs.app import create_jobs_app  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    return tmp_path / "jobs.db"


def _command_tagged_plan(tmp_path: Path) -> Path:
    """A deterministic command-source plan so preload registers jobs offline."""
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        "jobs:\n"
        "  - id: alpha\n"
        "    tags: [prod, api]\n"
        "    source: {kind: command, config: {argv: [printf, a]}}\n"
        "    schedule: {mode: once}\n"
        "  - id: beta\n"
        "    tags: [staging, web]\n"
        "    source: {kind: command, config: {argv: [printf, b]}}\n"
        "    schedule: {mode: once}\n",
        encoding="utf-8",
    )
    return plan


def test_jobs_serve_tag_filters_preload(tmp_path, db):
    plan = _command_tagged_plan(tmp_path)
    app = create_jobs_app(run_engine=False, plan_path=str(plan), tags=["prod"])
    with TestClient(app) as c:
        loaded = c.get("/health").json()["plan"]["loaded"]
    assert loaded == ["alpha"]


def test_jobs_serve_exclude_tag_filters_preload(tmp_path, db):
    plan = _command_tagged_plan(tmp_path)
    app = create_jobs_app(run_engine=False, plan_path=str(plan), exclude_tags=["web"])
    with TestClient(app) as c:
        loaded = c.get("/health").json()["plan"]["loaded"]
    assert loaded == ["alpha"]


def test_jobs_serve_no_tag_flags_loads_all(tmp_path, db):
    """No tag selectors → every job preloaded (additive regression)."""
    plan = _command_tagged_plan(tmp_path)
    app = create_jobs_app(run_engine=False, plan_path=str(plan))
    with TestClient(app) as c:
        loaded = c.get("/health").json()["plan"]["loaded"]
    assert set(loaded) == {"alpha", "beta"}


def test_jobs_serve_cli_tag_no_match_exits_nonzero(tmp_path, monkeypatch, capsys):
    """The jobs-serve pre-flight rejects a no-match --tag before binding a port."""
    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    plan = _command_tagged_plan(tmp_path)
    rc = main(["jobs-serve", "--plan", str(plan), "--tag", "ghost"])
    err = capsys.readouterr().err
    assert rc != 0
    assert "ujin:" in err
    assert "ghost" in err
    assert "Traceback" not in err


def test_plan_introspection_surfaces_tags(tmp_path, db):
    plan = _command_tagged_plan(tmp_path)
    app = create_jobs_app(run_engine=False, plan_path=str(plan))
    with TestClient(app) as c:
        jobs = c.get("/plan").json()["jobs"]
    by_id = {j["id"]: j["tags"] for j in jobs}
    assert by_id["alpha"] == ["prod", "api"]
    assert by_id["beta"] == ["staging", "web"]


def test_plan_introspection_tags_honor_selection(tmp_path, db):
    plan = _command_tagged_plan(tmp_path)
    app = create_jobs_app(run_engine=False, plan_path=str(plan), tags=["prod"])
    with TestClient(app) as c:
        jobs = c.get("/plan").json()["jobs"]
    assert {j["id"] for j in jobs} == {"alpha"}
    assert jobs[0]["tags"] == ["prod", "api"]


def test_shipped_example_plan_tags_render(capsys):
    """The shipped examples/ingest-plan-tags.yaml resolves with the documented tags."""
    example = Path(__file__).resolve().parents[1] / "examples" / "ingest-plan-tags.yaml"
    main(["plan", "render", str(example), "--json"])
    data = json.loads(capsys.readouterr().out)
    by_id = {d["id"]: d.get("tags", []) for d in data}
    assert by_id["crossref"] == ["prod", "api"]
    assert by_id["feed-tech"] == ["prod", "feeds", "region-eu"]
    assert by_id["feed-science"] == ["prod", "feeds", "region-us"]
