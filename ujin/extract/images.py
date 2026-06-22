"""Image extraction: every ``<img>`` → one normalized dict.

    extract_images('<img src="/a.jpg" alt="A" width="640">',
                   base_url="https://x.test/page")
    # -> [{"src": "https://x.test/a.jpg", "alt": "A", "width": 640}]

Per ``<img>`` the returned dict always carries an absolute ``src`` and an
``alt`` string (``""`` when the attribute is missing), plus an integer
``width``/``height`` and a ``title`` only when those are present and usable.

Source resolution, in order:

* The ``src`` attribute wins when it is a real URL. When ``src`` is a ``data:``
  URI placeholder (the common lazy-load pattern) the real target is taken from
  ``data-src`` / ``data-original`` or, failing those, the first ``srcset``
  candidate — so a ``data:`` URI is skipped whenever another src exists for the
  same image. A lone ``data:`` URI (no other candidate) is kept as-is.
* Relative URLs are made absolute against ``base_url`` (``urljoin``); with no
  ``base_url`` they are returned unchanged.

Identical resolved ``src`` values are de-duplicated, first occurrence kept, in
document order. Robust by contract: empty or malformed input yields ``[]``
rather than raising. Uses selectolax (``web``/``diff`` extra), like
:mod:`ujin.extract.tables`.
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import urljoin


def extract_images(html: str, base_url: Optional[str] = None) -> list[dict]:
    """Parse every ``<img>`` in ``html`` into a normalized list of dicts.

    See the module docstring for the src-resolution and de-dup rules. Never
    raises: bad input or a parser hiccup returns whatever images were assembled
    so far (``[]`` in the worst case).
    """
    try:
        from selectolax.parser import HTMLParser

        tree = HTMLParser(html or "")
    except Exception:  # noqa: BLE001 — a parser failure must not propagate
        return []

    out: list[dict] = []
    seen: set[str] = set()
    try:
        for node in tree.css("img"):
            try:
                rec = _image_record(node, base_url)
            except Exception:  # noqa: BLE001 — skip a single malformed node
                continue
            if rec is None:
                continue
            src = rec["src"]
            if src in seen:
                continue  # dedupe identical src, keeping document order
            seen.add(src)
            out.append(rec)
    except Exception:  # noqa: BLE001 — keep whatever we managed to parse
        return out
    return out


def _image_record(node, base_url: Optional[str]) -> Optional[dict]:
    """Build the normalized dict for one ``<img>`` node (or ``None`` to skip)."""
    attrs = node.attributes
    src = _resolve_src(attrs, base_url)
    if not src:
        return None

    rec: dict = {"src": src, "alt": (attrs.get("alt") or "").strip()}

    width = _int_attr(attrs, "width")
    if width is not None:
        rec["width"] = width
    height = _int_attr(attrs, "height")
    if height is not None:
        rec["height"] = height

    title = attrs.get("title")
    if title is not None and title.strip():
        rec["title"] = title.strip()

    return rec


def _resolve_src(attrs, base_url: Optional[str]) -> Optional[str]:
    """Pick the best source URL for an image and make it absolute.

    Candidates are gathered in priority order (``src``, the lazy-load
    ``data-src`` / ``data-original``, then the first ``srcset`` candidate); the
    first non-``data:`` candidate wins so a ``data:`` placeholder is skipped
    whenever a real src exists. A lone ``data:`` URI is kept as the only option.
    """
    candidates: list[str] = []
    for attr in ("src", "data-src", "data-original"):
        value = attrs.get(attr)
        if value and value.strip():
            candidates.append(value.strip())
    first_srcset = _first_srcset(attrs.get("srcset"))
    if first_srcset:
        candidates.append(first_srcset)

    if not candidates:
        return None

    chosen = next((c for c in candidates if not _is_data_uri(c)), candidates[0])
    if _is_data_uri(chosen):
        return chosen  # only reached when every candidate is a data: URI
    if base_url:
        try:
            return urljoin(base_url, chosen)
        except Exception:  # noqa: BLE001 — a join failure keeps the raw value
            return chosen
    return chosen


def _first_srcset(srcset: Optional[str]) -> Optional[str]:
    """First URL of a ``srcset`` value, with its ``1x``/``640w`` descriptor dropped."""
    if not srcset:
        return None
    first = srcset.split(",", 1)[0].strip()
    if not first:
        return None
    return first.split()[0]


def _is_data_uri(url: str) -> bool:
    return url.strip().lower().startswith("data:")


def _int_attr(attrs, name: str) -> Optional[int]:
    """Parse a ``width``/``height`` attribute into a non-negative int, or ``None``."""
    raw = attrs.get(name)
    if raw is None:
        return None
    raw = raw.strip()
    if raw.lower().endswith("px"):
        raw = raw[:-2].strip()
    try:
        value = int(raw)
    except (ValueError, TypeError):
        return None
    return value if value >= 0 else None
