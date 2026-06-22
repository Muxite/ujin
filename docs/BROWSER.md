# Browser automation

Some pages don't yield their content to a plain HTTP GET or a static render —
they load it with JavaScript, behind infinite scroll, or behind a "**Load more**"
button you must click repeatedly. ujin's browser layer drives a real browser
through a **declarative interaction recipe**, then hands the fully-loaded HTML to
the same extractors used everywhere else.

> Browser support ships in the heavy **`ujin-browser`** image (Playwright +
> Chromium + Selenium + chromedriver). The default `ujin` image stays slim and
> degrades gracefully — a browser job there fails fast with a clear message.

```bash
docker compose --profile browser up ujin-jobs-browser    # :8902, browsers baked in
# or locally:
pip install 'ujin[browser]' && python -m playwright install chromium
UJIN_BROWSER_ENABLED=1 ujin jobs-serve
```

## Two ways to use it

**1. A `browser` job source** (recommended for recurring harvests):

```jsonc
POST /jobs
{
  "name": "harvest",
  "source": { "kind": "browser", "config": {
    "url": "https://example.com/list",
    "engine": "playwright",          // or "selenium"
    "actions": [ ...recipe... ],
    "extract": "links",              // links | article | structured | raw
    "results_selector": ".item",     // optional: harvest items directly (raw)
    "headless": true                 // default true; set false for headed debugging
  } },
  "transforms": [ { "kind": "chunk", "config": { "size": 25 } } ],
  "sinks": [ ... ]
}
```

**2. The scrape service**, pinning `render="browser"`:

```bash
curl -X POST localhost:8911/scrape -H 'content-type: application/json' -d '{
  "url": "https://example.com/list",
  "render": "browser",
  "actions": [{"action":"load_more","button":".more","results":".item","max_clicks":200}],
  "page_size": 25
}'
```

## Engines

| engine | notes |
|--------|-------|
| `playwright` (default) | native async, auto-waiting, manages Chromium itself. |
| `selenium` | blocking WebDriver marshalled onto a dedicated thread; uses `chromedriver`. Pick it for sites/tooling that need it. |

Both run the **same recipe**. Configure via `UJIN_BROWSER_ENGINE`,
`UJIN_BROWSER_HEADLESS`, `UJIN_BROWSER_TIMEOUT_SECS`.

## The recipe — action reference

A recipe is a list of `{ "action": <name>, ...params }` steps, run in order. A step
that errors is recorded in `actions_log` and the recipe continues (you still get
the partial HTML).

| action | params | effect |
|--------|--------|--------|
| `goto` | `url` | navigate (the source `url` is visited first automatically) |
| `wait_for_selector` | `selector`, `timeout_ms?` | wait until an element appears |
| `wait_ms` | `ms` | fixed pause |
| `click` | `selector` | click once |
| `fill` | `selector`, `value` | type into a field |
| `press` | `selector`, `key` | press a key (e.g. `Enter`) |
| `scroll` / `scroll_to_bottom` | `results?`, `max_scrolls?`, `timeout_ms?`, `settle_ms?` | infinite-scroll until the item count stops growing |
| **`load_more`** | `button`, `results`, `max_clicks?`, `timeout_ms?`, `settle_ms?` | **click `button` until exhausted** (see below) |
| `eval_js` | `script` | run JS, capture the return value |
| `screenshot` | `name?` | capture a PNG (returned in `screenshots`) |
| `plugin:<name>` | (custom) | a custom step — see [recipes/custom-actions-plugin.md](recipes/custom-actions-plugin.md) |

### `load_more` — click until it runs out

The headline primitive. Each round it clicks `button` and waits for the count of
`results` elements to grow. It **stops** when:

- the button is **gone** or **disabled** (`button_gone` / `disabled`), or
- the item count **stops increasing** (`stable`), or
- it hits **`max_clicks`** (default 200) or **`timeout_ms`** (default 60000).

The chosen stop reason is reported in the action log. This is exactly what you
need to harvest *all* of a paginated list — see
[recipes/load-more-academic-profile.md](recipes/load-more-academic-profile.md).

## Output is large — chunk it

A full harvest can be too big for an LLM to ingest accurately. Pair the browser
source with the **`chunk`** transform (one event per N items) or the scrape
**`page_size`/`cursor`** pagination. See
[recipes/feed-an-llm-with-chunking.md](recipes/feed-an-llm-with-chunking.md).

## Cookbook

See [recipes/](recipes/README.md) for end-to-end walkthroughs.
