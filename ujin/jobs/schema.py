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
            "tags": {"$ref": "#/$defs/tags"},
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
    tags = {
        "description": (
            "free-form labels for tag-based selection (--tag/--exclude-tag): a list "
            "of strings, or a single string. Templated values (e.g. {{ region }}) "
            "resolve per matrix/for_each entry."
        ),
        "type": ["array", "string"],
        "items": {"type": "string"},
    }
    schema_root = {
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
            "tags": {"$ref": "#/$defs/tags"},
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
            "tags": tags,
        },
    }
    return schema_root


# ── minimal, stdlib-only validator ──────────────────────────────────────────── #
#
# ``ujin plan validate --schema`` checks the *unresolved* document against
# :func:`plan_schema` without depending on a third-party validator. We hand-roll a
# tiny walker over the schema dict rather than pull in ``jsonschema`` so installing
# and running ujin needs no new mandatory dependency. The walker interprets only the
# handful of constructs :func:`plan_schema` actually uses — ``type``, ``properties``,
# ``items``, ``$ref`` (into ``$defs``), and ``anyOf`` — which is exactly enough to
# reject a structurally malformed plan (a scalar document, a non-list ``jobs:``, a
# string ``source:``) while accepting every plan the loader would. ``additionalProperties``
# stays open in the schema, so unknown keys are never reported here — that stays the
# job of the advisory ``--strict`` unknown-key check.


def _json_type_name(value: Any) -> str:
    """Return the JSON Schema type name for a YAML/JSON-decoded Python value."""
    if value is None:
        return "null"
    if isinstance(value, bool):  # bool before int — bool is a subclass of int
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _matches_type(value: Any, type_name: str) -> bool:
    """Return True when *value* satisfies a single JSON Schema ``type`` name."""
    if type_name == "object":
        return isinstance(value, dict)
    if type_name == "array":
        return isinstance(value, list)
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_name == "null":
        return value is None
    return True  # an unrecognized type name constrains nothing


def _resolve_ref(subschema: dict, defs: dict) -> dict:
    """Follow a ``$ref`` into ``$defs`` (siblings like ``description`` are ignored)."""
    ref = subschema.get("$ref")
    if not ref:
        return subschema
    return defs.get(ref.rsplit("/", 1)[-1], {})


def _expected_types(subschema: dict, defs: dict) -> list[str]:
    """List the JSON type names *subschema* accepts (across ``anyOf`` branches)."""
    names: list[str] = []
    if "anyOf" in subschema:
        for branch in subschema["anyOf"]:
            names.extend(_expected_types(_resolve_ref(branch, defs), defs))
    else:
        t = subschema.get("type")
        if isinstance(t, list):
            names.extend(t)
        elif t:
            names.append(t)
    seen: list[str] = []
    for n in names:  # dedupe, preserve order
        if n not in seen:
            seen.append(n)
    return seen


def _walk(value: Any, subschema: dict, path: str, defs: dict, errors: list[str]) -> None:
    subschema = _resolve_ref(subschema, defs)

    if "anyOf" in subschema:
        for branch in subschema["anyOf"]:
            trial: list[str] = []
            _walk(value, branch, path, defs, trial)
            if not trial:
                return  # value satisfies at least one branch
        expected = " or ".join(_expected_types(subschema, defs)) or "a different shape"
        errors.append(f"{path}: expected {expected}, got {_json_type_name(value)}")
        return

    types = _expected_types(subschema, defs)
    if types and not any(_matches_type(value, t) for t in types):
        errors.append(f"{path}: expected {' or '.join(types)}, got {_json_type_name(value)}")
        return  # type mismatch — descending further would only add noise

    props = subschema.get("properties")
    if props and isinstance(value, dict):
        for key, child in props.items():
            if key in value:
                _walk(value[key], child, f"{path}.{key}", defs, errors)

    items = subschema.get("items")
    if items and isinstance(value, list):
        for i, elem in enumerate(value):
            _walk(elem, items, f"{path}[{i}]", defs, errors)


def validate_document(document: Any, schema: "dict[str, Any] | None" = None) -> list[str]:
    """Return structural violations of *document* against the INGEST-PLAN schema.

    *document* is the **unresolved** YAML/JSON-decoded value (before ``defaults:``
    merge, ``include:`` splicing, or ``matrix:`` fan-out). The return value is a list
    of human-readable messages, each naming the offending location as a JSON path
    (e.g. ``$``, ``$.jobs``, ``$.jobs[0].source``); an empty list means the document
    conforms. Pure stdlib — it interprets the dict from :func:`plan_schema` directly
    and pulls in no third-party validator.
    """
    if schema is None:
        schema = plan_schema()
    errors: list[str] = []
    _walk(document, schema, "$", schema.get("$defs", {}), errors)
    return errors
