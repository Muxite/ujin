"""FastAPI route handlers for the scrape service — thin wrappers around
ScrapeService / sources.

This module holds the generic, dependency-light surface: /health, /scrape,
/scrape:batch, /metrics, /feed, /sitemap, /discover. Social and trends routes
live in :mod:`ujin.scrape.routes_social` and are mounted only when wired.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..fetch.obscura import obscura_available
from ..sources import discover_sources, fetch_sitemap, parse_feed
from .config import ScrapeConfig
from .models import (
    ArticlePayload,
    BatchScrapeRequest,
    BatchScrapeResponse,
    DiscoverRequest,
    DiscoverResponse,
    FeedItemModel,
    FeedRequest,
    FeedResponse,
    HealthResponse,
    LinkItem,
    MetricsResponse,
    ScrapeRequest,
    ScrapeResponse,
    SitemapEntryModel,
    SitemapRequest,
    SitemapResponse,
)
from .service import HostCooldown


router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    """Liveness probe with cache and renderer availability."""
    cache = request.app.state.cache.stats()
    return HealthResponse(
        status="ok",
        obscura_available=obscura_available(),
        cache=cache,
    )


def _result_to_response(result) -> ScrapeResponse:
    """Convert a ScrapeService.scrape() result into the wire response."""
    article = None
    if result.article is not None:
        article = ArticlePayload(
            url=result.article.url,
            title=result.article.title,
            text=result.article.text,
            byline=result.article.byline,
            published=result.article.published,
            language=result.article.language,
            top_image=result.article.top_image,
        )
    return ScrapeResponse(
        url=result.url,
        kind=result.kind,
        fingerprint=result.fingerprint,
        fetched_at=result.fetched_at,
        cached=result.cached,
        age_secs=result.age_secs,
        used_renderer=result.used_renderer,
        strategy_used=result.strategy_used,
        links=[
            LinkItem(
                url=l.url,
                text=l.text,
                summary=l.summary,
                published=l.published,
                seen_in=list(l.seen_in),
                tier=getattr(l, "_tier", "mainstream"),
                breaking_score=getattr(l, "_breaking_score", 0.0),
                score_components=getattr(l, "_score_components", {}),
            )
            for l in result.links
        ],
        article=article,
        structured=getattr(result, "structured", None),
        final_url=result.final_url,
        note=result.note,
        next_poll_hint_secs=getattr(result, "next_poll_hint_secs", None),
        max_breaking_score=max(
            (getattr(l, "_breaking_score", 0.0) for l in result.links),
            default=0.0,
        ),
    )


def _encode_cursor(offset: int, fingerprint: str) -> str:
    import base64

    return base64.urlsafe_b64encode(f"{offset}:{fingerprint}".encode()).decode()


def _decode_cursor(cursor: str) -> tuple[int, str]:
    import base64

    raw = base64.urlsafe_b64decode(cursor.encode()).decode()
    offset_s, fp = raw.split(":", 1)
    return int(offset_s), fp


def _paginate(resp: ScrapeResponse, page_size: int, cursor: str | None) -> ScrapeResponse:
    """Slice the link-set to one page; pin the cursor to the fingerprint.

    A stale cursor (the underlying list changed between pulls) raises 409 so the
    caller restarts from the first page.
    """
    all_links = resp.links
    resp.total = len(all_links)
    offset = 0
    if cursor:
        try:
            offset, fp = _decode_cursor(cursor)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail="invalid cursor") from exc
        if fp != resp.fingerprint:
            raise HTTPException(
                status_code=409,
                detail="cursor stale (result changed); restart without a cursor",
            )
    resp.links = all_links[offset:offset + page_size]
    nxt = offset + page_size
    resp.next_cursor = _encode_cursor(nxt, resp.fingerprint) if nxt < len(all_links) else None
    return resp


@router.post("/scrape", response_model=ScrapeResponse)
async def scrape(req: ScrapeRequest, request: Request) -> ScrapeResponse:
    """Render and extract a single page (headlines or article body)."""
    if not req.url:
        raise HTTPException(status_code=400, detail="url required")
    service = request.app.state.service
    try:
        result = await service.scrape(
            req.url,
            mode=req.mode,
            force_refresh=req.force_refresh,
            enrich_html_top_n=req.enrich_html_top_n,
            render=req.render,
            actions=req.actions,
        )
    except HostCooldown as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    resp = _result_to_response(result)
    if req.page_size is not None:
        resp = _paginate(resp, req.page_size, req.cursor)
    return resp


def _error_response(url: str, exc: Exception) -> ScrapeResponse:
    return ScrapeResponse(
        url=url,
        kind="error",
        fingerprint="",
        fetched_at=0.0,
        cached=False,
        age_secs=0.0,
        used_renderer=False,
        strategy_used="error",
        note=f"{type(exc).__name__}: {exc}",
    )


@router.post("/scrape:batch", response_model=BatchScrapeResponse)
async def scrape_batch(
    req: BatchScrapeRequest, request: Request
) -> BatchScrapeResponse:
    """Fan out N scrape calls concurrently. Per-item failures come back inline."""
    if not req.requests:
        return BatchScrapeResponse(results=[])
    config: ScrapeConfig = request.app.state.config
    if len(req.requests) > config.batch_max_items:
        raise HTTPException(
            status_code=400,
            detail=f"batch size {len(req.requests)} exceeds max {config.batch_max_items}",
        )
    service = request.app.state.service
    items = [(r.url, r.mode, r.force_refresh) for r in req.requests]
    raw = await service.scrape_batch(items)
    out: list[ScrapeResponse] = []
    for r, raw_item in zip(req.requests, raw):
        if isinstance(raw_item, Exception):
            out.append(_error_response(r.url, raw_item))
        else:
            out.append(_result_to_response(raw_item))
    return BatchScrapeResponse(results=out)


@router.get("/metrics", response_model=MetricsResponse)
async def metrics(request: Request) -> MetricsResponse:
    """Per-host fetch metrics snapshot."""
    snap = request.app.state.metrics.snapshot()
    return MetricsResponse(
        total_fetches=snap["total_fetches"], hosts=snap["hosts"]
    )


@router.post("/feed", response_model=FeedResponse)
async def feed(req: FeedRequest) -> FeedResponse:
    """Parse an RSS or Atom feed (no rendering, no cache)."""
    if not req.url:
        raise HTTPException(status_code=400, detail="url required")
    try:
        items = await parse_feed(req.url)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return FeedResponse(
        items=[
            FeedItemModel(
                url=i.url, title=i.title, summary=i.summary, published=i.published
            )
            for i in items
        ]
    )


@router.post("/sitemap", response_model=SitemapResponse)
async def sitemap(req: SitemapRequest, request: Request) -> SitemapResponse:
    """Fetch and parse a sitemap XML document."""
    if not req.url:
        raise HTTPException(status_code=400, detail="url required")
    http = request.app.state.http
    try:
        entries = await fetch_sitemap(http, req.url)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return SitemapResponse(
        entries=[
            SitemapEntryModel(url=e.url, lastmod=e.lastmod, title=e.title)
            for e in entries
        ]
    )


@router.post("/discover", response_model=DiscoverResponse)
async def discover(req: DiscoverRequest, request: Request) -> DiscoverResponse:
    """Probe a homepage for RSS and sitemap URLs."""
    if not req.homepage:
        raise HTTPException(status_code=400, detail="homepage required")
    http = request.app.state.http
    try:
        found = await discover_sources(http, req.homepage)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return DiscoverResponse(
        homepage=found.homepage, rss=found.rss, sitemap=found.sitemap
    )
