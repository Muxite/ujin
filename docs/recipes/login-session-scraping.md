# Recipe: log in, then scrape gated pages

**Scenario.** The content you want is behind a login form. You need to fill
credentials, submit, and then read the authenticated page.

> Treat credentials as secrets — pass them via env/secret store, not in a committed
> job spec. The job below shows the shape; substitute your own secret handling.

## The recipe

```jsonc
POST :8902/jobs
{
  "name": "members-area",
  "source": {
    "kind": "browser",
    "config": {
      "url": "https://example.com/login",
      "actions": [
        { "action": "wait_for_selector", "selector": "#username" },
        { "action": "fill",  "selector": "#username", "value": "me@example.com" },
        { "action": "fill",  "selector": "#password", "value": "..." },
        { "action": "click", "selector": "button[type=submit]" },
        { "action": "wait_for_selector", "selector": ".dashboard" },
        { "action": "goto",  "url": "https://example.com/members/reports" },
        { "action": "wait_for_selector", "selector": ".report-row" }
      ],
      "extract": "raw",
      "results_selector": ".report-row"
    }
  },
  "sinks": [ { "kind": "jsonl", "config": { "path": "/data/reports.jsonl" } } ],
  "schedule": { "mode": "cron", "cron": "0 7 * * *" }
}
```

## Notes

- Each browser run uses a **fresh context** (clean cookies). The login steps run at
  the start of every poll — fine for daily/cron harvests. (Persisting an
  authenticated session across runs is a planned enhancement.)
- After login, `goto` navigates to the gated page within the same context, so the
  auth cookie carries over.
- For multi-step or OAuth logins, add `press`/`wait_ms` steps, or a custom
  `@register.action` (see [custom-actions-plugin.md](custom-actions-plugin.md)) for
  anything the primitives don't cover.
- Use `eval_js` to read a value the page sets in JS (e.g. a CSRF token) if a step
  needs it.
