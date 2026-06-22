# Recipe: harvest Amazon product prices

Use the built-in **`amazon_search`** source to turn search terms into normalized
products (`title`, `image_url`, `price_cents`, `currency`, `source_id`/ASIN,
`url`) and push them to an HTTP ingest endpoint.

`amazon_search` is render-agnostic and escalates `http → obscura → browser`
(`engine: auto`), so it works on the slim image and uses Playwright/obscura only
when a plain fetch comes back without products. Amazon blocks datacenter IPs
aggressively — set `proxy:` (or the `PROXY_URL` env) for reliable runs.

```yaml
# /workflows/amazon-electronics.yaml  ->  workflow id "amazon-electronics"
source:
  kind: amazon_search
  config:
    terms:                       # one poll per term, combined into one batch
      - "Logitech MX Master 3S"
      - "Sony WH-1000XM5"
    max_results: 1               # top organic result per term
    category: Electronics        # stamped onto every product
    engine: auto                 # auto | http | obscura | playwright | selenium
    # proxy: "http://user:pass@host:port"   # or set PROXY_URL in the env
transforms:
  - kind: select
    config:
      path: payload
      fields: [source, source_id, title, image_url, price_cents, currency, category, url]
  - kind: dedupe
    config: { key: source_id }
sinks:
  - kind: webhook
    config:
      url: "${BACKEND_URL}/api/ingest"          # ${ENV} is expanded at load
      method: POST
      headers: { X-Ingest-Secret: "${INGEST_SECRET}" }
schedule:
  mode: once                     # run on load; switch to adaptive to keep prices fresh
```

Run it, then pull what was obtained:

```bash
docker compose --profile browser up ujin-jobs-browser   # browser-capable image
curl -X POST localhost:8902/jobs/amazon-electronics/run
curl localhost:8902/jobs/amazon-electronics/content
```

The reusable extractor behind the source —
[`ujin.extract.product.extract_products`](../../ujin/extract/product.py) — also
works standalone on any schema.org `Product` page (JSON-LD/OpenGraph) with a
`selectolax` card fallback; pass `selectors=` to target another marketplace.
```
