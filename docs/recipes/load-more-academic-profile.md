# Recipe: harvest every publication behind a "Load more" button

**Scenario.** A faculty profile (e.g. Sidney Fels's) lists publications 20 at a
time; a "Load more" button at the bottom reveals the next 20, and so on. A single
HTTP fetch — or even a static render — sees only the first page. You want *all* of
them.

**Approach.** A `browser` source runs a `load_more` recipe that clicks the button
until it runs out, then the existing link extractor normalizes the fully-loaded
page. Because the result is large, a `chunk` transform hands it onward in bites.

## The job

```jsonc
POST :8902/jobs
{
  "name": "fels-publications",
  "source": {
    "kind": "browser",
    "config": {
      "url": "https://www.ece.ubc.ca/~ssfels/",
      "engine": "playwright",
      "actions": [
        { "action": "wait_for_selector", "selector": ".publications" },
        { "action": "load_more",
          "button":  "button.load-more",     // the "Load 20 more" button
          "results": ".publication-item",    // its count must keep growing
          "max_clicks": 200,                  // safety cap
          "timeout_ms": 120000 }              // overall cap
      ],
      "extract": "links"                      // normalize the final HTML
    }
  },
  "transforms": [
    { "kind": "chunk", "config": { "size": 25 } }   // 25 publications per event
  ],
  "sinks": [
    { "kind": "jsonl",   "config": { "path": "/data/fels.jsonl" } },
    { "kind": "forward", "config": { "url": "http://llm/ingest" } }   // 1 POST per chunk
  ],
  "schedule": { "mode": "once" }              // or adaptive to re-harvest periodically
}
```

Run it now and inspect:

```bash
curl -X POST :8902/jobs/<id>/run
curl :8902/jobs/<id>/runs      # the run is recorded
```

## How exhaustion is decided

`load_more` stops at the first of: the button **disappears/disables**, the
`.publication-item` **count stops growing**, or the **`max_clicks`/`timeout_ms`**
caps. The reason is in the action log. If the site uses infinite scroll instead of
a button, swap `load_more` for `scroll_to_bottom` with the same `results` selector.

## If items aren't anchors

When publications aren't plain `<a>` links the headline extractor recognizes, set
`"extract": "raw"` and add `"results_selector": ".publication-item"` — the fetcher
harvests each item's text + href directly into a list.

## Tuning

- `settle_ms` (default 800): how long to wait for new items after each click. Raise
  it on slow endpoints.
- Prefer `engine: "selenium"` only if the site misbehaves under Playwright.
