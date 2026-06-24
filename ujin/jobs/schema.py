"""JSON Schema (draft 2020-12) for the INGEST-PLAN / workflow file format.

Pure-stdlib: this module just builds and returns a plain ``dict`` describing the
*unresolved* file format that ``jobs-serve`` and the plan/workflow loaders accept
— the per-job shape (``source`` / ``transforms`` / ``sinks`` / ``schedule``), the
top-level ``defaults:`` block, ``include:``/``use:`` fragment references,
``matrix:``/``for_each:`` fan-out, ``vars:`` defaults, and ``profiles:`` overlays.

It is intentionally **permissive**, mirroring the loader. A plan file is resolved
through deep-merged ``defaults:``, spliced ``include:`` fragments, and
``matrix:`` fan-out *before* a :class:`~ujin.jobs.model.JobSpec` is built, so an
individual job mapping in the file may legitimately omit ``source:`` / ``name:``
(they arrive from ``defaults:`` or the filename stem). The schema therefore
constrains the document shape and the *types* of recognized keys rather than
requiring keys that resolution supplies — enough to reject a structurally
malformed plan (a scalar document, a non-list ``jobs:``, a string ``source:``)
without rejecting any plan the loader would accept. ``additionalProperties`` stays
open so the advisory unknown-key check in ``ujin plan validate --strict`` — not
this schema — owns typo detection.

This is read-only metadata: it never feeds ``JobSpec.from_dict`` or the loaders.
``ujin plan schema`` emits it; nothing on the resolution path imports it.
"""
from __future__ import annotations

from typing import Any

#: The JSON Schema dialect the emitted document declares.
SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
#: A stable identifier for the schema (informational; need not resolve).
SCHEMA_ID = "https://ujin.dev/schemas/ingest-plan.schema.json"


def plan_schema() -> dict[str, Any]:
    """Return the JSON Schema (draft 2020-12) for an INGEST-PLAN / workflow file.

    A fresh ``dict`` is built on each call (callers may mutate it freely); its
    contents are deterministic and authored in a fixed key order, so serializing
    it is byte-for-byte reproducible.
    """
    spec_item = {
        "type": "object",
        "description": (
            "A source / transform / sink entry: a {kind, config} mapping, or an "
            "{include}/{use} fragment reference spliced in before the job builds."
        ),
        "properties": {
            "kind": {
                "type": "string",
                "description": "a built-in name or a plugin:<name> reference",
            },
            "config": {"type": "object", "description": "kind-specific settings"},
            "include": {"$ref": "#/$defs/fragmentRef"},
            "use": {"$ref": "#/$defs/fragmentRef"},
        },
        "additionalProperties": True,
    }
    schedule = {
        "type": "object",
        "description": "When the source fires (e.g. adaptive | cron | once | poll).",
        "properties": {
            "mode": {"type": "string"},
            "base": {"type": "number"},
            "min": {"type": "number"},
            "max": {"type": "number"},
            "grow": {"type": "number"},
            "shrink": {"type": "number"},
            "jitter": {"type": "string"},
            "cron": {"type": ["string", "null"]},
            "include": {"$ref": "#/$defs/fragmentRef"},
            "use": {"$ref": "#/$defs/fragmentRef"},
        },
        "additionalProperties": True,
    }
    matrix = {
        "description": (
            "Fan one entry into many jobs: a list of variable maps, a "
            "{from: <path>} data-file reference, or a mapping of axis-name -> "
            "list-of-values (cartesian product), optionally carrying exclude:/"
            "include: filter lists. Each row substitutes into every {{ var }}."
        ),
        "anyOf": [
            {"type": "array", "description": "an inline list of variable maps"},
            {"type": "object", "description": "axes mapping, or {from: <path>}"},
        ],
    }
    fragment_ref = {
        "description": (
            "A fragment file path (or list of paths) to splice in, resolved "
            "relative to this file's directory then $UJIN_WORKFLOWS_DIR."
        ),
        "anyOf": [
            {"type": "string"},
            {"type": "array", "items": {"type": "string"}},
        ],
    }
    job = {
        "type": "object",
        "description": (
            "One declarative job. In an unresolved file any key may instead be "
            "supplied by the top-level defaults: block (or the filename stem, for "
            "name/id), so no key is required here."
        ),
        "properties": {
            "name": {"type": "string"},
            "id": {"type": "string"},
            "enabled": {"type": "boolean"},
            "created_at": {"type": "number"},
            "source": {"$ref": "#/$defs/specItem"},
            "transforms": {"type": "array", "items": {"$ref": "#/$defs/specItem"}},
            "sinks": {"type": "array", "items": {"$ref": "#/$defs/specItem"}},
            "schedule": {"$ref": "#/$defs/schedule"},
            "matrix": {"$ref": "#/$defs/matrix"},
            "for_each": {"$ref": "#/$defs/matrix"},
            "include": {"$ref": "#/$defs/fragmentRef"},
            "use": {"$ref": "#/$defs/fragmentRef"},
        },
        "additionalProperties": True,
    }
    return {
        "$schema": SCHEMA_DIALECT,
        "$id": SCHEMA_ID,
        "title": "ujin INGEST-PLAN / workflow file",
        "description": (
            "The file format ujin's plan/workflow loaders accept: a single job "
            "mapping (workflow file), a top-level list of job mappings, or a "
            "mapping with a jobs: list plus optional defaults:/vars:/profiles: and "
            "include:/use: fragment references. Describes the unresolved file — "
            "defaults: deep-merge, include: splicing, and matrix: fan-out all run "
            "before each JobSpec is built. Emitted by `ujin plan schema`."
        ),
        "type": ["object", "array"],
        "properties": {
            "jobs": {
                "type": "array",
                "description": "the jobs this file declares",
                "items": {"$ref": "#/$defs/job"},
            },
            "defaults": {
                "description": "job keys deep-merged under every job (per-job keys win)",
                "$ref": "#/$defs/job",
            },
            "vars": {
                "type": "object",
                "description": "default values for {{ var }} placeholders nothing else sets",
            },
            "profiles": {
                "type": "object",
                "description": "named overlays deep-merged over defaults: when --profile selects one",
                "additionalProperties": {"$ref": "#/$defs/job"},
            },
            "include": {"$ref": "#/$defs/fragmentRef"},
            "use": {"$ref": "#/$defs/fragmentRef"},
            # Job-level keys, for the single-job workflow-file form (no jobs: key).
            "name": {"type": "string"},
            "id": {"type": "string"},
            "enabled": {"type": "boolean"},
            "created_at": {"type": "number"},
            "source": {"$ref": "#/$defs/specItem"},
            "transforms": {"type": "array", "items": {"$ref": "#/$defs/specItem"}},
            "sinks": {"type": "array", "items": {"$ref": "#/$defs/specItem"}},
            "schedule": {"$ref": "#/$defs/schedule"},
            "matrix": {"$ref": "#/$defs/matrix"},
            "for_each": {"$ref": "#/$defs/matrix"},
        },
        "items": {"$ref": "#/$defs/job"},
        "additionalProperties": True,
        "$defs": {
            "job": job,
            "specItem": spec_item,
            "schedule": schedule,
            "matrix": matrix,
            "fragmentRef": fragment_ref,
        },
    }
