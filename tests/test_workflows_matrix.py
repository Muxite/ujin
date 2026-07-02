"""Matrix / parameterized workflows — one template fanned out over a data list.

A workflow file carrying a ``matrix:`` (or ``for_each:``) list produces one
:class:`JobSpec` per parameter map, with that entry's ``{{ var }}`` placeholders
substituted into source/transforms/sinks/schedule BEFORE ``${ENV}`` expansion,
and the top-level ``defaults:`` map deep-merged under every entry. A malformed
matrix block is skipped and surfaced on ``GET /health`` rather than crashing.
"""
from __future__ import annotations

import pytest

from ujin.jobs.app import (
    WorkflowMatrixError,
    _load_workflows_dir,
    _specs_from_workflow_file,
)


def _write(d, stem: str, text: str):
    p = d / f"{stem}.yaml"
    p.write_text(text)
    return p


# ── fan-out cardinality + per-section substitution ──────────────────────────

MATRIX_WF = """\
matrix:
  - {site: github, host: "api.github.com", every: 300}
  - {site: gitlab, host: "gitlab.com", every: 600}
  - {site: bitbucket, host: "api.bitbucket.org", every: 900}
name: "watch-{{ site }}"
source:
  kind: http
  config:
    url: "https://{{ host }}/status"
transforms:
  - kind: select
    config: { path: "{{ site }}.body" }
sinks:
  - kind: webhook
    config: { url: "https://hooks.test/{{ site }}" }
schedule:
  mode: adaptive
  base: "{{ every }}"
"""


def test_matrix_fans_out_one_spec_per_entry(tmp_path):
    f = _write(tmp_path, "status-pages", MATRIX_WF)
    specs = _specs_from_workflow_file(f)

    assert len(specs) == 3
    # deterministic, stem-derived ids
    assert [s.id for s in specs] == ["status-pages-0", "status-pages-1", "status-pages-2"]
    assert [s.name for s in specs] == ["watch-github", "watch-gitlab", "watch-bitbucket"]


def test_matrix_substitutes_into_every_section(tmp_path):
    f = _write(tmp_path, "status-pages", MATRIX_WF)
    specs = _specs_from_workflow_file(f)
    s0 = specs[0]

    # source, transforms, sinks, schedule all carry the entry's vars
    assert s0.source.config["url"] == "https://api.github.com/status"
    assert s0.transforms[0].config["path"] == "github.body"
    assert s0.sinks[0].config["url"] == "https://hooks.test/github"
    # a whole-string placeholder keeps the entry's *type* (int, not "300")
    assert s0.schedule.base == 300
    assert isinstance(s0.schedule.base, int)

    assert specs[1].source.config["url"] == "https://gitlab.com/status"
    assert specs[1].schedule.base == 600


def test_for_each_is_an_alias_for_matrix(tmp_path):
    f = _write(
        tmp_path,
        "pages",
        "for_each:\n"
        "  - {q: cats}\n"
        "  - {q: dogs}\n"
        "source:\n"
        "  kind: http\n"
        "  config: { url: \"https://ex.test/s?q={{ q }}\" }\n",
    )
    specs = _specs_from_workflow_file(f)
    assert [s.source.config["url"] for s in specs] == [
        "https://ex.test/s?q=cats",
        "https://ex.test/s?q=dogs",
    ]


# ── composition with ${ENV} (matrix layer runs first) ───────────────────────


def test_matrix_composes_with_env_substitution(tmp_path, monkeypatch):
    monkeypatch.setenv("BASE_HOST", "prod.example.com")
    f = _write(
        tmp_path,
        "envwf",
        "matrix:\n"
        "  - {path: alpha}\n"
        "  - {path: beta}\n"
        "source:\n"
        "  kind: http\n"
        # matrix var resolves first, then the ${BASE_HOST} it sits next to
        "  config: { url: \"https://${BASE_HOST}/{{ path }}\" }\n",
    )
    specs = _specs_from_workflow_file(f)
    assert [s.source.config["url"] for s in specs] == [
        "https://prod.example.com/alpha",
        "https://prod.example.com/beta",
    ]


def test_matrix_value_can_carry_env_reference(tmp_path, monkeypatch):
    # A matrix value that *contains* ${VAR:-default} composes: matrix subst puts
    # the reference in place, env expansion then resolves it.
    monkeypatch.delenv("REGION", raising=False)
    f = _write(
        tmp_path,
        "regionwf",
        "matrix:\n"
        "  - {host: \"${REGION:-us-east}.svc\"}\n"
        "source:\n"
        "  kind: http\n"
        "  config: { url: \"https://{{ host }}/ping\" }\n",
    )
    specs = _specs_from_workflow_file(f)
    assert specs[0].source.config["url"] == "https://us-east.svc/ping"


# ── composition with top-level defaults (Track 1) ───────────────────────────


