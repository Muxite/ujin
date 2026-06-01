"""Social + trends route handlers (optional surface).

Mounted by :func:`ujin.scrape.app.create_scrape_app` alongside the core router.
Endpoints degrade gracefully when the relevant pieces aren't configured:
``/social/twitter`` returns 503 without a Brave key, ``/social/x`` skips the
nitter leg without a pool, and ``/trends/corroborated`` returns an empty list
when no corroboration store is wired.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..sources.social import (
    BraveError,
    BraveNotConfigured,
    fetch_x_trends,
    mastodon_timeline,
    truth_social_posts,
    twitter_search,
    x_posts,
)
from .models import (
    CorroboratedCluster,
    CorroboratedResponse,
    MastodonRequest,
    SocialPostModel,
    SocialResponse,
    TruthRequest,
    TwitterRequest,
    XRequest,
    XResponse,
    XTrendItem,
    XTrendsRequest,
    XTrendsResponse,
)

router = APIRouter()


@router.post("/social/twitter", response_model=SocialResponse)
async def social_twitter(req: TwitterRequest, request: Request) -> SocialResponse:
    if not req.username:
        raise HTTPException(status_code=400, detail="username required")
    api_key = getattr(request.app.state.config, "brave_api_key", None)
    try:
        posts = await twitter_search(req.username, req.count, api_key=api_key)
    except BraveNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except BraveError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return SocialResponse(
        posts=[SocialPostModel(url=p.url, text=p.text) for p in posts]
    )


@router.post("/social/mastodon", response_model=SocialResponse)
async def social_mastodon(req: MastodonRequest, request: Request) -> SocialResponse:
    if not req.account:
        raise HTTPException(status_code=400, detail="account required")
    user_agent = getattr(request.app.state.config, "user_agent", None)
    try:
        posts = await mastodon_timeline(req.account, req.count, user_agent=user_agent)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return SocialResponse(
        posts=[SocialPostModel(url=p.url, text=p.text) for p in posts]
    )


@router.post("/social/x", response_model=XResponse)
async def social_x(req: XRequest, request: Request) -> XResponse:
    if not req.username:
        raise HTTPException(status_code=400, detail="username required")
    pool = getattr(request.app.state, "nitter_pool", None)
    result = await x_posts(
        req.username,
        req.count,
        nitter=pool,
        allow_brave=req.allow_brave,
    )
    return XResponse(
        leg=result.leg,
        posts=[SocialPostModel(url=p.url, text=p.text) for p in result.posts],
    )


@router.post("/social/truth", response_model=SocialResponse)
async def social_truth(req: TruthRequest) -> SocialResponse:
    if not req.username:
        raise HTTPException(status_code=400, detail="username required")
    try:
        posts = await truth_social_posts(req.username, req.count)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return SocialResponse(
        posts=[SocialPostModel(url=p.url, text=p.text) for p in posts]
    )


@router.post("/trends/x", response_model=XTrendsResponse)
async def trends_x(req: XTrendsRequest) -> XTrendsResponse:
    result = await fetch_x_trends(req.region, req.count)
    return XTrendsResponse(
        region=result.region,
        items=[
            XTrendItem(rank=i.rank, tag=i.tag, url=i.url, volume=i.volume)
            for i in result.items
        ],
        source=result.source,
    )


@router.get("/trends/corroborated", response_model=CorroboratedResponse)
async def trends_corroborated(request: Request) -> CorroboratedResponse:
    """Active cross-source corroborated headline clusters.

    Returns an empty list when no corroboration store is wired (the generic
    app); jennie enables it via UJIN_BREAKING_SCORER.
    """
    config = request.app.state.config
    store = getattr(request.app.state, "corroboration", None)
    window = int(getattr(config, "corroboration_window_secs", 1800))
    if store is None:
        return CorroboratedResponse(window_secs=window, clusters=[])

    min_h = config.corroboration_min_hosts
    max_h = max(min_h, config.corroboration_max_hosts_for_full_score)
    clusters = sorted(store.clusters(), key=lambda c: -len(c.hosts))
    items: list[CorroboratedCluster] = []
    for c in clusters:
        hosts = sorted(c.hosts)
        n = len(hosts)
        if n >= max_h:
            floor = 1.0
        elif n >= min_h:
            floor = (n - min_h) / max(1, max_h - min_h) * 0.5 + 0.5
        else:
            floor = 0.0
        items.append(CorroboratedCluster(
            representative=c.representative,
            hosts=hosts,
            member_count=len(c.members),
            first_seen_ts=c.first_seen_ts,
            last_seen_ts=c.last_seen_ts,
            velocity_per_min=c.velocity_per_min(),
            breaking_score_floor=floor,
        ))
    return CorroboratedResponse(window_secs=window, clusters=items)
