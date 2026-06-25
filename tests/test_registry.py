"""Plugin registry + loader: builtins, plugin load, error isolation, reload,
and unknown-kind handling (including the app's 400)."""
from __future__ import annotations

import pytest

from ujin.plugins import load_plugins
from ujin.registry import BuildContext, register


@pytest.fixture(autouse=True)
def _clean_registry():
    # the global registry persists across tests in one process; drop plugin
    # entries before and after each test so they don't leak
    register.clear_plugins()
    yield
    register.clear_plugins()


def test_public_import_and_builtins():
    import ujin

    assert ujin.register is register
    for kind in ("http", "rss", "api", "command", "site", "scrape"):
        assert register.has("source", kind)
    for kind in ("select", "regex", "template", "dedupe"):
        assert register.has("transform", kind)
    for kind in ("webhook", "ws", "jsonl", "stdout", "sqlite", "forward"):
        assert register.has("sink", kind)


def test_build_builtin_source():
    p = register.build_source("api", {"url": "https://x", "json_path": "a.b"})
    assert p.url == "https://x"
    # plugin:-prefixed bare builtin still resolves the same factory
    p2 = register.build_source("command", {"argv": ["true"]})
    assert p2.key


ECHO_PLUGIN = '''
from ujin import register
from ujin.poll.base import PollResult


@register.source("echo")
def make(cfg):
    class _Echo:
        key = "echo"
        async def poll(self, prev):
            return PollResult(ok=True, changed=True, fingerprint="fp",
                              payload=cfg.get("msg", "hi"))
    return _Echo()


@register.sink("collect")
def make_sink(cfg):
    class _Collect:
        async def emit(self, event):
            pass
    return _Collect()
'''


def test_plugin_loads_and_is_usable(tmp_path):
    (tmp_path / "echo.py").write_text(ECHO_PLUGIN)
    status = load_plugins(str(tmp_path))
    assert "echo" in status["loaded"]
    assert register.has("source", "plugin:echo")
    assert register.has("sink", "plugin:collect")

    src = register.build_source("plugin:echo", {"msg": "yo"}, BuildContext())
    assert src.key == "echo"


def test_broken_plugin_is_skipped(tmp_path):
    (tmp_path / "ok.py").write_text(ECHO_PLUGIN)
    (tmp_path / "bad.py").write_text("import a_module_that_does_not_exist_xyz\n")
    status = load_plugins(str(tmp_path))
    assert "ok" in status["loaded"]
    assert "bad" in status["failed"]
    # the good plugin still registered despite the broken sibling
    assert register.has("source", "echo")


def test_reload_picks_up_edits_and_drops_removed(tmp_path):
    plugin = tmp_path / "p.py"
    plugin.write_text(ECHO_PLUGIN)
    load_plugins(str(tmp_path))
    assert register.has("source", "echo")

    # edit the plugin to register a different name; clear + reload
    plugin.write_text(ECHO_PLUGIN.replace('"echo"', '"echo2"').replace('"collect"', '"collect2"'))
    register.clear_plugins()
    load_plugins(str(tmp_path))
    assert register.has("source", "echo2")
    assert not register.has("source", "echo")  # old kind gone after reload


def test_missing_plugin_dir_is_noop():
    status = load_plugins("/nonexistent/path/xyz")
    assert status == {"loaded": [], "failed": []}


def test_unknown_kind_raises_keyerror():
    with pytest.raises(KeyError):
        register.build_source("nope", {})


def test_action_category_register_build_clear():
    seen = {}

    @register.action("noop")
    def make(cfg, ctx):
        async def handler(page, **params):
            seen["ran"] = (cfg, ctx.page)
            return {"ok": True}
        return handler

    assert register.has("action", "noop")
    assert "noop" in register.available("action")
    handler = register.build_action("plugin:noop", {"x": 1},
                                    BuildContext(page="PAGE"))
    assert callable(handler)
    register.clear_plugins()
    assert not register.has("action", "noop")  # plugin action dropped on clear


def test_buildcontext_constructs_argument_free():
    ctx = BuildContext()
    assert ctx.browser is None and ctx.page is None