def test_matrix_receives_top_level_defaults_merge(tmp_path):
    f = _write(
        tmp_path,
        "defwf",
        "defaults:\n"
        "  schedule: { mode: adaptive, base: 3600, max: 86400 }\n"
        "  sinks:\n"
        "    - kind: sqlite\n"
        "matrix:\n"
        "  - {q: cats, base: 300}\n"
        "  - {q: dogs, base: 1200}\n"
        "source:\n"
        "  kind: http\n"
        "  config: { url: \"https://ex.test/s?q={{ q }}\" }\n"
        "schedule:\n"
        "  base: \"{{ base }}\"\n",
    )
    specs = _specs_from_workflow_file(f)

    # per-entry key wins over the default (base), other default keys survive
    assert specs[0].schedule.base == 300
    assert specs[1].schedule.base == 1200
    assert specs[0].schedule.max == 86400.0        # untouched default flows through
    assert specs[0].schedule.mode == "adaptive"
    # the default sink is inherited by every fanned-out job
    assert [s.kind for s in specs[0].sinks] == ["sqlite"]
    assert [s.kind for s in specs[1].sinks] == ["sqlite"]


def test_matrix_composes_with_include_fragment_and_defaults(tmp_path):
    # A matrix template may ALSO pull an `include:` fragment and a `defaults:`
    # block: matrix vars, the merged default, and the fragment all land on every
    # fanned-out job (per-entry keys still win over both layers).
    (tmp_path / "fragments").mkdir()
    (tmp_path / "fragments" / "sqlite-sink.yaml").write_text("sinks:\n  - kind: sqlite\n")
    f = _write(
        tmp_path,
        "incwf",
        "matrix:\n"
        "  - {site: a, every: 300}\n"
        "  - {site: b, every: 600}\n"
        "include: [fragments/sqlite-sink.yaml]\n"
        "defaults:\n"
        "  schedule: { mode: adaptive }\n"
        "name: \"watch-{{ site }}\"\n"
        "source:\n"
        "  kind: http\n"
        "  config: { url: \"https://{{ site }}.example.com\" }\n"
        "schedule:\n"
        "  base: \"{{ every }}\"\n",
    )
    specs = sorted(_specs_from_workflow_file(f), key=lambda s: s.id)

    assert [s.id for s in specs] == ["incwf-0", "incwf-1"]
    # matrix var → source, typed matrix var → schedule.base (stays int)
    assert specs[0].source.config["url"] == "https://a.example.com"
    assert specs[0].schedule.base == 300 and isinstance(specs[0].schedule.base, int)
    assert specs[1].schedule.base == 600
    # default (mode) and the included fragment (sqlite sink) reach every job
    assert specs[0].schedule.mode == "adaptive"
    assert [s.kind for s in specs[0].sinks] == ["sqlite"]
    assert [s.kind for s in specs[1].sinks] == ["sqlite"]


# ── malformed matrix → clear error, skipped, surfaced ───────────────────────


@pytest.mark.parametrize(
    "body",
    [
        "matrix: {}\nsource: {kind: http, config: {url: x}}\n",          # not a list
        "matrix: []\nsource: {kind: http, config: {url: x}}\n",          # empty list
        "matrix:\n  - not-a-map\nsource: {kind: http, config: {url: x}}\n",  # entry not a map
    ],
)
def test_malformed_matrix_raises_matrix_error(tmp_path, body):
    f = _write(tmp_path, "bad", body)
    with pytest.raises(WorkflowMatrixError):
        _specs_from_workflow_file(f)


@pytest.mark.parametrize(
    "url",
    [
        "\"https://ex/{{ missing }}\"",   # embedded placeholder (textual sub)
        "\"{{ missing }}\"",              # whole-string placeholder (typed sub)
    ],
)
def test_undefined_matrix_var_raises(tmp_path, url):
    f = _write(
        tmp_path,
        "bad",
        "matrix:\n  - {a: 1}\n"
        f"source:\n  kind: http\n  config: {{ url: {url} }}\n",
    )
    with pytest.raises(WorkflowMatrixError) as ei:
        _specs_from_workflow_file(f)
    assert "missing" in str(ei.value)


def test_malformed_matrix_is_skipped_and_surfaced(tmp_path):
    # A bad matrix file does not sink its neighbours and is reported to `failed`.
    _write(tmp_path, "bad", "matrix: []\nsource: {kind: http, config: {url: x}}\n")
    _write(
        tmp_path,
        "good",
        "matrix:\n  - {q: cats}\n"
        "source:\n  kind: http\n  config: { url: \"https://ex.test/{{ q }}\" }\n",
    )
    failed: list = []
    specs = _load_workflows_dir(str(tmp_path), failed=failed)

    assert [s.id for s in specs] == ["good-0"]
    assert len(failed) == 1
    assert failed[0]["id"] == "bad"
    assert failed[0]["file"] == "bad.yaml"
    assert "matrix" in failed[0]["error"]


