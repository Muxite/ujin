# Recipe: a custom recipe step (`@register.action`)

**Scenario.** The built-in primitives (`click`, `scroll_to_bottom`, `load_more`, …)
don't cover something your target needs — a bespoke pagination scheme, a shadow-DOM
traversal, a canvas interaction. Add your own **action** as a plugin.

> Plugins run in-process with no sandbox — only mount code you trust. See
> [../PLUGINS.md](../PLUGINS.md).

## Write the action

Drop a file into the plugin directory (`UJIN_PLUGINS_DIR`, default `/plugins`):

```python
# /plugins/dismiss_cookie_banner.py
from ujin import register

@register.action("dismiss_banner")
def make(cfg, ctx):
    """A custom step. The factory gets (config, BuildContext);
    BuildContext carries the live `browser` and `page`."""
    selector = cfg.get("selector", "#cookie-accept")

    async def handler(page, **params):
        # `page` is the live page handle (the _Page surface).
        if await page.exists(selector):
            await page.click(selector)
        return {"dismissed": selector}

    return handler
```

The factory returns an **async `handler(page, **params)`**. It runs against the same
`page` the primitives use, so you can call `page.click`, `page.query_count`,
`page.eval_js`, etc. Whatever dict you return is merged into the action log.

## Load it and use it

```bash
curl -X POST :8902/plugins/reload    # -> {"loaded":["dismiss_cookie_banner"], "failed":[]}
```

Reference it in any recipe by its `plugin:` kind:

```jsonc
"actions": [
  { "action": "plugin:dismiss_banner", "selector": "#cookie-accept" },
  { "action": "load_more", "button": ".more", "results": ".item" }
]
```

## The page surface

Your handler's `page` exposes (all `async`): `goto`, `content`, `final_url`,
`click`, `fill`, `press`, `query_count`, `exists`, `is_enabled`,
`wait_for_selector`, `scroll_into_view`, `scroll_to_bottom`, `eval_js`,
`screenshot`, `harvest`. The same surface backs both the Playwright and Selenium
engines, so a custom action works under either.
