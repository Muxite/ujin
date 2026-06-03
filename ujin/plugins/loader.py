"""Discover + import plugin modules so their ``@register.*`` decorators run.

``load_plugins(dir)`` imports every top-level ``*.py`` file and every package
directory (one with ``__init__.py``) under ``dir``. Each module is loaded in
isolation: a broken plugin is logged and skipped — it never aborts startup or
other plugins.

Reload semantics: modules are dropped from ``sys.modules`` before re-import, so
editing a plugin file and calling :func:`load_plugins` again re-executes it.
Plugin-contributed registry entries should be cleared first (the jobs app does
``register.clear_plugins()`` before reloading) so removed kinds disappear.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger("ujin.plugins")

_MODULE_PREFIX = "ujin_plugins."


def _discover(directory: Path) -> list[Path]:
    targets: list[Path] = []
    for entry in sorted(directory.iterdir()):
        if entry.name.startswith((".", "_")):
            continue
        if entry.is_file() and entry.suffix == ".py":
            targets.append(entry)
        elif entry.is_dir() and (entry / "__init__.py").exists():
            targets.append(entry / "__init__.py")
    return targets


def _load_one(path: Path, mod_name: str) -> None:
    sys.modules.pop(mod_name, None)  # drop any prior copy so edits re-execute
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not create import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)


def load_plugins(directory: str | os.PathLike | None = None) -> dict[str, list[str]]:
    """Import all plugins under ``directory`` (default: ``$UJIN_PLUGINS_DIR``).

    Returns ``{"loaded": [names], "failed": [names]}``. A missing directory is a
    no-op (returns empty lists).
    """
    raw = directory if directory is not None else os.environ.get("UJIN_PLUGINS_DIR", "/plugins")
    root = Path(raw)
    loaded: list[str] = []
    failed: list[str] = []
    if not root.is_dir():
        log.debug("plugin dir %s absent; nothing to load", root)
        return {"loaded": loaded, "failed": failed}

    for path in _discover(root):
        stem = path.parent.name if path.name == "__init__.py" else path.stem
        mod_name = f"{_MODULE_PREFIX}{stem}"
        try:
            _load_one(path, mod_name)
            loaded.append(stem)
            log.info("loaded plugin %s from %s", stem, path)
        except Exception as exc:  # noqa: BLE001
            failed.append(stem)
            sys.modules.pop(mod_name, None)
            log.warning("plugin %s failed to load: %s", stem, exc)
    return {"loaded": loaded, "failed": failed}
