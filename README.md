# scraperv2

Shared web-scraping library extracted from the jennie scraper-v2 service so
multiple projects (awork, moeka, jennie) can depend on one implementation.

## Modules

- `scraperv2.fetch.http` — `HttpFetcher`: async HTTP with per-host rate limiting
  and conditional GET (ETag / Last-Modified).
- `scraperv2.fetch.obscura` — `ObscuraFetcher`: render JS / anti-bot pages via the
  obscura engine (binary `OBSCURA_BIN` or service `OBSCURA_URL`); degrades when absent.
- `scraperv2.extract.article` — `extract_article`: clean body text via trafilatura.
- `scraperv2.extract.links` — boilerplate-stripped link extraction + `normalize_url`.
- `scraperv2.cache.store` / `scraperv2.cache.disk` — LRU+TTL and SQLite caches so
  repeated runs are cheap and idempotent.
- `scraperv2.sources.rss` / `sitemap` / `discover` — feed/sitemap parsing and
  auto-discovery from a homepage.

## Install

```bash
pip install -e .          # or, as a path source under uv
```

## Lineage

The `fetch`/`extract.article`/`cache` modules are the dependency-decoupled
versions hardened in awork; `extract.links` and `sources/*` come from jennie's
scraper-v2. News-specific pieces (trends, social, per-host profiles) were left in
jennie and can be merged here later. Consumed as a git submodule (local path for
now; repoint to a remote once pushed).