# ── strict additivity: non-matrix files are unaffected ──────────────────────


def test_non_matrix_workflow_is_unchanged(tmp_path):
    f = _write(
        tmp_path,
        "plain",
        "source:\n  kind: http\n  config: { url: \"https://ex.test\" }\n"
        "schedule: { mode: once }\n",
    )
    specs = _specs_from_workflow_file(f)
    assert len(specs) == 1
    assert specs[0].id == "plain" and specs[0].name == "plain"
    assert specs[0].source.config["url"] == "https://ex.test"
    assert specs[0].schedule.mode == "once"


def test_load_workflows_dir_without_failed_arg_still_works(tmp_path):
    # default failed=None keeps the old positional signature working
    _write(
        tmp_path,
        "plain",
        "source:\n  kind: http\n  config: { url: \"https://ex.test\" }\n",
    )
    specs = _load_workflows_dir(str(tmp_path))
    assert [s.id for s in specs] == ["plain"]


# ── end-to-end through the app: fan-out loads, bad matrix surfaces on /health ─


def test_matrix_loads_and_surfaces_through_health(tmp_path, monkeypatch):
    fastapi = pytest.importorskip("fastapi")  # noqa: F841
    from fastapi.testclient import TestClient

    from ujin.jobs.app import create_jobs_app

    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    wf = tmp_path / "workflows"
    wf.mkdir()
    _write(
        wf,
        "pages",
        "matrix:\n  - {q: cats}\n  - {q: dogs}\n"
        "source:\n  kind: http\n  config: { url: \"https://ex.test/{{ q }}\" }\n"
        "schedule: { mode: once }\n",
    )
    _write(wf, "broken", "matrix: []\nsource: {kind: http, config: {url: x}}\n")

    app = create_jobs_app(run_engine=False, workflows_dir=str(wf))
    with TestClient(app) as c:
        health = c.get("/health").json()
        # both fanned-out jobs loaded; the malformed template is surfaced, not fatal
        assert set(health["workflows"]["loaded"]) == {"pages-0", "pages-1"}
        assert [f["id"] for f in health["workflows"]["failed"]] == ["broken"]
        assert "matrix" in health["workflows"]["failed"][0]["error"]

        ids = {j["id"] for j in c.get("/jobs").json()}
        assert {"pages-0", "pages-1"} <= ids


# ── duplicate job ids across a matrix are handled, not fatal ────────────────


def test_matrix_with_static_id_produces_duplicate_ids(tmp_path):
    # An explicit `id:` in the template is shared by every fanned-out entry (the
    # per-entry `<stem>-<index>` fallback only applies when `id` is unset), so a
    # static id collapses the whole matrix onto one job id.
    f = _write(
        tmp_path,
        "dupe",
        "matrix:\n  - {q: cats}\n  - {q: dogs}\n"
        "id: fixed-id\n"
        "source:\n  kind: http\n  config: { url: \"https://ex.test/{{ q }}\" }\n",
    )
    specs = _specs_from_workflow_file(f)

    assert [s.id for s in specs] == ["fixed-id", "fixed-id"]
    # each entry still got its own substituted body, only the id collides
    assert [s.source.config["url"] for s in specs] == [
        "https://ex.test/cats",
        "https://ex.test/dogs",
    ]


def test_duplicate_matrix_ids_upsert_last_wins_and_stay_non_fatal(tmp_path, monkeypatch):
    # Registering colliding ids must not crash the loader: the manager upserts,
    # so the last generated entry wins and exactly one job survives, while an
    # unrelated workflow in the same directory still loads.
    fastapi = pytest.importorskip("fastapi")  # noqa: F841
    from fastapi.testclient import TestClient

    from ujin.jobs.app import create_jobs_app

    monkeypatch.setenv("UJIN_JOBS_DB", str(tmp_path / "jobs.db"))
    wf = tmp_path / "workflows"
    wf.mkdir()
    _write(
        wf,
        "dupe",
        "matrix:\n  - {q: cats}\n  - {q: dogs}\n"
        "id: fixed-id\n"
        "source:\n  kind: http\n  config: { url: \"https://ex.test/{{ q }}\" }\n"
        "schedule: { mode: once }\n",
    )
    _write(
        wf,
        "solo",
        "source:\n  kind: http\n  config: { url: \"https://ex.test/solo\" }\n"
        "schedule: { mode: once }\n",
    )

    app = create_jobs_app(run_engine=False, workflows_dir=str(wf))
    with TestClient(app) as c:
        health = c.get("/health").json()
        # both colliding entries load without error; neither is a failure
        assert health["workflows"]["failed"] == []

        jobs = {j["id"]: j for j in c.get("/jobs").json()}
        # the collision collapses to a single job (last entry wins) alongside solo
        assert set(jobs) == {"fixed-id", "solo"}
        content = c.get("/jobs/fixed-id").json()
        assert content["spec"]["source"]["config"]["url"] == "https://ex.test/dogs"
