"""Tripwire for the external-config / INGEST-PLAN subsystem (offline).

This subsystem (the ``ujin plan`` CLI, the ``ujin/jobs/schema.py`` JSON Schema,
the ``jobs-serve --plan`` / ``$UJIN_INGEST_PLAN`` external loader, and the
``defaults:`` deep-merge + ``include:``/``use:`` fragments + ``matrix:``/
``for_each:`` fan-out engine) has been silently reverted more than once by
coverage merges that branched off a stale base. Each revert also regressed
``ujin.__version__`` back to a much older value, and dropped the docs/examples
that describe the subsystem (the README feature list, ``docs/INGEST_PLAN.md``,
``docs/WORKFLOWS.md``, the external-config recipe, and the bundled examples).

These assertions exist so any such revert turns the gate RED instead of merging
quietly. They deliberately check *behaviour and wiring*, not internals: the
public version string, that the schema module imports, that every ``plan``
subcommand is registered, that ``jobs-serve`` still advertises ``--plan``, that
the shipped ``examples/ingest-plan.yaml`` still fans out to its expected jobs
with ``defaults:`` deep-merged and per-job keys winning, and that the README
feature list plus the docs still *document* the includes/defaults, matrix, and
external ingest-plan capabilities (acceptance-criterion #8 — a docs-only revert
must fail the gate too, not just a code revert). If you are reading this because
the test failed: the external-config subsystem, its version, or its docs were
reverted — restore them, do not delete this file.

All checks are offline and deterministic — they exercise the pure loader and the
argparse wiring directly and never touch the network.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_EXAMPLE = _ROOT / "examples" / "ingest-plan.yaml"
_README = _ROOT / "README.md"
_DOCS = _ROOT / "docs"

# Every subcommand `ujin plan` must expose. A registered subcommand exits 0 on
# `--help`; an unknown one makes argparse exit 2 — so the exit code is the probe.
_PLAN_SUBCOMMANDS = ("validate", "render", "diff", "schema", "env", "eject", "init")


def test_version_pinned_to_0_47_0():
    """The restore pins ujin.__version__ to 0.47.0; a stale-base revert drops it."""
    import ujin

    assert ujin.__version__ == "0.47.0", (
        "ujin.__version__ regressed away from 0.47.0 — the external-config "
        f"restore was reverted (got {ujin.__version__!r})."
    )


def test_pyproject_version_agrees_with_package():
    """pyproject.toml version and ujin.__version__ must agree (no stale-base split).

    A stale-base revert regressed ``ujin.__version__`` while leaving the static
    ``pyproject.toml`` ``version`` on yet another older line — so the package and
    its packaging metadata disagreed. Pin them together at the restored 0.47.0.
    """
    import ujin

    text = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    # The `[project]` table's literal `version = "..."` line (first match).
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("version") and "=" in stripped:
            pyproject_version = stripped.split("=", 1)[1].strip().strip('"').strip("'")
            break
    else:  # pragma: no cover - only hit if the version line is removed entirely
        pytest.fail("pyproject.toml has no `version = \"...\"` line")

    assert pyproject_version == ujin.__version__ == "0.47.0", (
        "pyproject.toml version and ujin.__version__ disagree or regressed "
        f"(pyproject={pyproject_version!r}, package={ujin.__version__!r}) — "
        "the external-config restore was partially reverted."
    )


def test_schema_module_importable_and_wired():
    """ujin/jobs/schema.py imports and exposes the INGEST-PLAN JSON Schema API."""
    schema = importlib.import_module("ujin.jobs.schema")
    assert hasattr(schema, "plan_schema"), "schema.plan_schema() is missing"
    assert hasattr(schema, "validate_document"), "schema.validate_document() is missing"

    doc = schema.plan_schema()
    assert isinstance(doc, dict) and doc.get("$schema"), "plan_schema() must return a JSON Schema dict"
    # A well-formed minimal plan validates with no errors.
    assert schema.validate_document({"jobs": [{"id": "x"}]}) == []


def test_external_loader_and_env_constant_present():
    """The jobs app exposes the INGEST-PLAN loader and the $UJIN_INGEST_PLAN env name."""
    app = importlib.import_module("ujin.jobs.app")
    assert app.INGEST_PLAN_ENV == "UJIN_INGEST_PLAN"
    # The pure loader plus the deep-merge / fragment / matrix engine helpers.
    for name in (
        "_load_ingest_plan",
        "_plan_entries",
        "_expand_matrix_entries",
        "_specs_from_workflow_file",
        "WorkflowIncludeError",
    ):
        assert hasattr(app, name), f"ujin.jobs.app.{name} is missing — engine reverted"


def test_plan_cli_subcommands_registered(capsys):
    """`ujin plan` registers every subcommand (validate/render/diff/schema/env/eject/init)."""
    import ujin.cli as cli

    for sub in _PLAN_SUBCOMMANDS:
        with pytest.raises(SystemExit) as exc:
            cli.main(["plan", sub, "--help"])
        assert exc.value.code == 0, f"`ujin plan {sub}` is not registered"
        capsys.readouterr()  # drain the help text


def test_jobs_serve_advertises_plan_flag(capsys):
    """jobs-serve still offers --plan, the external-config mount point."""
    import ujin.cli as cli

    with pytest.raises(SystemExit) as exc:
        cli.main(["jobs-serve", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--plan" in out, "jobs-serve --plan flag (external-config loader) was removed"


def test_example_plan_fans_out_with_merged_defaults():
    """examples/ingest-plan.yaml resolves to its fanned-out jobs with defaults deep-merged.

    Exercises the whole engine end-to-end: a top-level ``defaults:`` block
    deep-merged under every job, ``include:`` fragments, and a ``matrix:``
    fan-out — with per-job keys winning over the defaults.
    """
    app = importlib.import_module("ujin.jobs.app")
    specs = list(app._load_ingest_plan(_EXAMPLE))
    by_id = {s.id: s.to_dict() for s in specs}

    # matrix: fan-out — crossref plus one job per feed slug.
    assert set(by_id) == {"crossref", "feed-tech", "feed-science", "feed-business"}

    crossref = by_id["crossref"]
    src = crossref["source"]["config"]
    # per-job key wins...
    assert src["url"] == "https://api.crossref.org/works?rows=50"
    # ...while sibling keys are deep-merged in from defaults (not clobbered).
    assert src["json_path"] == "items"
    assert crossref["source"]["kind"] == "api"
    # transforms + sinks + schedule all inherited from the defaults: block / fragments.
    assert [t["kind"] for t in crossref["transforms"]] == ["dedupe"]
    assert "sqlite" in [s["kind"] for s in crossref["sinks"]]
    assert crossref["schedule"]["mode"] == "adaptive"

    # The matrix substituted {{ slug }} into each feed's URL and added its jsonl sink.
    feed = by_id["feed-tech"]
    assert feed["source"]["config"]["url"] == "https://feeds.example.com/tech?rows=50"
    assert "jsonl" in [s["kind"] for s in feed["sinks"]]


def _readme_feature_list() -> str:
    """The intro numbered feature list — everything before the first ``## `` heading."""
    text = _README.read_text(encoding="utf-8")
    head, _, _ = text.partition("\n## ")
    return head


