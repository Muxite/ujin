"""ujin plugins — load operator-supplied Python from a mounted volume.

A plugin is just a ``.py`` file (or a package dir) under ``UJIN_PLUGINS_DIR``
(default ``/plugins``) that registers kinds with the global
:data:`ujin.registry.register` at import time. See :mod:`ujin.plugins.loader`.

This is a *trusted-operator* mechanism — plugin code runs in-process with no
sandbox. Only mount code you trust.
"""
from __future__ import annotations

from .loader import load_plugins

__all__ = ["load_plugins"]
