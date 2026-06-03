# Recipe: paginated link-set harvest

**Scenario.** A page (or a `browser` harvest) yields a large link-set and you want
to consume it page-by-page from your own code, without ujin pushing to a sink.

## Pull pages with `page_size` + `cursor`

```python
import httpx

def pages(url, size=25):
    cursor = None
    with httpx.Client(base_url="http://localhost:8901") as c:
        while True:
            body = {"url": url, "page_size": size}
            if cursor:
                body["cursor"] = cursor
            r = c.post("/scrape", json=body)
            if r.status_code == 409:        # list changed mid-walk
                cursor = None               # restart from the top
                continue
            data = r.json()
            yield data["links"], data["total"]
            cursor = data["next_cursor"]
            if cursor is None:
                break

for links, total in pages("https://site/list"):
    print(f"got {len(links)} of {total}")
    # ... hand this page to the LLM ...
```

## Notes

- **Opt-in:** without `page_size` the response is exactly as before (full `links`,
  `total`/`next_cursor` are `null`). Existing callers are unaffected.
- **Cursor = `base64("{offset}:{fingerprint}")`** — server-derived, no server-side
  state. Because it's pinned to the result fingerprint, a changed list is detected
  (HTTP 409) rather than silently skipping/duplicating.
- **Caching:** repeated pulls within the cache window are stable (same fingerprint).
  Set `force_refresh` only on the first page if you need a guaranteed-fresh walk.
- For a JS-driven source, combine with `render: "browser"` + `actions` so the full
  list is loaded before it's paginated.
