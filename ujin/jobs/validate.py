"""Offline workflow validation and editor schema. Optional imports stay lazy.

``validate_path`` returns every diagnostic (an empty list means success).
Directories follow the server's top-level YAML glob; fragments are resolved by
its include loader rather than treated as independent jobs. Files with named
``templates``/``fragments`` are ingest plans; other files use workflow semantics.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ValidationError:
    path: str
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.message}"


class _Locations:
    """Best-effort lookup in original YAML, including referenced fragments."""

    def __init__(self, path, text):
        self.path = path
        self.nodes = []
        self.failures = []
        self._read(path, text, set())

    def _read(self, path, text, seen):
        import yaml
        from .app import _resolve_fragment_path

        if path.resolve() in seen:
            return
        seen.add(path.resolve())
        node = yaml.compose(text)

        def walk(node, keys, ancestors):
            if id(node) in ancestors:  # YAML aliases may be recursive
                return
            ancestors = ancestors | {id(node)}
            self.nodes.append((path, keys, node))
            if isinstance(node, yaml.MappingNode):
                for key, value in node.value:
                    walk(value, (*keys, key.value), ancestors)
                    if key.value in ("include", "use"):
                        refs = value.value if isinstance(value, yaml.SequenceNode) else [value]
                        for ref in refs:
                            if isinstance(ref, yaml.ScalarNode):
                                target = _resolve_fragment_path(ref.value, path)
                                if target.is_file():
                                    try:
                                        self._read(target, target.read_text(encoding="utf-8"), seen)
                                    except (OSError, yaml.YAMLError) as exc:
                                        self.failures.append((str(target), str(exc)))
            elif isinstance(node, yaml.SequenceNode):
                for i, value in enumerate(node.value):
                    walk(value, (*keys, i), ancestors)

        if node is not None:
            walk(node, (), set())

    def find(self, index, keys=(), value=None):
        candidates = []
        for path, trail, node in self.nodes:
            if keys and tuple(trail[-len(keys):]) != tuple(keys):
                continue
            if value is not None and getattr(node, "value", None) != str(value):
                continue
            if trail[:1] == ("jobs",) and trail[1:2] != (index,):
                continue
            if trail and isinstance(trail[0], int) and trail[0] != index:
                continue
            scoped = trail[:2] == ("jobs", index) or trail[:1] == (index,)
            candidates.append((scoped, path == self.path, path, node.start_mark.line + 1))
        if candidates:
            _, _, path, line = max(candidates, key=lambda c: c[:2])
            return str(path), line
        if keys:
            return self.find(index, keys[:-1])
        return str(self.path), 1


def _body_errors(body, registry):
    """Yield (key path, scalar value, message) without constructing factories."""
    from .model import ScheduleSpec

    for key, category in (("source", "source"), ("transforms", "transform"), ("sinks", "sink")):
        items = [body.get(key)] if key == "source" else body.get(key, [])
        if not isinstance(items, list):
            yield (key,), None, f"{key} must be a list"
            continue
        for i, item in enumerate(items):
            trail = (key,) if key == "source" else (key, i)
            if not isinstance(item, dict):
                yield trail, None, f"{category} must be a mapping with a kind"
                continue
            kind = item.get("kind")
            if not isinstance(kind, str) or not kind:
                yield (*trail, "kind"), None, f"{category} requires a non-empty kind"
            elif not registry.has(category, kind):
                yield (*trail, "kind"), kind, f"unknown {category} kind {kind!r}"
            if not isinstance(item.get("config", {}), dict):
                yield (*trail, "config"), None, f"{category} config must be a mapping"
            for extra in item.keys() - {"kind", "config"}:
                yield (*trail, extra), None, f"unknown {category} key {extra!r}"
    schedule = body.get("schedule", {})
    if not isinstance(schedule, dict):
        yield ("schedule",), None, "schedule must be a mapping"
    else:
        for key, value in schedule.items():
            if key == "mode":
                if value not in ("adaptive", "cron", "once"):
                    yield ("schedule", key), value, "schedule mode must be adaptive, cron, or once"
            elif key not in {f.name for f in fields(ScheduleSpec)}:
                yield ("schedule", key), None, f"unknown schedule key {key!r}"
            elif key not in ("cron", "jitter") and (isinstance(value, bool) or not isinstance(value, (int, float))):
                yield ("schedule", key), None, f"schedule {key} must be a number"
        for key in ("cron", "jitter"):
            if key in schedule and schedule[key] is not None and not isinstance(schedule[key], str):
                yield ("schedule", key), None, f"schedule {key} must be a string"
    for key in ("name", "id"):
        if key in body and not isinstance(body[key], str):
            yield (key,), None, f"{key} must be a string"
    if not isinstance(body.get("enabled", True), bool):
        yield ("enabled",), None, "enabled must be a boolean"


def _plan_bodies(data, path, report):
    from .plan import parse_plan, _apply_templates, _matrix_combos, _build_body

    # Let the actual parser validate the envelope, without stopping at a job.
    parse_plan({**data, "jobs": []}, source=str(path))
    jobs = data.get("jobs")
    if not isinstance(jobs, list):
        raise ValueError("jobs must be a list of mappings")
    seq = 0
    for i, job in enumerate(jobs):
        try:
            if not isinstance(job, dict):
                raise ValueError("job must be a mapping")
            merged = _apply_templates(data.get("defaults") or {}, data.get("templates", data.get("fragments")) or {}, job, i, str(path))
            matrix = merged.pop("matrix", None)
            combos = [{}] if matrix is None else _matrix_combos(matrix, i, str(path))
            for local, combo in enumerate(combos):
                yield i, _build_body(merged, combo, matrix is not None, local, seq, path.stem)
                seq += 1
        except (ValueError, TypeError, AttributeError) as exc:
            report(i, exc)


def validate_path(path: str | Path, *, plugins: str | Path | None = None) -> list[ValidationError]:
    """Validate a file/directory without running jobs. Plugins import only on request.

    Plugin modules execute their registration code, exactly as on server startup.
    Registry changes are restored afterwards so validation is safe to repeat.
    """
    try:
        import yaml  # noqa: F401 -- validate needs the optional YAML extra
    except ImportError:
        return [ValidationError(str(path), 1, "validation requires PyYAML: pip install 'ujin[yaml]'")]

    from ujin.registry import register
    from ujin.plugins.loader import load_plugins

    saved = {key: dict(value) for key, value in register._maps.items()}
    errors = []
    try:
        if plugins is not None:
            status = load_plugins(plugins)
            errors.extend(ValidationError(str(plugins), 1, f"plugin {name!r} failed to load") for name in status["failed"])
        root = Path(path)
        files = sorted(root.glob("*.y*ml")) if root.is_dir() else [root]
        for file in files:
            errors.extend(_validate_file(file, register))
    finally:
        register._maps = saved
    return errors


def _validate_file(path, registry):
    import re
    import yaml
    from .app import (
        WorkflowIncludeError, _expand_env, _has_matrix, _file_directives,
        _workflow_bodies,
    )
    from .model import JobSpec

    errors = []
    locations = None

    def report(index, exc, keys=(), value=None):
        message = str(exc)
        hint = re.sub(r"^ingest plan \(.*?\): ", "", message)
        if not keys and "unknown template" in hint:
            keys = ("use",)
        if not keys:
            keys = next(((key,) for key in ("matrix", "for_each", "include", "use", "defaults", "source") if re.search(r"\b" + key + r"\b", hint)), ())
        file, line = locations.find(index, keys, value) if locations else (str(path), 1)
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            line = mark.line + 1
        if locations:
            for fragment, failure in locations.failures:
                if failure == message:
                    file = fragment
                    break
        error = ValidationError(file, line, message)
        if error not in errors:
            errors.append(error)

    try:
        text = path.read_text(encoding="utf-8")
        locations = _Locations(path, text)
        peek = yaml.safe_load(text) or {}  # mirror the server loader: empty file == {}
        if isinstance(peek, dict) and ("templates" in peek or "fragments" in peek):
            bodies = _plan_bodies(peek, path, report)
        else:
            matrix = _has_matrix(peek)
            data = peek if matrix else (yaml.safe_load(_expand_env(text)) or {})
            defaults, includes, data = _file_directives(data)
            bodies = _workflow_bodies(data, defaults, includes, path, matrix, on_error=report)
        for index, body in bodies:
            diagnostics = list(_body_errors(body, registry))
            for keys, value, message in diagnostics:
                report(index, ValueError(message), keys, value)
            if not diagnostics:
                try:
                    JobSpec.from_dict(body)
                except (ValueError, TypeError, KeyError) as exc:
                    report(index, exc)
    except (OSError, ValueError, TypeError, AttributeError,
            yaml.YAMLError, WorkflowIncludeError) as exc:
        report(0, exc)
    return errors


def workflow_schema() -> dict[str, Any]:
    """Generate a deterministic Draft 2020-12 schema for workflows and plans.

    Fragments and job entries may be partial because defaults/includes/templates
    can supply their fields. The CLI checks the fully resolved required source.
    Config mappings remain open: each factory owns its configuration contract.
    """
    from ujin.registry import Registry, _install_builtins
    from .model import JobSpec, ScheduleSpec

    registry = Registry()
    _install_builtins(registry)
    definitions = {}
    for category in ("source", "transform", "sink"):
        definitions[category] = {
            "type": "object", "additionalProperties": False,
            "properties": {
                "kind": {"anyOf": [{"enum": registry.available(category)}, {"type": "string", "pattern": "^plugin:.+$"}]},
                "config": {"type": "object"},
            },
        }
    schedule = ScheduleSpec()
    definitions["schedule"] = {
        "type": "object", "additionalProperties": False,
        "properties": {
            f.name: {"type": ["string", "null"] if f.name == "cron" else "string" if f.name in ("mode", "jitter") else "number",
                     "default": getattr(schedule, f.name)}
            for f in fields(schedule)
        },
    }
    definitions["schedule"]["properties"]["mode"]["enum"] = ["adaptive", "cron", "once"]
    def ref(name):
        return {"$ref": f"#/$defs/{name}"}

    properties = {"name": {"type": "string"}, "id": {"type": "string"}, "source": ref("source"),
                  "transforms": {"type": "array", "items": ref("transform")},
                  "sinks": {"type": "array", "items": ref("sink")},
                  "schedule": ref("schedule"), "enabled": {"type": "boolean"},
                  "created_at": {"type": "number"}}
    assert set(properties) == {f.name for f in fields(JobSpec)}
    paths = {"anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]}
    matrix = {"anyOf": [{"type": "array", "items": {"type": "object"}},
                         {"type": "object", "additionalProperties": {"type": "array"}}]}
    properties.update({"include": paths, "use": paths, "matrix": matrix, "for_each": matrix})
    definitions["job"] = {"type": "object", "properties": properties, "additionalProperties": False}
    shared = {"defaults": ref("job"), "include": paths, "use": paths}
    definitions["workflow"] = {"type": "object", "properties": {**properties, **shared}, "additionalProperties": False}
    definitions["fleet"] = {
        "type": "object", "required": ["jobs"], "additionalProperties": False,
        "properties": {**shared, "jobs": {"type": "array", "items": ref("job")},
                       "templates": {"type": "object", "additionalProperties": ref("job")},
                       "fragments": {"type": "object", "additionalProperties": ref("job")}},
    }
    return {"$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "ujin workflow / ingest plan", "$defs": definitions,
            "anyOf": [ref("workflow"), ref("fleet"), {"type": "array", "items": ref("job")}]}


def _cmd_validate(args) -> int:
    import json

    if args.print_schema:
        print(json.dumps(workflow_schema(), indent=2, sort_keys=True))
        return 0
    errors = [error for path in args.paths for error in validate_path(path, plugins=args.plugins)]
    if not args.paths:
        errors.append(ValidationError("validate", 1, "provide at least one PATH or --print-schema"))
    if args.format == "json":
        print(json.dumps([asdict(error) for error in errors], indent=2))
    else:
        for error in errors:
            print(error)
    return int(bool(errors))
