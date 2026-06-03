# Recipe: watch a JS-rendered region for change

**Scenario.** A dashboard or listing renders its content with JavaScript, and you
only want to be notified — and only emit — when something *new* appears.

## The job

```jsonc
POST :8902/jobs
{
  "name": "watch-listing",
  "source": {
    "kind": "browser",
    "config": {
      "url": "https://example.com/live",
      "actions": [ { "action": "wait_for_selector", "selector": ".feed-item" } ],
      "extract": "raw",
      "results_selector": ".feed-item"        // harvest items as a list
    }
  },
  "transforms": [
    { "kind": "dedupe", "config": { "key": "href" } }   // only items not seen before
  ],
  "sinks": [ { "kind": "ws" }, { "kind": "webhook", "config": { "url": "https://hooks/me" } } ],
  "schedule": { "mode": "adaptive", "base": 120, "min": 30, "max": 3600 }
}
```

## How it works

- The `browser` source renders the page and harvests `.feed-item`s into a list.
- The source **fingerprints** that list — the adaptive schedule polls faster when it
  changes, slower when it doesn't (grows toward `max`, shrinks toward `min`).
- `dedupe` (keyed on `href`) drops items seen on earlier polls, so each sink emit
  carries only the genuinely new entries; if nothing is new, the event is dropped
  and the sinks stay quiet.
- `ws` streams to any client on `WS /jobs/events`; the webhook POSTs the new items.

## Variations

- Swap `dedupe` for a `select` filter to also constrain by field
  (`{"where": {"status": "open"}}`).
- For a *button*-paginated page, add a `load_more` action before extraction to watch
  the whole list, not just the first page.
