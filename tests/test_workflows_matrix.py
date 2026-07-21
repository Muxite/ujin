"""Matrix / for_each workflow fan-out.

A workflow template carrying a ``matrix:`` (alias ``for_each:``) list expands to
one job per entry, with the entry's variables substituted into the whole tree
(source/transforms/sinks/schedule) via ``${VAR}``/``${VAR:-default}``. Generated
ids are distinct and deterministic; a malformed matrix is a clear, named error.
"""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
import yaml  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from ujin.jobs.app import (  # noqa: E402
    WorkflowIncludeError,
    WorkflowMatrixError,
    _specs_from_workflow_file,
    create_jobs_app,
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    return tmp_path


def _write(d, stem, spec):
    (d / f"{stem}.yaml").write_text(yaml.safe_dump(spec))
    return d / f"{stem}.yaml"


# --- unit: expansion, substitution, ids ------------------------------------ #

def test_matrix_expands_to_n_jobs(tmp_path):
    fp = _write(tmp_path, "watch", {
        "matrix": [
            {"NAME": "alpha", "HOST": "a.example.com", "INTERVAL": 2},
            {"NAME": "beta", "HOST": "b.example.com"},
        ],
        "source": {"kind": "http", "config": {"url": "https://${HOST}/s"}},
        "sinks": [{"kind": "jsonl", "config": {"path": "out-${NAME}.jsonl"}}],
        "schedule": {"mode": "cron", "cron": "*/${INTERVAL:-9} * * * *"},
    })
    specs = _specs_from_workflow_file(fp)
    assert len(specs) == 2

    a, b = specs
    # vars substituted into source + sink
    assert a.source.config["url"] == "https://a.example.com/s"
    assert a.sinks[0].config["path"] == "out-alpha.jsonl"
    # schedule substitution + per-entry default fallback
    assert a.schedule.cron == "*/2 * * * *"
    assert b.schedule.cron == "*/9 * * * *"     # INTERVAL unset -> default 9


def test_generated_ids_are_distinct_and_deterministic(tmp_path):
    spec = {
        "matrix": [{"NAME": "alpha"}, {"NAME": "beta"}],
        "source": {"kind": "http", "config": {"url": "https://${NAME}.x"}},
    }
    fp = _write(tmp_path, "watch", spec)
    ids1 = [s.id for s in _specs_from_workflow_file(fp)]
    ids2 = [s.id for s in _specs_from_workflow_file(fp)]
    assert ids1 == ["watch-alpha", "watch-beta"]
    assert ids1 == ids2                          # stable across reloads
    assert len(set(ids1)) == len(ids1)           # no duplicate-id collision


def test_slug_prefers_identity_var(tmp_path):
    # With extra vars, the id slugs off the conventional NAME, not every value.
    fp = _write(tmp_path, "watch", {
        "matrix": [
            {"NAME": "alpha", "HOST": "a.example.com", "INTERVAL": 2},
            {"NAME": "beta", "HOST": "b.example.com"},
        ],
        "source": {"kind": "http", "config": {"url": "https://${HOST}/s"}},
    })
    assert [s.id for s in _specs_from_workflow_file(fp)] == ["watch-alpha", "watch-beta"]


def test_var_driven_id_template(tmp_path):
    fp = _write(tmp_path, "watch", {
        "id": "svc-${NAME}",
        "matrix": [{"NAME": "alpha"}, {"NAME": "beta"}],
        "source": {"kind": "http", "config": {"url": "https://x"}},
    })
    assert [s.id for s in _specs_from_workflow_file(fp)] == ["svc-alpha", "svc-beta"]


def test_colliding_slugs_stay_distinct(tmp_path):
    # Two entries slugify to the same token -> ids disambiguated by index.
    fp = _write(tmp_path, "watch", {
        "matrix": [{"NAME": "a b"}, {"NAME": "a/b"}],
        "source": {"kind": "http", "config": {"url": "https://x"}},
    })
    ids = [s.id for s in _specs_from_workflow_file(fp)]
    assert len(set(ids)) == 2
    assert ids == ["watch-a-b", "watch-a-b-1"]


def test_slug_falls_back_to_values_then_index(tmp_path):
    # No id/name var -> slug of all values; empty values -> positional index.
    fp = _write(tmp_path, "watch", {
        "matrix": [{"REGION": "us"}, {"REGION": ""}],
        "source": {"kind": "http", "config": {"url": "https://${REGION:-any}.x"}},
    })
    ids = [s.id for s in _specs_from_workflow_file(fp)]
    assert ids == ["watch-us", "watch-1"]


def test_mixed_matrix_and_plain_jobs_list(tmp_path):
    # A `jobs:` list may mix a matrix template with an ordinary job.
    fp = _write(tmp_path, "watch", {"jobs": [
        {"id": "solo",
         "source": {"kind": "http", "config": {"url": "https://solo.x"}}},
        {"matrix": [{"NAME": "alpha"}, {"NAME": "beta"}],
         "source": {"kind": "http", "config": {"url": "https://${NAME}.x"}}},
    ]})
    ids = [s.id for s in _specs_from_workflow_file(fp)]
    assert "solo" in ids
    assert "watch-1-alpha" in ids and "watch-1-beta" in ids


def test_for_each_is_an_alias(tmp_path):
    fp = _write(tmp_path, "watch", {
        "for_each": [{"NAME": "alpha"}, {"NAME": "beta"}],
        "source": {"kind": "http", "config": {"url": "https://${NAME}.x"}},
    })
    specs = _specs_from_workflow_file(fp)
    assert [s.id for s in specs] == ["watch-alpha", "watch-beta"]
    assert specs[0].source.config["url"] == "https://alpha.x"


def test_whole_scalar_var_preserves_numeric_type(tmp_path):
    # A whole-value ``${VAR}`` reference keeps the matrix value's native type: a
    # numeric interval stays int (partial ``${NAME}`` interpolation stays a string).
    fp = _write(tmp_path, "watch", {
        "matrix": [{"NAME": "alpha", "IVL": 300}, {"NAME": "beta", "IVL": 600}],
        "source": {"kind": "http", "config": {"url": "https://${NAME}.x"}},
        "schedule": {"mode": "adaptive", "base": "${IVL}"},
    })
    a, b = _specs_from_workflow_file(fp)
    assert a.schedule.base == 300 and isinstance(a.schedule.base, int)
    assert b.schedule.base == 600 and isinstance(b.schedule.base, int)
    # partial interpolation is unaffected — still a substituted string
    assert a.source.config["url"] == "https://alpha.x"


# --- cartesian: a mapping of axes expands to the product ------------------- #

def test_mapping_of_axes_expands_to_cartesian_product(tmp_path):
    # Written with explicit key order (sort_keys=False) so the product order is
    # readable: the first axis varies slowest, the last axis fastest.
    fp = tmp_path / "watch.yaml"
    fp.write_text(yaml.safe_dump({
        "id": "g-${REGION}-${ENV}",
        "for_each": {"REGION": ["us", "eu"], "ENV": ["prod", "dev"]},
        "source": {"kind": "http", "config": {"url": "https://${REGION}.${ENV}/x"}},
    }, sort_keys=False))
    specs = _specs_from_workflow_file(fp)
    assert [s.id for s in specs] == [
        "g-us-prod", "g-us-dev", "g-eu-prod", "g-eu-dev",
    ]
    assert specs[0].source.config["url"] == "https://us.prod/x"
    assert specs[3].source.config["url"] == "https://eu.dev/x"


def test_cartesian_non_list_axis_raises(tmp_path):
    fp = _write(tmp_path, "watch", {
        "matrix": {"REGION": "us"},   # axis value must be a list
        "source": {"kind": "http", "config": {"url": "https://x"}},
    })
    with pytest.raises(WorkflowMatrixError) as ei:
        _specs_from_workflow_file(fp)
    assert "REGION" in str(ei.value) and "list of values" in str(ei.value)


# --- malformed matrix errors ----------------------------------------------- #

def test_non_list_matrix_raises(tmp_path):
    fp = _write(tmp_path, "watch", {
        "matrix": {"NAME": "alpha"},
        "source": {"kind": "http", "config": {"url": "https://x"}},
    })
    with pytest.raises(WorkflowMatrixError) as ei:
        _specs_from_workflow_file(fp)
    assert "watch" in str(ei.value) and "matrix" in str(ei.value)


def test_non_map_entry_raises(tmp_path):
    fp = _write(tmp_path, "watch", {
        "matrix": ["alpha", "beta"],
        "source": {"kind": "http", "config": {"url": "https://x"}},
    })
    with pytest.raises(WorkflowMatrixError) as ei:
        _specs_from_workflow_file(fp)
    assert "watch" in str(ei.value)


def test_duplicate_identity_raises(tmp_path):
    # Two entries carry the same explicit identity var -> a genuine author
    # duplicate, surfaced with the offending id (not silently renumbered).
    fp = _write(tmp_path, "watch", {
        "matrix": [{"id": "same"}, {"id": "same"}],
        "source": {"kind": "http", "config": {"url": "https://x"}},
    })
    with pytest.raises(WorkflowMatrixError) as ei:
        _specs_from_workflow_file(fp)
    assert "duplicate job id" in str(ei.value) and "same" in str(ei.value)


def test_duplicate_var_driven_id_raises(tmp_path):
    # A var-driven id template that collides on identical vars is a duplicate too.
    fp = tmp_path / "watch.yaml"
    fp.write_text(yaml.safe_dump({
        "id": "g-${REGION}",
        "matrix": [{"REGION": "us"}, {"REGION": "us"}],
        "source": {"kind": "http", "config": {"url": "https://${REGION}.x"}},
    }, sort_keys=False))
    with pytest.raises(WorkflowMatrixError) as ei:
        _specs_from_workflow_file(fp)
    assert "duplicate job id" in str(ei.value) and "g-us" in str(ei.value)


# --- API: fan-out visible over /health + /jobs ----------------------------- #

def test_matrix_loads_over_api(db, tmp_path):
    wf = tmp_path / "workflows"
    wf.mkdir()
    _write(wf, "watch", {
        "matrix": [{"NAME": "alpha"}, {"NAME": "beta"}, {"NAME": "gamma"}],
        "source": {"kind": "http", "config": {"url": "https://${NAME}.x"}},
        "schedule": {"mode": "once"},
    })
    app = create_jobs_app(run_engine=False, workflows_dir=str(wf))
    with TestClient(app) as c:
        loaded = set(c.get("/health").json()["workflows"]["loaded"])
        assert {"watch-alpha", "watch-beta", "watch-gamma"} <= loaded
        ids = {j["id"] for j in c.get("/jobs").json()}
        assert {"watch-alpha", "watch-beta", "watch-gamma"} <= ids


def test_malformed_matrix_lands_in_failed(db, tmp_path, caplog):
    wf = tmp_path / "workflows"
    wf.mkdir()
    _write(wf, "bad", {
        "matrix": 5,
        "source": {"kind": "http", "config": {"url": "https://x"}},
    })
    # a sibling valid workflow still loads alongside the bad one
    _write(wf, "good", {
        "source": {"kind": "http", "config": {"url": "https://ok.x"}},
        "schedule": {"mode": "once"},
    })
    app = create_jobs_app(run_engine=False, workflows_dir=str(wf))
    with TestClient(app) as c:
        health = c.get("/health").json()
        # a bad matrix is reported, not fatal: startup still succeeds
        assert health["ok"] is True
        wfs = health["workflows"]
        # the bad file is surfaced in the non-fatal failed channel...
        assert any(f["file"].endswith("bad.yaml") for f in wfs["failed"])
        bad = next(f for f in wfs["failed"] if f["file"].endswith("bad.yaml"))
        assert "matrix" in bad["error"]
        # ...while the valid sibling still loads
        assert "good" in wfs["loaded"]
        assert not any(j["id"].startswith("bad") for j in c.get("/jobs").json())


def test_duplicate_id_lands_in_failed(db, tmp_path):
    wf = tmp_path / "workflows"
    wf.mkdir()
    # a matrix whose entries collide on an explicit identity is non-fatal:
    # reported under failed with the file + offending id, siblings still load.
    _write(wf, "dup", {
        "matrix": [{"id": "same"}, {"id": "same"}],
        "source": {"kind": "http", "config": {"url": "https://x"}},
    })
    _write(wf, "good", {
        "source": {"kind": "http", "config": {"url": "https://ok.x"}},
        "schedule": {"mode": "once"},
    })
    app = create_jobs_app(run_engine=False, workflows_dir=str(wf))
    with TestClient(app) as c:
        health = c.get("/health").json()
        assert health["ok"] is True
        wfs = health["workflows"]
        bad = next(f for f in wfs["failed"] if f["file"].endswith("dup.yaml"))
        assert "duplicate job id" in bad["error"] and "same" in bad["error"]
        assert "good" in wfs["loaded"]
        assert not any(j["id"] == "same" for j in c.get("/jobs").json())


# --- Track 1: defaults deep-merge composes with matrix fan-out ------------- #

def test_defaults_merged_into_every_matrix_job(tmp_path):
    # A shared ``defaults:`` block is deep-merged under each template *before*
    # the matrix fans out, so every expanded job inherits the common settings
    # while its own keys (deep) override them.
    fp = tmp_path / "watch.yaml"
    fp.write_text(yaml.safe_dump({
        "defaults": {
            "source": {"kind": "http", "config": {"timeout": 30}},
            "schedule": {"mode": "once"},
        },
        "jobs": [{
            "matrix": [
                {"NAME": "alpha", "URL": "https://a.x"},
                {"NAME": "beta", "URL": "https://b.x"},
            ],
            "id": "watch-${NAME}",
            "source": {"config": {"url": "${URL}"}},
        }],
    }, sort_keys=False))
    specs = _specs_from_workflow_file(fp)
    assert [s.id for s in specs] == ["watch-alpha", "watch-beta"]
    for s, url in zip(specs, ["https://a.x", "https://b.x"]):
        # kind + shared config come from defaults; url is the per-entry override,
        # and the deep-merge keeps both keys under ``source.config``.
        assert s.source.kind == "http"
        assert s.source.config == {"timeout": 30, "url": url}
        assert s.schedule.mode == "once"


def test_defaults_apply_to_cartesian_matrix(tmp_path):
    fp = tmp_path / "grid.yaml"
    fp.write_text(yaml.safe_dump({
        "defaults": {"source": {"kind": "http"}, "schedule": {"mode": "once"}},
        "jobs": [{
            "id": "g-${REGION}-${ENV}",
            "for_each": {"REGION": ["us", "eu"], "ENV": ["prod", "dev"]},
            "source": {"config": {"url": "https://${REGION}.${ENV}/x"}},
        }],
    }, sort_keys=False))
    specs = _specs_from_workflow_file(fp)
    assert [s.id for s in specs] == [
        "g-us-prod", "g-us-dev", "g-eu-prod", "g-eu-dev",
    ]
    assert all(s.source.kind == "http" for s in specs)
    assert specs[0].source.config["url"] == "https://us.prod/x"


def test_defaults_merge_into_single_template_matrix(tmp_path):
    # The natural one-file form: a top-level ``defaults:`` beside a top-level
    # ``matrix:`` (no wrapping ``jobs:`` list). Defaults are still deep-merged
    # under the template before fan-out, so every expanded job inherits them.
    fp = tmp_path / "watch.yaml"
    fp.write_text(yaml.safe_dump({
        "defaults": {
            "transforms": [{"kind": "select", "config": {"path": "payload"}}],
            "schedule": {"mode": "once"},
        },
        "matrix": [{"NAME": "one"}, {"NAME": "two"}],
        "source": {"kind": "http", "config": {"url": "https://${NAME}.x"}},
    }, sort_keys=False))
    specs = _specs_from_workflow_file(fp)
    assert [s.id for s in specs] == ["watch-one", "watch-two"]
    for s, url in zip(specs, ["https://one.x", "https://two.x"]):
        assert s.source.config["url"] == url
        assert s.transforms[0].kind == "select"
        assert s.schedule.mode == "once"


def test_defaults_without_matrix(tmp_path):
    # ``defaults:`` also merges into a plain (non-matrix) ``jobs:`` list.
    fp = _write(tmp_path, "plain", {
        "defaults": {"schedule": {"mode": "once"}},
        "jobs": [
            {"id": "one", "source": {"kind": "http", "config": {"url": "https://1.x"}}},
            {"id": "two", "source": {"kind": "http", "config": {"url": "https://2.x"}}},
        ],
    })
    specs = _specs_from_workflow_file(fp)
    assert [s.id for s in specs] == ["one", "two"]
    assert all(s.schedule.mode == "once" for s in specs)


def test_defaults_non_mapping_raises(tmp_path):
    fp = tmp_path / "bad.yaml"
    fp.write_text(yaml.safe_dump({
        "defaults": ["not", "a", "mapping"],
        "jobs": [{
            "matrix": [{"NAME": "a"}],
            "source": {"kind": "http", "config": {"url": "https://${NAME}.x"}},
        }],
    }, sort_keys=False))
    with pytest.raises(WorkflowIncludeError) as ei:
        _specs_from_workflow_file(fp)
    assert "defaults" in str(ei.value) and "mapping" in str(ei.value)


def test_malformed_defaults_lands_in_failed(db, tmp_path):
    wf = tmp_path / "workflows"
    wf.mkdir()
    fp = wf / "bad.yaml"
    fp.write_text(yaml.safe_dump({
        "defaults": 5,   # not a mapping
        "jobs": [{
            "matrix": [{"NAME": "a"}],
            "source": {"kind": "http", "config": {"url": "https://${NAME}.x"}},
        }],
    }, sort_keys=False))
    _write(wf, "good", {
        "source": {"kind": "http", "config": {"url": "https://ok.x"}},
        "schedule": {"mode": "once"},
    })
    app = create_jobs_app(run_engine=False, workflows_dir=str(wf))
    with TestClient(app) as c:
        wfs = c.get("/health").json()["workflows"]
        assert any(f["file"].endswith("bad.yaml") for f in wfs["failed"])
        bad = next(f for f in wfs["failed"] if f["file"].endswith("bad.yaml"))
        assert "defaults" in bad["error"]
        assert "good" in wfs["loaded"]


def test_non_matrix_workflow_unchanged(db, tmp_path):
    # A plain workflow (no matrix) still loads as a single stem-id job.
    wf = tmp_path / "workflows"
    wf.mkdir()
    _write(wf, "plain", {
        "source": {"kind": "command", "config": {"argv": ["printf", "hi"]}},
        "schedule": {"mode": "once"},
    })
    app = create_jobs_app(run_engine=False, workflows_dir=str(wf))
    with TestClient(app) as c:
        assert c.get("/health").json()["workflows"]["loaded"] == ["plain"]
