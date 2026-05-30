"""Article-mode extraction: cleaned title/body/byline/published using trafilatura.

Vendored from jennie/services/scraper-v2/app/extract/article.py.
Modifications:
- Removed jennie config dependency; MIN_OUTPUT_SIZE baked in.
- Logger name changed to awork.scrape.extract.
- extract_article_lenient() added: skips index-page heuristics for cases
  where we know the URL is a deliberate content page (e.g. a README view).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit


# Path patterns that almost always mean "this is an index page, not a story."
_INDEX_PATH_RE = re.compile(
    r"""^/?
        (sections?|category|categories|tag|tags|topics?|news|world|politics|
         business|sports?|opinion|technology|tech|science|health|culture|
         latest|trending|live)?
        /?$
    """,
    re.IGNORECASE | re.VERBOSE,
)

_INDEX_TITLE_TOKENS = frozenset({
    "world", "politics", "business", "sports", "opinion", "latest",
    "technology", "tech", "science", "health", "news", "home",
    "trending", "live",
})


@dataclass
class Article:
    url: str
    title: Optional[str]
    text: str
    byline: Optional[str]
    published: Optional[str]
    language: Optional[str]
    top_image: Optional[str]


def _looks_like_index_url(url: str) -> bool:
    path = urlsplit(url).path
    return bool(_INDEX_PATH_RE.match(path))


def _looks_like_index_body(title: Optional[str], text: str) -> bool:
    if title:
        title_words = [w.lower().strip() for w in re.split(r"\W+", title) if w]
        if len(title_words) <= 2 and any(
            w in _INDEX_TITLE_TOKENS for w in title_words
        ):
            return True
    if len(text) >= 500:
        return False
    paragraphs = [p.strip() for p in re.split(r"\n+", text) if p.strip()]
    long_paragraphs = sum(1 for p in paragraphs if len(p) > 200)
    return long_paragraphs < 1


def _run_trafilatura(html: str, url: str) -> Optional[dict]:
    """Run trafilatura and return parsed JSON dict, or None."""
    try:
        import json
        import trafilatura
        from trafilatura.settings import use_config

        cfg = use_config()
        cfg.set("DEFAULT", "MIN_OUTPUT_SIZE", "80")
        cfg.set("DEFAULT", "MIN_EXTRACTED_SIZE", "80")

        extracted = trafilatura.extract(
            html,
            url=url,
            output_format="json",
            with_metadata=True,
            include_comments=False,
            include_tables=False,
            favor_precision=True,
            config=cfg,
        )
        if not extracted:
            return None
        return json.loads(extracted)
    except ImportError as exc:
        raise ImportError(
            "trafilatura is required for article extraction: "
            "pip install 'awork[scrape]'"
        ) from exc
    except Exception:  # noqa: BLE001
        return None


def extract_article(html: str, url: str) -> Optional[Article]:
    """Extract a cleaned article from HTML.

    Returns None when:
      - html is empty / too short
      - trafilatura returns no body
      - the URL or extracted body matches an index-page heuristic
    """
    if not html:
        return None
    if _looks_like_index_url(url):
        return None

    data = _run_trafilatura(html, url)
    if data is None:
        return None

    text = (data.get("text") or "").strip()
    if not text:
        return None

    title = data.get("title")
    if _looks_like_index_body(title, text):
        return None

    return Article(
        url=url,
        title=title,
        text=text,
        byline=data.get("author"),
        published=data.get("date"),
        language=data.get("language"),
        top_image=data.get("image"),
    )


def extract_article_lenient(html: str, url: str) -> Optional[Article]:
    """Like extract_article but skips index-page heuristics.

    Use when you already know the URL points to real content (e.g. a
    GitHub rendered README page) and you just want whatever trafilatura
    can pull out.
    """
    if not html:
        return None

    data = _run_trafilatura(html, url)
    if data is None:
        return None

    text = (data.get("text") or "").strip()
    if not text:
        return None

    return Article(
        url=url,
        title=data.get("title"),
        text=text,
        byline=data.get("author"),
        published=data.get("date"),
        language=data.get("language"),
        top_image=data.get("image"),
    )
