"""Extract headline-candidate links from rendered HTML.

The v1 scraper dumped every `<a href>` on the page, which is why callers
see hundreds of noise links: nav, footer, ads, "share on twitter",
section indices. v2 restricts to plausibly-content regions and drops
obvious boilerplate by link text.

This is a heuristic — newsroom homepages don't have a clean schema for
"this is a story link" vs "this is a section link." But filtering on
(a) container and (b) text helps a lot.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode

from selectolax.parser import HTMLParser, Node


# Containers that almost always wrap actual stories on newsroom pages.
_MAIN_SELECTORS = (
    "main",
    "article",
    "[role=main]",
    "[data-component=story]",
    "section[data-testid*=story i]",
    "section[class*=feed i]",
    "section[class*=stream i]",
    "div[class*=headline i]",
    "div[class*=top-stories i]",
)

# Tags whose subtrees we never read links from — navigation chrome.
_EXCLUDE_TAGS = frozenset({"nav", "header", "footer", "aside"})

# Attribute-value substrings that mark a region as chrome regardless of tag.
# Stored lowercase; we lowercase the attribute value before comparing.
_EXCLUDE_CLASS_PATTERNS = (
    "nav",
    "footer",
    "menu",
    "sidebar",
    "related",
    "newsletter",
    "subscribe",
)
_EXCLUDE_ROLES = frozenset({"navigation", "banner", "contentinfo"})

# Common dead-text patterns. Lowercased compare.
_BOILERPLATE_TEXTS = frozenset(
    {
        "subscribe",
        "sign in",
        "sign up",
        "log in",
        "login",
        "register",
        "menu",
        "more",
        "share",
        "read more",
        "continue reading",
        "see all",
        "see more",
        "view all",
        "all stories",
        "all news",
        "next",
        "previous",
        "back to top",
        "skip to content",
        "skip to main content",
        "advertisement",
        "sponsored",
        "newsletter",
        "podcast",
        "video",
        "videos",
        "live",
        "watch",
        "listen",
        "play",
        "settings",
        "search",
        "home",
    }
)

# UTM-style query params we strip during canonicalization.
# Match by prefix where most tracking params share one (utm_*, mc_*, ns_*, etc.),
# and by exact-name for the standalone tokens (gclid, fbclid, igshid, ...).
_TRACKING_PARAM_PREFIXES = (
    "utm_", "mc_", "ns_", "wt_", "ic_", "cm_", "ito_", "smid_", "cmpid_",
    "_ga", "_gl", "fb_", "ref_", "s_",
    # Cycle 5.1: BBC RSS appends `?at_campaign=rss&at_medium=RSS` to every
    # feed item. The `at_*` family is BBC-specific but harmless to strip
    # globally — it's never load-bearing.
    "at_",
)
_TRACKING_PARAM_NAMES = frozenset({
    "gclid", "fbclid", "msclkid", "igshid", "yclid", "dclid", "twclid",
    "ref", "ref_url", "ref_src", "ref_source", "source", "src",
    "share", "shared", "via", "campaign_id", "campaign", "ito",
    "trk", "trkCampaign", "ocid", "mc_eid", "mc_cid",
    # Cycle 5.1:
    #   - `traffic_source=rss` — Al Jazeera RSS tracking
    #   - `update=NNNN` — AJ live-blog deep links (anchors a specific
    #     update entry on the same liveblog page; stripping it collapses
    #     N "different" URLs back to one canonical liveblog).
    "traffic_source", "update",
})


def _is_tracking_param(key: str) -> bool:
    k = key.lower()
    if k in _TRACKING_PARAM_NAMES:
        return True
    return any(k.startswith(p) for p in _TRACKING_PARAM_PREFIXES)


# ── Slop filtering ────────────────────────────────────────────────────────────
# Non-news verticals that publishers wedge onto homepages: games, recipes,
# product reviews, horoscopes, etc. They aren't headlines and they aren't
# what downstream NLP / market-linking is looking for, but the link
# extractor would happily include them since they live in <main>.

# Path-segment patterns. Match if any path segment equals one of these,
# or if the path starts with /<segment>/.
_SLOP_PATH_SEGMENTS = frozenset(
    {
        # Puzzles / games
        "games", "game", "puzzles", "puzzle", "crosswords", "crossword",
        "wordle", "connections", "spelling-bee", "spellingbee", "mini",
        "midi", "strands", "tiles", "vertex", "letterboxed", "sudoku",
        # Recipes / food / lifestyle commerce
        "cooking", "recipes", "recipe", "food52", "wirecutter",
        "shopping", "deals", "gifts", "gift-guide", "buying-guide",
        # Style / fluff
        "style", "fashion", "horoscopes", "horoscope", "astrology",
        # Service pages
        "tips", "contact", "help", "support", "about-us",
        "privacy", "terms", "cookies", "ethics", "corrections",
        # Account / commerce
        "account", "login", "signin", "subscribe", "subscription",
        "newsletters", "newsletter", "store", "shop",
        # Classifieds
        "jobs", "careers", "realestate", "real-estate", "autos",
    }
)

# Host-prefix patterns (subdomains used for slop verticals).
_SLOP_HOST_PREFIXES = (
    "cooking.",
    "recipes.",
    "wirecutter.",
    "shop.",
    "store.",
    "jobs.",
    "careers.",
    "deals.",
    "horoscopes.",
)

# Anchor-text substring patterns. Lowercased compare.
_SLOP_TEXT_SUBSTRINGS = (
    "play wordle",
    "play connections",
    "play the crossword",
    "play the mini",
    "spelling bee",
    "the crossword",
    "the mini crossword",
    "the midi crossword",
    "today's wordle",
    "todays wordle",
    "wirecutter",
    "buy now",
    "shop now",
    "view deal",
    "horoscope",
    "your horoscope",
)


def _is_slop_url(url: str) -> bool:
    """True for non-news verticals (games, recipes, shopping, horoscopes).

    Matched by host prefix OR path-segment intersection with the slop set.
    Slop links can live inside <main> on news homepages because publishers
    cross-link them for engagement; this filter is the last line of defense
    after the chrome-region exclusion has run."""
    parts = urlsplit(url)
    host = parts.netloc.lower()
    if any(host.startswith(p) for p in _SLOP_HOST_PREFIXES):
        return True
    segments = [s for s in parts.path.lower().split("/") if s]
    return any(seg in _SLOP_PATH_SEGMENTS for seg in segments)


def _is_slop_text(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    return any(s in t for s in _SLOP_TEXT_SUBSTRINGS)


@dataclass(frozen=True)
class NormalizedLink:
    url: str
    text: str
    # Optional fields populated by the combined RSS+HTML strategy. Default
    # empties so existing code that constructs `NormalizedLink(url=..., text=...)`
    # stays valid. `seen_in` records which sources produced this link
    # ("rss", "html") so callers can prioritise / dedup intelligently.
    summary: str = ""
    published: str = ""
    seen_in: tuple[str, ...] = ()


def normalize_url(url: str, base: Optional[str] = None) -> Optional[str]:
    """Make link absolute, drop fragment + tracking params, normalize host case.

    Returns None for unusable URLs (mailto:, javascript:, empty)."""
    if not url:
        return None
    url = url.strip()
    if url.startswith(("javascript:", "mailto:", "tel:", "#")):
        return None
    if base:
        url = urljoin(base, url)
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return None
    if not parts.netloc:
        return None

    # Strip tracking params, then sort the remaining (k, v) pairs so the
    # same article shared with different param orderings canonicalizes to
    # the same string.
    kept = sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if not _is_tracking_param(k)
    )
    clean_query = urlencode(kept)
    # Drop trailing slash on path? No — some sites differ. Leave as-is.
    return urlunsplit(
        (parts.scheme, parts.netloc.lower(), parts.path, clean_query, "")
    )


def _text_of(node: Node) -> str:
    return " ".join((node.text(deep=True, separator=" ") or "").split())


def _is_excluded_element(node: Node) -> bool:
    """Check tag + attributes against the chrome-detection rules.

    We don't use object identity (selectolax wrapper objects don't have
    a stable Python id), so we re-check each ancestor on the fly."""
    tag = (node.tag or "").lower()
    if tag in _EXCLUDE_TAGS:
        return True
    attrs = node.attributes or {}
    role = (attrs.get("role") or "").lower()
    if role in _EXCLUDE_ROLES:
        return True
    aria_label = (attrs.get("aria-label") or "").lower()
    if aria_label and "nav" in aria_label:
        return True
    cls = (attrs.get("class") or "").lower()
    if cls and any(p in cls for p in _EXCLUDE_CLASS_PATTERNS):
        return True
    return False


def _has_excluded_ancestor(node: Node) -> bool:
    cur = node.parent
    while cur is not None:
        if _is_excluded_element(cur):
            return True
        cur = cur.parent
    return False


_PAYWALL_OR_SPONSORED_FRAGMENTS = (
    "sponsored", "advertisement", "promoted", "subscribe now",
    "sign up to read", "this story is for subscribers",
    "subscribe to continue", "subscribe to read", "members only",
    "paid post", "from our advertiser",
)

_NAV_PREFIXES = (
    "watch:", "listen:", "read more:", "coming up:", "explainer:",
    "in pictures:", "in video:", "in photos:", "live:", "live updates:",
    "newsletter:", "more on this story:",
)

_NAV_SUFFIX_CHARS = ("→", "›", "»", "►")

# Cycle 5.1: photo / wire credits captured as headlines. CNN obit articles
# linked via the lead image surface anchor text like
# "Al Pereira/Michael Ochs Archives/Getty Images" — valid URL, junk title.
# The list below must be specific enough not to false-positive on real
# headlines that quote one of these names ("Reuters reports new sanctions"
# is a real headline; "via REUTERS" is a credit line).
_PHOTO_CREDIT_PATTERNS = (
    re.compile(r"/\s*Getty Images\b", re.IGNORECASE),
    re.compile(r"\bAP Photo\b", re.IGNORECASE),
    re.compile(r"\bvia REUTERS\b", re.IGNORECASE),
    re.compile(r"\bAFP\s*/", re.IGNORECASE),
    re.compile(r"\bPhotographer:\s*", re.IGNORECASE),
    re.compile(r"^Photo:\s+", re.IGNORECASE),
    re.compile(r"/Bloomberg\b", re.IGNORECASE),
    re.compile(r"/Michael Ochs Archives\b", re.IGNORECASE),
)


def _is_photo_credit(text: str) -> bool:
    if not text:
        return False
    return any(p.search(text) for p in _PHOTO_CREDIT_PATTERNS)


def _strip_numeric_prefix(text: str) -> str:
    """Strip a leading "<N> " prefix from titles. BBC's "Most read" widget
    decorates anchors with their list position ("3 What are the Enhanced
    Games"); other publishers do similar. Returns the cleaned text — the
    article URL stays valid."""
    return re.sub(r"^\d{1,3}\s+", "", text).strip()


# Cycle 5.1: title floor — minimum length in characters before a title is
# considered a plausible news headline. Previously enforced by the irene
# ingest stage; consolidated here so the scraper is the single source of
# truth for what counts as a headline. Some short legitimate headlines
# ("Trump signs bill") get filtered — acceptable per the irene policy
# this replaces.
_MIN_HEADLINE_CHARS = 30


def _is_boilerplate_text(text: str) -> bool:
    t = text.lower().strip()
    if not t:
        return True
    if len(t) < _MIN_HEADLINE_CHARS:
        return True
    if t in _BOILERPLATE_TEXTS:
        return True
    # Word-count guards. < 4 words is almost always nav-cruft; > 25 words
    # is almost always a paragraph slipped through the link extractor.
    words = t.split()
    if len(words) < 4:
        return True
    if len(words) > 25:
        return True
    # All-caps section labels: "LATEST", "VIDEO", "BREAKING".
    if text == text.upper() and len(text) <= 20 and not any(ch.isdigit() for ch in text):
        return True
    # Nav prefixes like "Watch:", "Listen:", "Read more:".
    if any(t.startswith(p) for p in _NAV_PREFIXES):
        return True
    # Titles that are *only* a "see more" arrow.
    if text.strip() in _NAV_SUFFIX_CHARS or text.strip().rstrip(" ›→»►").strip() == "":
        return True
    # Sponsored / paywall teaser keywords.
    if any(frag in t for frag in _PAYWALL_OR_SPONSORED_FRAGMENTS):
        return True
    # Photo / wire credits captured as link text.
    if _is_photo_credit(text):
        return True
    # Pure date / time strings, e.g. "2h ago", "May 22, 2026"
    if re.fullmatch(r"\d+\s*(h|hr|hour|hours|m|min|mins|d|day|days|w|week|weeks)\s*ago", t):
        return True
    if re.fullmatch(r"\d{1,2}[:/-]\d{1,2}[:/-]?\d{0,4}", t):
        return True
    # Repeated-phrase junk: anchors that wrap a section header + the same
    # phrase as the click target ("Texas Senate Texas Senate Runoff ›").
    # If half the tokens repeat back-to-back this is almost certainly a
    # section-link card, not a headline.
    if len(words) >= 4:
        tail = " ".join(words[len(words)//2:])
        head = " ".join(words[:len(words)//2])
        if tail.startswith(head):
            return True
    # Trailing nav chevron with no real headline ahead of it.
    if text.rstrip().endswith(("›", "→", "»", "►")) and len(words) < 8:
        return True
    return False


def extract_headline_links(
    html: str, base_url: str, *, max_links: int = 200
) -> list[NormalizedLink]:
    """Extract candidate headline links.

    Strategy:
      1. Build a set of "excluded" nodes (nav/footer/etc).
      2. Build a set of "main" containers if any are detected; only
         iterate <a> within those. If none detected, fall back to whole
         document but still skip excluded ancestors.
      3. Filter <a> by text length and boilerplate patterns. Strip
         leading "<N> " numeric prefixes from titles before filtering.
      4. Dedupe by canonical URL — when a URL is seen twice (image
         caption anchor + headline anchor pointing at the same article),
         keep the longer non-boilerplate text.
    """
    if not html:
        return []
    tree = HTMLParser(html)
    if tree.body is None:
        return []

    main_nodes: list[Node] = []
    for sel in _MAIN_SELECTORS:
        main_nodes.extend(tree.css(sel))

    seen: dict[str, NormalizedLink] = {}

    def consider(anchor: Node) -> None:
        if len(seen) >= max_links:
            return
        if _has_excluded_ancestor(anchor):
            return
        href = anchor.attributes.get("href")
        if not href:
            return
        canonical = normalize_url(href, base=base_url)
        if not canonical:
            return
        if _is_slop_url(canonical):
            return
        # Prefer aria-label if anchor text is empty.
        text = _text_of(anchor) or (anchor.attributes.get("aria-label") or "")
        text = " ".join(text.split())
        # Cycle 5.1: strip the "<N> " widget-position prefix before any
        # text-based filtering. Done in-place so the same anchor's
        # cleaned text feeds boilerplate / dedup checks below.
        text = _strip_numeric_prefix(text)
        if _is_boilerplate_text(text):
            return
        if _is_slop_text(text):
            return
        # Cycle 5.1: prefer-better-text dedup. When the same URL appears
        # twice on a page (e.g. lead image + headline anchor), we used to
        # keep the first text seen — sometimes that was the photo
        # credit. Replace with the longer text. Both candidates already
        # passed the boilerplate gate, so we only choose by length.
        existing = seen.get(canonical)
        if existing is not None and len(existing.text) >= len(text):
            return
        seen[canonical] = NormalizedLink(url=canonical, text=text)

    if main_nodes:
        for container in main_nodes:
            for a in container.css("a"):
                consider(a)
    else:
        for a in tree.css("a"):
            consider(a)

    return list(seen.values())


def fingerprint_links(links: Iterable[NormalizedLink]) -> str:
    """Deterministic fingerprint of a link-set.

    We hash only the URL set (sorted), not the text. Text often drifts
    minute-to-minute on live sites (vote counts, "2 hours ago") and we
    don't want that to look like a content change."""
    import hashlib

    urls = sorted({link.url for link in links})
    return hashlib.sha256("\n".join(urls).encode("utf-8")).hexdigest()
