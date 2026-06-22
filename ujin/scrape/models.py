"""Pydantic request/response schemas for the v2 API."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


# ── /scrape ───────────────────────────────────────────────────────────────────

class ScrapeRequest(BaseModel):
    url: str = Field(
        "",
        description=(
            "Absolute URL to scrape. Must include scheme (http/https). "
            "Required for a single-URL request; omit it (and instead set "
            "`urls`) to scrape several URLs in one request."
        ),
        examples=["https://apnews.com", "https://www.reuters.com/world/"],
    )
    urls: Optional[list[str]] = Field(
        None,
        description=(
            "Opt-in multi-URL batch: scrape every listed URL in one request and "
            "receive one result per URL. When set (non-empty), the URLs are "
            "fetched concurrently under a bounded concurrency cap "
            "(`batch_max_concurrency`, default 8) and the per-URL "
            "`ScrapeResponse`s come back in request order under the new `batch` "
            "list; the top-level fields mirror the FIRST URL's result. A failure "
            "on one URL is isolated as a `kind='error'` entry and never fails the "
            "others. Every URL is scraped with the request's `mode`, "
            "`force_refresh`, `render`, `actions`, and `enrich_html_top_n`; the "
            "batch form is single-`mode` (the `modes` multi-extract map and "
            "`page_size`/`cursor` pagination are not applied per URL). Bounded by "
            "the service `batch_max_items` setting (default 64). Omit (the "
            "default) for the classic single-`url` behaviour, which is "
            "byte-for-byte unchanged."
        ),
        examples=[["https://apnews.com", "https://www.reuters.com/world/"]],
    )
    mode: Literal["links", "article", "auto", "combined", "structured"] = Field(
        "links",
        description=(
            "What to extract. `links` returns the headline link-set "
            "(homepage/section pages). `article` returns cleaned body text "
            "for a single article URL. `auto` picks based on page shape. "
            "`combined` fetches RSS + HTML in parallel and merges "
            "the link sets by canonical URL; RSS contributes title + summary "
            "+ published, HTML contributes any breaking links not yet in "
            "the feed. `structured` returns JSON-LD / OpenGraph / microdata "
            "from the page in the `structured` field."
        ),
        examples=["links", "article", "auto", "combined", "structured"],
    )
    modes: Optional[list[Literal["links", "article", "auto", "structured", "html"]]] = Field(
        None,
        description=(
            "Opt-in multi-extract: request several extract modes over a single "
            "fetch and receive a result per mode. When set (non-empty), the page "
            "is fetched once and each mode is extracted from the same body; the "
            "per-mode `ScrapeResponse`s come back under the `extracts` mapping "
            "(keyed by mode), and the top-level fields mirror the first listed "
            "mode. A failure in one mode is isolated as a `kind='error'` entry "
            "and never fails the others. Omit (the default) for the classic "
            "single-`mode` behaviour, which is byte-for-byte unchanged. The "
            "`combined` strategy is single-`mode` only and not accepted here; "
            "`html` returns the raw fetched HTML in `html`."
        ),
        examples=[["links", "structured"], ["article", "html"]],
    )
    force_refresh: bool = Field(
        False,
        description=(
            "Bypass the cache and any ETag/Last-Modified revalidation. "
            "Use sparingly — disables host-cooldown short-circuiting too."
        ),
        examples=[False, True],
    )
    enrich_html_top_n: int = Field(
        0,
        ge=0,
        le=20,
        description=(
            "When > 0 (and `mode == 'combined'`), fan out `mode=article` "
            "fetches for the top-N HTML-only links (those without an RSS "
            "summary) and attach the first paragraph as the link's summary. "
            "Adds latency in exchange for richer LLM input downstream."
        ),
        examples=[0, 5],
    )
    render: Literal["auto", "http", "obscura", "browser"] = Field(
        "auto",
        description=(
            "Pin the fetch strategy. `auto` keeps the per-host/default "
            "escalation (HTTP → obscura). `browser` runs the `actions` recipe "
            "in a real browser (Playwright/Selenium) and snapshots the result — "
            "use it for JS-driven pages and `load_more` pagination."
        ),
        examples=["auto", "browser"],
    )
    actions: Optional[list[dict]] = Field(
        None,
        description=(
            "Browser interaction recipe (used when `render='browser'`). A list "
            "of steps like `{'action':'load_more','button':'.more',"
            "'results':'.item','max_clicks':200}` or click/scroll/fill/wait_for_selector."
        ),
        examples=[[{"action": "load_more", "button": "button.load-more",
                    "results": ".publication-item", "max_clicks": 200}]],
    )
    page_size: Optional[int] = Field(
        None,
        ge=1,
        description=(
            "When set, return only this many links/items and a `next_cursor` "
            "for the next page — so an LLM ingests a large result set in bites. "
            "Omit for the full list (default)."
        ),
        examples=[25, 50],
    )
    cursor: Optional[str] = Field(
        None,
        description=(
            "Opaque pagination cursor from a prior response's `next_cursor`. "
            "Pinned to the result fingerprint; a stale cursor (the underlying "
            "list changed) yields HTTP 409."
        ),
    )


class LinkItem(BaseModel):
    url: str = Field(
        ...,
        description="Absolute, normalized URL of a discovered link.",
        examples=["https://apnews.com/article/election-2024-abc123"],
    )
    text: str = Field(
        ...,
        description="Visible anchor text (headline). May be empty for image-only links.",
        examples=["Senate passes spending bill in late-night vote"],
    )
    # The fields below are populated by `mode=combined` (and by the
    # article fan-out enrichment) — empty otherwise. They are additive,
    # so /scrape mode=links responses remain backwards-compatible.
    summary: str = Field(
        "",
        description=(
            "RSS-side summary or, when the article body fan-out is "
            "enabled, the first paragraph of the fetched article. Empty "
            "for plain HTML-only headlines without enrichment."
        ),
        examples=[
            "The Senate on Thursday passed a sweeping spending bill that funds the federal government through next September.",
        ],
    )
    published: str = Field(
        "",
        description=(
            "Publication timestamp from RSS (ISO-8601 when parseable). "
            "Empty for HTML-only links."
        ),
        examples=["2024-11-21T03:14:00+00:00"],
    )
    seen_in: list[str] = Field(
        default_factory=list,
        description=(
            "Which sub-strategies surfaced this link in combined mode. "
            "Subset of {'rss','html','article'}. Empty for legacy modes."
        ),
        examples=[["rss"], ["html"], ["rss", "html"], ["html", "article"]],
    )
    tier: str = Field(
        "mainstream",
        description=(
            "Coarse source class. The default `NullScorer` stamps every link "
            "`generic`; a wired `BreakingScorer` classifies from per_host.yaml "
            "`tier:` into `wire` (AP/Reuters/AFP), `mainstream` (BBC/NYT/CNN, "
            "the per-host default), `specialty` (trade press), `social` "
            "(X/Mastodon/Truth), or `trend` (corroborated cluster)."
        ),
        examples=["generic", "wire", "mainstream", "specialty", "social", "trend"],
    )
    breaking_score: float = Field(
        0.0,
        description=(
            "Scraper-side composite priority score in [0, 1]. Blends "
            "source rank, lede markers, lastmod recency, cross-source "
            "corroboration, and X-trend overlap. >=0.6 is breaking, "
            "0.3-0.6 developing, <0.3 background. Downstream is expected "
            "to multiply with the freshness label."
        ),
        examples=[0.0, 0.35, 0.7, 0.95],
    )
    score_components: dict[str, float] = Field(
        default_factory=dict,
        description=(
            "Per-component contributions for debug — keys: "
            "source_rank, lede_marker, recency, corroboration, "
            "trend_overlap. Sum equals breaking_score."
        ),
        examples=[{"source_rank": 0.25, "lede_marker": 0.15, "recency": 0.18, "corroboration": 0.30, "trend_overlap": 0.0}],
    )


class ArticlePayload(BaseModel):
    url: str = Field(
        ...,
        description="Canonical article URL (post-redirect, if any).",
        examples=["https://www.reuters.com/world/us/example-article-2024-11-05/"],
    )
    title: Optional[str] = Field(
        None,
        description="Extracted headline. None if the extractor could not find one.",
        examples=["Senate passes spending bill in late-night vote"],
    )
    text: str = Field(
        ...,
        description="Cleaned body text — boilerplate (menus, ads, share bars) removed.",
        examples=[
            "WASHINGTON (AP) — The Senate on Thursday passed a sweeping "
            "spending bill that funds the federal government through ..."
        ],
    )
    byline: Optional[str] = Field(
        None,
        description="Author byline if detected.",
        examples=["By Mary Clare Jalonick"],
    )
    published: Optional[str] = Field(
        None,
        description="Publication date in ISO-8601 form when extractable.",
        examples=["2024-11-21T03:14:00+00:00"],
    )
    language: Optional[str] = Field(
        None,
        description="BCP-47 language tag detected for the body text.",
        examples=["en", "es", "fr"],
    )
    top_image: Optional[str] = Field(
        None,
        description="Lead image URL (og:image or first in-article figure).",
        examples=["https://dims.apnews.com/dims4/default/abc123/2147483647/strip/true/example.jpg"],
    )


# ── /social/x and /trends/* (Phase A + B) ─────────────────────────────────────


class XRequest(BaseModel):
    username: str = Field(
        ...,
        description="X/Twitter handle without leading @. Walks nitter → syndication → brave chain.",
        examples=["realDonaldTrump"],
    )
    count: int = Field(20, description="Max posts to return.", examples=[20])
    allow_brave: bool = Field(
        True,
        description="When False, the chain stops after the two free legs and never burns Brave credit.",
    )


class XResponse(BaseModel):
    leg: str = Field(
        ...,
        description="Which chain leg produced the result — `nitter`, `syndication`, `brave`, or `empty`.",
        examples=["nitter", "syndication", "brave", "empty"],
    )
    posts: list["SocialPostModel"] = Field(default_factory=list)


class XTrendsRequest(BaseModel):
    region: str = Field(
        "united-states",
        description="Region slug, e.g. united-states, united-kingdom, worldwide.",
        examples=["united-states"],
    )
    count: int = Field(20, description="Max trends to return.")


class XTrendItem(BaseModel):
    rank: int
    tag: str
    url: Optional[str] = None
    volume: Optional[str] = None


class XTrendsResponse(BaseModel):
    region: str
    items: list[XTrendItem] = Field(default_factory=list)
    source: str = Field(
        ...,
        description="`trends24` | `getdaytrends` | `empty`",
    )


class CorroboratedCluster(BaseModel):
    representative: str = Field(..., description="Best headline for the cluster.")
    hosts: list[str] = Field(default_factory=list)
    member_count: int
    first_seen_ts: float
    last_seen_ts: float
    velocity_per_min: float
    breaking_score_floor: float = Field(
        ...,
        description="Minimum breaking_score this cluster contributes to its members.",
    )


class CorroboratedResponse(BaseModel):
    window_secs: int
    clusters: list[CorroboratedCluster] = Field(default_factory=list)


class ScrapeResponse(BaseModel):
    url: str = Field(
        ...,
        description="The request URL, echoed back for client correlation.",
        examples=["https://apnews.com"],
    )
    kind: str = Field(
        ...,
        description=(
            "What this response contains. One of: `links` (headline link-set "
            "in `links`), `article` (parsed body in `article`), `structured` "
            "(JSON-LD/OpenGraph/microdata in `structured`), `html` (raw fetched "
            "HTML in `html`, multi-extract only), `empty` "
            "(fetch succeeded but extractor found nothing usable), `error` "
            "(batch-only, or a per-mode failure inside `extracts` — wrapping a "
            "single failure)."
        ),
        examples=["links", "article", "structured", "html", "empty", "error"],
    )
    fingerprint: str = Field(
        ...,
        description=(
            "Stable SHA-256 over the normalized payload (link-set for "
            "`links` mode, body text for `article`). Compare across "
            "successive calls to detect real content change. Empty string "
            "on `empty` or `error`."
        ),
        examples=["3f9a1c8b2e7d4a5601c9f0e8b7d6a5c4e3f2a1b0c9d8e7f6a5b4c3d2e1f0a9b8"],
    )
    fetched_at: float = Field(
        ...,
        description="Wall-clock unix timestamp when this response was produced.",
        examples=[1732147200.5],
    )
    cached: bool = Field(
        ...,
        description=(
            "True when the body was served from the local cache "
            "(fingerprint unchanged, or 304-revalidated, or host on cooldown)."
        ),
        examples=[False, True],
    )
    age_secs: float = Field(
        ...,
        description="Seconds since the cached entry was originally fetched. 0.0 on fresh fetches.",
        examples=[0.0, 42.7],
    )
    used_renderer: bool = Field(
        ...,
        description="True when the obscura headless renderer was invoked (JS-heavy fallback).",
        examples=[False, True],
    )
    strategy_used: str = Field(
        "http",
        description=(
            "Which fetch path produced the payload. One of: `http` "
            "(plain HTTP fast path), `http_304` (revalidated, served from "
            "cache), `obscura` (headless render), `browser` (pinned "
            "`render='browser'` recipe run), `sitemap_news` (altpath "
            "via news sitemap), `rss` (altpath via RSS), `combined` "
            "(parallel RSS+HTML merge, `mode='combined'`), `cache` (host on "
            "cooldown, cache served), `error` (batch-only wrap)."
        ),
        examples=["http", "obscura", "browser", "sitemap_news", "rss",
                  "combined", "cache", "http_304"],
    )
    links: list[LinkItem] = Field(
        default_factory=list,
        description="Headline link-set when `kind == 'links'`. Empty list otherwise.",
        examples=[[{"url": "https://apnews.com/article/election-2024-abc123", "text": "Senate passes spending bill"}]],
    )
    article: Optional[ArticlePayload] = Field(
        None,
        description="Parsed article payload when `kind == 'article'`. None otherwise.",
        examples=[None],
    )
    structured: Optional[dict] = Field(
        None,
        description=(
            "Structured data (`jsonld`, `opengraph`, `microdata`) when "
            "`kind == 'structured'`. None otherwise."
        ),
        examples=[None],
    )
    html: Optional[str] = Field(
        None,
        description=(
            "Raw fetched HTML when `kind == 'html'` (multi-extract `html` mode). "
            "None for every other mode."
        ),
        examples=[None],
    )
    final_url: Optional[str] = Field(
        None,
        description="Post-redirect URL when different from the request URL.",
        examples=["https://www.apnews.com/"],
    )
    note: Optional[str] = Field(
        None,
        description=(
            "Diagnostic message — e.g. `'304 Not Modified'`, "
            "`'content unchanged'`, `'host cooldown 45s; served cache'`, "
            "or in batch error responses the exception type and message."
        ),
        examples=["304 Not Modified", "content unchanged", "host cooldown 45s; served cache"],
    )
    next_poll_hint_secs: Optional[float] = Field(
        None,
        description=(
            "Scraper-suggested wait before the caller's next poll for this "
            "URL. Derived from observed churn × source tier. Watchers may "
            "honour this hint to amortise budget across consumers. None "
            "when no hint is available."
        ),
        examples=[30.0, 90.0, 300.0],
    )
    max_breaking_score: float = Field(
        0.0,
        description=(
            "Maximum `breaking_score` across the link-set in this response. "
            "Lets a caller short-circuit downstream LLM work when nothing in "
            "the response crosses the breaking threshold."
        ),
        examples=[0.0, 0.45, 0.85],
    )
    total: Optional[int] = Field(
        None,
        description=(
            "Total number of links/items available (before pagination). "
            "Set only when `page_size` was requested."
        ),
        examples=[None, 137],
    )
    next_cursor: Optional[str] = Field(
        None,
        description=(
            "Cursor for the next page; pass it back as `cursor`. None when this "
            "is the last page or pagination was not requested."
        ),
    )
    extracts: Optional[dict[str, "ScrapeResponse"]] = Field(
        None,
        description=(
            "Multi-extract result map (only when the request set `modes`). Keyed "
            "by mode name, each value is a full `ScrapeResponse` for that mode "
            "(its own `extracts` is always null — no nesting). A mode that failed "
            "appears here with `kind='error'`. None for classic single-`mode` "
            "requests."
        ),
    )
    batch: Optional[list["ScrapeResponse"]] = Field(
        None,
        description=(
            "Multi-URL batch results (only when the request set `urls`). One full "
            "`ScrapeResponse` per requested URL, in request order; each entry's "
            "own `batch` is always null (no nesting). A URL that failed appears "
            "here with `kind='error'` and its exception in `note`. The top-level "
            "fields mirror the first URL's result. None for classic single-`url` "
            "requests."
        ),
    )


# Resolve the self-reference in `extracts` (forward-ref under deferred annotations).
ScrapeResponse.model_rebuild()


class BatchScrapeRequest(BaseModel):
    requests: list[ScrapeRequest] = Field(
        ...,
        description=(
            "List of scrape requests to fan out concurrently. Bounded by "
            "the service `batch_max_items` setting (default 64). Each item "
            "honours only `url`, `mode`, and `force_refresh` — per-item "
            "`render`/`actions`/`page_size`/`cursor`/`enrich_html_top_n` are "
            "ignored in batch mode. Per-item failures are returned in-line "
            "with `kind='error'` rather than failing the whole batch."
        ),
        examples=[[
            {"url": "https://apnews.com", "mode": "links"},
            {"url": "https://www.reuters.com/world/", "mode": "links"},
        ]],
    )


class BatchScrapeResponse(BaseModel):
    results: list[ScrapeResponse] = Field(
        ...,
        description=(
            "One response per input request, in the same order. Failed "
            "items appear with `kind='error'` and `strategy_used='error'`; "
            "the original exception is in `note`."
        ),
        examples=[[]],
    )


# ── /feed ─────────────────────────────────────────────────────────────────────

class FeedRequest(BaseModel):
    url: str = Field(
        ...,
        description="Absolute URL of an RSS or Atom feed.",
        examples=[
            "https://feeds.bbci.co.uk/news/rss.xml",
            "https://thehill.com/feed/",
        ],
    )


class FeedItemModel(BaseModel):
    url: str = Field(
        ...,
        description="Item link (article URL).",
        examples=["https://www.bbc.com/news/world-us-canada-67890123"],
    )
    title: str = Field(
        ...,
        description="Item title as published in the feed.",
        examples=["US Senate passes spending bill in late-night vote"],
    )
    summary: str = Field(
        ...,
        description="Item description/summary, HTML stripped.",
        examples=["The bill funds the federal government through next September."],
    )
    published: Optional[str] = Field(
        None,
        description="Publication date as published in the feed (ISO-8601 when parseable).",
        examples=["2024-11-21T03:14:00+00:00"],
    )


class FeedResponse(BaseModel):
    items: list[FeedItemModel] = Field(
        ...,
        description="Parsed feed entries, most-recent first as ordered by the feed source.",
        examples=[[]],
    )


# ── /sitemap ──────────────────────────────────────────────────────────────────

class SitemapRequest(BaseModel):
    url: str = Field(
        ...,
        description=(
            "Absolute URL of a sitemap XML document. Supports both "
            "<urlset> and <sitemapindex> roots and the news-sitemap "
            "extension."
        ),
        examples=[
            "https://www.nytimes.com/sitemaps/new/news.xml.gz",
            "https://www.wsj.com/news-sitemap.xml",
        ],
    )


class SitemapEntryModel(BaseModel):
    url: str = Field(
        ...,
        description="`<loc>` URL from the sitemap entry.",
        examples=["https://www.nytimes.com/2024/11/21/us/politics/senate-spending.html"],
    )
    lastmod: Optional[str] = Field(
        None,
        description="`<lastmod>` timestamp from the sitemap entry.",
        examples=["2024-11-21T03:14:00+00:00"],
    )
    title: Optional[str] = Field(
        None,
        description="News-sitemap `<news:title>` when present.",
        examples=["Senate passes spending bill in late-night vote"],
    )


class SitemapResponse(BaseModel):
    entries: list[SitemapEntryModel] = Field(
        ...,
        description="Parsed sitemap entries in document order.",
        examples=[[]],
    )


# ── /discover ─────────────────────────────────────────────────────────────────

class DiscoverRequest(BaseModel):
    homepage: str = Field(
        ...,
        description=(
            "Homepage URL to probe for RSS/Atom and sitemap candidates. "
            "Walks `<link rel='alternate'>` tags, `robots.txt`, and the "
            "usual conventional paths (`/feed`, `/rss`, `/sitemap.xml`)."
        ),
        examples=["https://apnews.com", "https://www.reuters.com"],
    )


class DiscoverResponse(BaseModel):
    homepage: str = Field(
        ...,
        description="The request homepage echoed back.",
        examples=["https://apnews.com"],
    )
    rss: list[str] = Field(
        ...,
        description="Candidate RSS/Atom feed URLs discovered.",
        examples=[["https://apnews.com/index.rss", "https://apnews.com/world-news.rss"]],
    )
    sitemap: list[str] = Field(
        ...,
        description="Candidate sitemap URLs discovered.",
        examples=[["https://apnews.com/sitemap.xml", "https://apnews.com/news-sitemap.xml"]],
    )


# ── /social/* ─────────────────────────────────────────────────────────────────

class TwitterRequest(BaseModel):
    username: str = Field(
        ...,
        description=(
            "X/Twitter handle without the leading `@`. The handler "
            "strips a leading `@` if supplied. Requires the service to "
            "be started with `SEARCH_API_KEY` configured (Brave Search)."
        ),
        examples=["realDonaldTrump", "elonmusk", "nytimes"],
    )
    count: int = Field(
        10,
        description="Max posts to return. Clamped to [1, 20] by the upstream Brave API.",
        examples=[10, 20],
    )


class MastodonRequest(BaseModel):
    account: str = Field(
        ...,
        description=(
            "Federated Mastodon address — `@user@instance.tld` or "
            "`user@instance.tld`. The leading `@` is optional. Posts "
            "are fetched from the instance's public statuses API; no "
            "auth required."
        ),
        examples=["@Gargron@mastodon.social", "Mastodon@mastodon.social"],
    )
    count: int = Field(
        20,
        description="Max statuses to return. Clamped to [1, 40].",
        examples=[20, 40],
    )


class TruthRequest(BaseModel):
    username: str = Field(
        ...,
        description=(
            "Truth Social handle without the leading `@`. Posts are "
            "pulled from the user's public RSS feed."
        ),
        examples=["realDonaldTrump", "DonaldJTrumpJr"],
    )
    count: int = Field(
        20,
        description="Max posts to return (best-effort; RSS feed length is upstream-controlled).",
        examples=[20, 40],
    )


class SocialPostModel(BaseModel):
    url: str = Field(
        ...,
        description="Permalink to the original post on the source platform.",
        examples=[
            "https://truthsocial.com/@realDonaldTrump/posts/123456789012345678",
            "https://mastodon.social/@Gargron/111111111111111111",
        ],
    )
    text: str = Field(
        ...,
        description=(
            "Post text — HTML stripped for Mastodon, title+description "
            "for Twitter (Brave results), RSS description for Truth."
        ),
        examples=["Great meeting today with our amazing supporters!"],
    )


class SocialResponse(BaseModel):
    posts: list[SocialPostModel] = Field(
        ...,
        description="List of recent posts, newest first (when the source orders that way).",
        examples=[[]],
    )


# ── /health ───────────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str = Field(
        ...,
        description="Liveness marker. Always `'ok'` when the process is serving.",
        examples=["ok"],
    )
    ok: bool = Field(
        True,
        description="Boolean liveness, normalized across all ujin services.",
    )
    service: str = Field(
        "ujin-scrape",
        description="Which ujin service answered (normalized across services).",
    )
    obscura_available: bool = Field(
        ...,
        description=(
            "True when the obscura headless-render dependency is "
            "importable. When false, JS-heavy fallbacks degrade to HTTP "
            "+ altpath only."
        ),
        examples=[True, False],
    )
    cache: dict[str, int] = Field(
        ...,
        description=(
            "In-memory cache counters — typically keys like `entries`, "
            "`hits`, `misses`, `evictions`."
        ),
        examples=[{"entries": 42, "hits": 318, "misses": 17, "evictions": 0}],
    )


class MetricsResponse(BaseModel):
    total_fetches: int = Field(
        ...,
        description="Sum of fetches across all hosts since process start.",
        examples=[1842],
    )
    hosts: dict[str, dict] = Field(
        ...,
        description=(
            "Per-host bucket keyed by netloc (lowercased). Each value "
            "contains `fetches`, `successes`, `failures`, "
            "`renderer_used`, `cached_returns`, `fallback_used` "
            "(strategy→count map), `latency_ms_p50`, `latency_ms_p95`, "
            "`samples`, `last_seen`."
        ),
        examples=[{
            "apnews.com": {
                "fetches": 42,
                "successes": 41,
                "failures": 1,
                "renderer_used": 0,
                "cached_returns": 12,
                "fallback_used": {"http": 30, "cache": 12},
                "latency_ms_p50": 187.4,
                "latency_ms_p95": 612.8,
                "samples": 42,
                "last_seen": 1732147200.5,
            }
        }],
    )
