# Marketplace search (profile-driven)

ujin ships a **generic marketplace engine** — `marketplace_search` (and the underlying
`AmazonSearchPollable` + `ujin/extract/product.py`) — that can sweep any product site.
It ships **no site-specific profiles**: the URL templates, CSS selectors, and keyterm
banks that describe a given site (Amazon, eBay, Walmart, Newegg, …) are **data you
supply**, so the specific scraping config can live in and be owned by the consuming
application (e.g. wordle-max) rather than being baked into ujin.

## Profile schema

```yaml
<name>:
  domain: ebay.com
  search_url: "https://www.{domain}/sch/?_nkw={query}"  # {domain}, {query} are filled in
  engine: browser            # auto | http | browser  (optional, default auto)
  wait_selector: ".s-card"   # optional; element to await on JS sites
  selectors:                 # optional CSS overrides; omit for JSON-LD/OpenGraph defaults
    card: ".s-card, .s-item"
    id_attr: "data-id"       # card attribute holding the product id (else derived from link)
    title: [".s-card__title", "h3"]
    image: [".s-card__image img", "img"]
    price: [".s-card__price"]
    link: ["a.su-link", "a[href*='/itm/']"]
  desc_selectors: [ ... ]    # optional; used when with_description is on
  keyterms:                  # category -> sample terms (sampled per poll)
    Electronics: ["wireless earbuds", "graphics card"]
```

A ready-to-use reference set (amazon / newegg / ebay / walmart) ships at
[`examples/marketplace_profiles.yaml`](../examples/marketplace_profiles.yaml). Copy it
into your app and evolve it there.

## Supplying profiles

Two mechanisms, usable together (inline overrides file entries of the same name):

### 1. Mounted file (volume) — `UJIN_MARKETPLACE_PROFILES`

```yaml
# docker-compose.yml (in the consuming app)
services:
  ujin:
    volumes:
      - ./config/marketplace_profiles.yaml:/config/marketplace_profiles.yaml:ro
    environment:
      UJIN_MARKETPLACE_PROFILES: /config/marketplace_profiles.yaml
```

### 2. In the workflow / source config (held by the calling program)

```yaml
source:
  kind: marketplace_search
  profile: ebay
  # either point at a file...
  profiles_path: /config/marketplace_profiles.yaml
  # ...or pass the mapping inline:
  profiles:
    ebay:
      domain: ebay.com
      search_url: "https://www.{domain}/sch/?_nkw={query}"
      engine: browser
      keyterms: { Electronics: ["graphics card"] }
  terms_per_poll: 3
  max_results: 8
```

If the named `profile` isn't found in the resolved set, construction raises a clear
`ValueError` listing what's available — ujin no longer falls back to a built-in profile.

## Full config reference (`marketplace_search` job source)

When used as a job source kind in the jobs control plane all constructor parameters
are available as config keys:

| key | default | description |
|-----|---------|-------------|
| `profile` | `"amazon"` | name of the profile to use; **must exist** in the resolved profile set or construction raises `ValueError` listing what's available |
| `profiles` | — | inline profile mapping (overrides same-named file entries) |
| `profiles_path` | — | path to a YAML/JSON profile file |
| `categories` | profile `keyterms` | override the keyterm bank; `{category: [term, ...]}` |
| `terms_per_poll` | `3` | how many terms to sample per sweep |
| `max_results` | `8` | max product cards to keep per term |
| `engine` | profile `engine` or `auto` | fetch backend: `auto` \| `http` \| `browser` |
| `proxy` | — | proxy URL passed to the fetch backend |
| `timeout_secs` | `40` | per-term request timeout |
| `headless` | `true` | run browser backend headlessly |
| `with_description` | `false` | scrape each product's detail page for a description (slow; triggers detail-page fetch) |
| `detail_cache` | `false` | skip re-fetching detail pages for `source_id`s seen within the TTL |
| `detail_cache_path` | `/data/{profile}_seen.json` | path to the persistent detail-cache JSON (on the durable `/data` volume) |
| `detail_cache_ttl_secs` | `604800` (7 days) | how long a `source_id` is considered fresh |

`$UJIN_MARKETPLACE_PROFILES` is always consulted as a fallback when neither `profiles`
nor `profiles_path` is provided.

## Migration note (breaking)

Earlier builds shipped built-in `amazon`/`newegg`/`ebay`/`walmart` profiles inside
`ujin.poll.marketplace.SITE_PROFILES`. Those are removed. To keep existing behaviour,
mount or pass `examples/marketplace_profiles.yaml` (it contains the same four profiles).