def test_readme_feature_list_documents_external_config():
    """README's feature list still describes includes/defaults, matrix, and ingest-plan.

    Acceptance-criterion #8: a docs revert that strips these from the feature
    list must fail the gate, not slip through behind green code tests. We assert
    on the intro feature list specifically (the region before the first ``##``
    section), so moving prose into a deeper section without keeping the headline
    capability list current still trips this wire.
    """
    features = _readme_feature_list()
    # includes / shared defaults
    assert "defaults:" in features, "README feature list dropped the `defaults:` deep-merge"
    assert "include:" in features or "use:" in features, (
        "README feature list dropped `include:`/`use:` fragments"
    )
    # matrix / for_each fan-out
    assert "matrix:" in features or "for_each:" in features, (
        "README feature list dropped `matrix:`/`for_each:` fan-out"
    )
    # external ingest-plan via env var / flag
    assert "INGEST-PLAN" in features or "ingest-plan" in features, (
        "README feature list dropped the INGEST-PLAN capability"
    )
    assert "UJIN_INGEST_PLAN" in features or "--plan" in features, (
        "README feature list dropped the `--plan` / `$UJIN_INGEST_PLAN` mount point"
    )


def test_docs_describe_external_config_capabilities():
    """The reference docs still document includes/defaults, matrix, and ingest-plan.

    ``docs/INGEST_PLAN.md`` is the full external-config reference and
    ``docs/WORKFLOWS.md`` is the per-workflow guide; both were dropped by past
    stale-base reverts. Each must still cover the three capabilities.
    """
    ingest = (_DOCS / "INGEST_PLAN.md").read_text(encoding="utf-8")
    for token in ("defaults:", "matrix:", "UJIN_INGEST_PLAN"):
        assert token in ingest, f"docs/INGEST_PLAN.md no longer documents `{token}`"
    assert "include:" in ingest or "use:" in ingest, (
        "docs/INGEST_PLAN.md no longer documents `include:`/`use:` fragments"
    )

    workflows = (_DOCS / "WORKFLOWS.md").read_text(encoding="utf-8")
    assert "defaults:" in workflows, "docs/WORKFLOWS.md no longer documents `defaults:`"
    assert "include:" in workflows or "use:" in workflows, (
        "docs/WORKFLOWS.md no longer documents `include:`/`use:` fragments"
    )
    assert "matrix:" in workflows or "for_each:" in workflows, (
        "docs/WORKFLOWS.md no longer documents `matrix:`/`for_each:` fan-out"
    )

    # The end-to-end external-config recipe must also survive a revert.
    assert (_DOCS / "recipes" / "external-config-flow.md").exists(), (
        "docs/recipes/external-config-flow.md (external-config walkthrough) was reverted"
    )
