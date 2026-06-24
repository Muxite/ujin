"""Plugin registry + loader: builtins, plugin load, error isolation, reload,
and unknown-kind handling (including the app's 400)."""
from __future__ import annotations

import types

import pytest

from ujin.plugins import load_plugins
from ujin.registry import BuildContext, Registry, register


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


# ---------------------------------------------------------------------------
# Decorator paths for transform and scorer (lines 64, 70)
# ---------------------------------------------------------------------------

def test_transform_and_scorer_decorators():
    @register.transform("my_xfm")
    def make_xfm(cfg):
        return {"xfm": cfg}

    @register.scorer("my_scr")
    def make_scr(cfg):
        return {"scr": cfg}

    assert register.has("transform", "my_xfm")
    assert register.has("scorer", "my_scr")


# ---------------------------------------------------------------------------
# build_transform / build_sink / build_scorer (lines 123, 126, 129)
# ---------------------------------------------------------------------------

def test_build_transform_builtin():
    """build_transform hits line 123; _mk_transform closure runs (line 299)."""
    t = register.build_transform("select", {"fields": ["a", "b"]})
    assert t is not None


def test_build_sink_builtin():
    """build_sink hits line 126; StdoutSink needs no hub/store."""
    s = register.build_sink("stdout", {}, BuildContext())
    assert s is not None


def test_build_scorer_plugin():
    """build_scorer hits line 129 via a plugin-registered scorer."""
    @register.scorer("rank")
    def make(cfg):
        return {"rank": cfg.get("weight", 1.0)}

    result = register.build_scorer("rank", {"weight": 2.0})
    assert result == {"rank": 2.0}


# ---------------------------------------------------------------------------
# inspect.signature TypeError/ValueError fallback (lines 113-114)
# ---------------------------------------------------------------------------

def test_inspect_signature_fallback_typeerror(monkeypatch):
    """TypeError from inspect.signature → nparams defaults to 1 (line 113-114)."""
    import inspect as _inspect
    import ujin.registry as _reg_mod

    fresh = Registry()
    sentinel = object()

    def _factory(cfg):
        return sentinel

    fresh.register_builtin("source", "_te_kind", _factory)

    real_sig = _inspect.signature

    def _broken_sig(fn, *a, **kw):
        if fn is _factory:
            raise TypeError("uninspectable")
        return real_sig(fn, *a, **kw)

    monkeypatch.setattr(_reg_mod, "inspect", types.SimpleNamespace(signature=_broken_sig))
    assert fresh.build_source("_te_kind", {}) is sentinel


# ---------------------------------------------------------------------------
# Built-in source factories (all bodies are lazy imports, safe offline)
# ---------------------------------------------------------------------------

def test_builtin_http_source():
    p = register.build_source("http", {"url": "https://example.com"})
    assert p.url == "https://example.com"


def test_builtin_rss_source():
    p = register.build_source("rss", {"url": "https://example.com/rss"})
    assert p.url == "https://example.com/rss"


def test_builtin_site_source():
    p = register.build_source("site", {"url": "https://example.com"})
    assert p.url == "https://example.com"


def test_builtin_scrape_source_raises_without_service():
    """scrape source raises ValueError when ctx.scrape_service is None (lines 180-181)."""
    with pytest.raises(ValueError, match="scrape source needs"):
        register.build_source("scrape", {"url": "https://x"}, BuildContext())


def test_builtin_scrape_source_with_service():
    """scrape source builds correctly when a service is provided (lines 182-184)."""
    class _FakeSvc:
        pass

    p = register.build_source(
        "scrape", {"url": "https://x"}, BuildContext(scrape_service=_FakeSvc())
    )
    assert p is not None


def test_builtin_browser_source():
    p = register.build_source("browser", {"url": "https://x"}, BuildContext())
    assert p is not None


def test_builtin_graphql_source():
    p = register.build_source("graphql", {"url": "https://x", "query": "{ hello }"})
    assert p.url == "https://x"


def test_builtin_amazon_search_single_term():
    from ujin.poll.amazon import AmazonSearchPollable

    p = register.build_source("amazon_search", {"term": "laptop", "max_results": 1})
    assert isinstance(p, AmazonSearchPollable)


def test_builtin_amazon_search_multi_term():
    """Multi-term cfg wraps results in a MultiPollable (lines 227-232)."""
    from ujin.poll.multi import MultiPollable

    p = register.build_source(
        "amazon_search", {"terms": ["laptop", "phone"], "max_results": 1}
    )
    assert isinstance(p, MultiPollable)


def test_builtin_amazon_category_source():
    from ujin.poll.amazon import AmazonCategoryPollable

    p = register.build_source("amazon_category", {})
    assert isinstance(p, AmazonCategoryPollable)


def test_builtin_marketplace_search_source():
    """marketplace_search with inline profiles requires no filesystem/network."""
    p = register.build_source(
        "marketplace_search",
        {
            "profile": "myshop",
            "profiles": {"myshop": {"type": "amazon", "domain": "amazon.com"}},
        },
    )
    assert p is not None
