"""Plugin registry + loader: builtins, plugin load, error isolation, reload,
and unknown-kind handling (including the app's 400)."""
from __future__ import annotations

import inspect
from unittest.mock import MagicMock

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


# --- decorator coverage: transform / scorer (lines 64, 70) ------------------- #

def test_transform_decorator_registers_and_builds():
    @register.transform("my_transform")
    def factory(cfg):
        return {"val": cfg.get("x")}

    assert register.has("transform", "my_transform")
    result = register.build_transform("my_transform", {"x": 7})
    assert result == {"val": 7}


def test_scorer_decorator_registers_and_builds():
    @register.scorer("my_scorer")
    def factory(cfg):
        return cfg.get("score", 0)

    assert register.has("scorer", "my_scorer")
    result = register.build_scorer("my_scorer", {"score": 42})
    assert result == 42


# --- inspect.signature() fallback (lines 113-114) ----------------------------- #

def test_inspect_signature_fallback_uses_single_param(monkeypatch):
    """When inspect.signature raises TypeError the factory is called as factory(cfg)."""
    monkeypatch.setattr(inspect, "signature", MagicMock(side_effect=TypeError("no sig")))

    @register.source("nosig_src")
    def factory(cfg):
        return cfg.get("v", "ok")

    result = register.build_source("nosig_src", {"v": "hit"})
    assert result == "hit"


# --- build_transform / build_sink / build_scorer via builtins (123, 126, 129, 299) --- #

def test_build_transform_builtin_select():
    t = register.build_transform("select", {"selector": "a"})
    assert type(t).__name__ == "SelectTransform"


def test_build_sink_builtin_stdout():
    from ujin.jobs.sinks import StdoutSink
    sink = register.build_sink("stdout", {}, BuildContext())
    assert isinstance(sink, StdoutSink)


def test_build_scorer_plugin():
    @register.scorer("pts")
    def factory(cfg):
        return cfg.get("pts", 0)

    assert register.build_scorer("pts", {"pts": 5}) == 5


# --- builtin source factory bodies -------------------------------------------- #

def test_build_source_http():
    p = register.build_source("http", {"url": "http://example.com"})
    assert p.url == "http://example.com"


def test_build_source_rss():
    p = register.build_source("rss", {"url": "http://feed.example.com/rss"})
    assert p.url == "http://feed.example.com/rss"


def test_build_source_site():
    p = register.build_source("site", {"url": "http://example.com"})
    assert p.url == "http://example.com"


def test_build_source_graphql():
    p = register.build_source("graphql", {"url": "http://api.example.com/gql", "query": "{items}"})
    assert p.url == "http://api.example.com/gql"


# --- scrape source (lines 180-184) -------------------------------------------- #

def test_build_source_scrape_raises_without_service():
    with pytest.raises(ValueError, match="scrape backend"):
        register.build_source("scrape", {"url": "http://x"}, BuildContext())


def test_build_source_scrape_with_service(monkeypatch):
    import ujin.poll.scrape as scrape_mod
    FakeScrape = MagicMock(return_value=MagicMock(key="scrape:x"))
    monkeypatch.setattr(scrape_mod, "ScrapePollable", FakeScrape)
    ctx = BuildContext(scrape_service=MagicMock())
    p = register.build_source("scrape", {"url": "http://x"}, ctx)
    assert p.key == "scrape:x"


# --- browser source (lines 190-197) ------------------------------------------- #

def test_build_source_browser(monkeypatch):
    import ujin.poll.browser as browser_mod
    fake = MagicMock(key="browser:test")
    monkeypatch.setattr(browser_mod, "BrowserPollable", MagicMock(return_value=fake))
    p = register.build_source("browser", {"url": "http://x"}, BuildContext())
    assert p.key == "browser:test"


# --- amazon_search: single term + multi-terms (lines 211-233) ----------------- #

def test_build_source_amazon_search_single():
    p = register.build_source("amazon_search", {"term": "laptop"})
    assert "laptop" in p.key


def test_build_source_amazon_search_multi_terms():
    from ujin.poll.multi import MultiPollable
    p = register.build_source("amazon_search", {"terms": ["laptop", "phone"]})
    assert isinstance(p, MultiPollable)


# --- amazon_category (lines 236-256) ------------------------------------------ #

def test_build_source_amazon_category():
    p = register.build_source("amazon_category", {})
    assert p.key == "amazon_category"


# --- marketplace_search (lines 267-286) --------------------------------------- #

def test_build_source_marketplace_search(monkeypatch):
    import ujin.poll.marketplace as mkt_mod
    sentinel = object()
    monkeypatch.setattr(mkt_mod, "MarketplaceSearchPollable", MagicMock(return_value=sentinel))
    p = register.build_source("marketplace_search", {})
    assert p is sentinel
