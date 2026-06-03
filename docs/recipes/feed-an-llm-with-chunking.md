# Recipe: feed an LLM with chunking

**Scenario.** You harvested hundreds of items. Sending them to an LLM all at once
overflows the context window and *degrades extraction accuracy*. You want the LLM
to see them in digestible pieces.

ujin offers two complementary mechanisms.

## 1. The `chunk` transform (job pipelines)

The pipeline lets a transform fan one event into many. `chunk` splits a list (or a
long string) payload into pieces and emits **one event per chunk** — so each chunk
hits your sink (e.g. a webhook to the LLM) as a separate call.

```jsonc
"transforms": [
  { "kind": "select", "config": { "fields": ["title", "url"] } },  // trim first
  { "kind": "chunk",  "config": { "size": 25 } }                    // 25 items/event
],
"sinks": [
  { "kind": "forward", "config": { "url": "http://llm/ingest" } }   // one POST per chunk
]
```

Each emitted event carries `chunk_index` and `chunk_total`, so the LLM side can
track progress and reassemble.

**Budget by tokens instead of count** when items vary in size:

```jsonc
{ "kind": "chunk", "config": { "token_budget": 2000 } }   // ~2000 tokens/chunk
```

`token_budget` packs items until the approximate token budget (~4 chars/token) is
hit. For a long article body, chunk on a string field the same way:
`{ "kind": "chunk", "config": { "path": "payload.text", "token_budget": 1500 } }`.

## 2. Paginated `/scrape` (one-shot callers)

If you're calling the scrape service directly and want to pull pages yourself, use
`page_size` + `cursor`:

```bash
# first page
curl -X POST :8901/scrape -d '{"url":"https://site/list","page_size":25}'
#  -> { "links":[...25], "total":137, "next_cursor":"MjU6ZmluZ2Vy..." }

# next page: pass the cursor back
curl -X POST :8901/scrape -d '{"url":"https://site/list","page_size":25,"cursor":"MjU6..."}'
```

The cursor is pinned to the result fingerprint: if the underlying list changes
between pulls you get **HTTP 409**, so restart without a cursor. Omit `page_size`
for the full list (default behavior, unchanged).

## Which to use

- **Job pipeline + `chunk`** — recurring harvests, push model, fire-and-forget to a
  sink. Combine with a `browser` source for "load everything, then chunk".
- **`/scrape` pagination** — interactive pull model, you drive the loop.
