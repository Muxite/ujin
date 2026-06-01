"""Cross-source headline corroboration via shingle Jaccard.

Every successful link extraction pushes `(text, host, ts)` into a ring
buffer. A "topic" is a connected set of headlines where Jaccard(set_i,
set_j) >= threshold on 6-grams of the lowercased word-stripped text.

The store is small (default 8 192 entries) and accessed only from the
scraper-v2 process — no shared state, no locking concern beyond the
single-event-loop assumption that the rest of the codebase already
makes.
"""

from __future__ import annotations

import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Iterable, Optional

logger = logging.getLogger("ujin.trends.corroboration")


_WORD_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset({
    "the", "a", "an", "of", "in", "on", "at", "to", "for", "and", "or",
    "but", "as", "is", "are", "was", "were", "be", "been", "by", "from",
    "with", "that", "this", "it", "its", "their", "his", "her", "him",
    "she", "he", "they", "we", "you", "i", "say", "says", "said",
    "after", "before", "over", "under", "into", "out", "up", "down",
    "new", "live", "watch", "video", "photos", "report", "reports",
})


def _tokenize(text: str) -> list[str]:
    return [t for t in _WORD_RE.findall(text.lower()) if t not in _STOPWORDS and len(t) > 2]


def shingleset(text: str, n: int = 2) -> frozenset[str]:
    """Bigram shingles + unigram fallback.

    Bigrams strike the right balance for short headlines (10–15 content
    tokens): they capture "putin orders" and "biden announces" while
    tolerating paraphrase ("US Senate passes" vs "Senate approves").
    Below n tokens we keep the bag-of-words so 4-word headlines still
    play.
    """
    toks = _tokenize(text)
    if len(toks) < n:
        return frozenset(toks)
    out: set[str] = set()
    # Bigrams.
    for i in range(len(toks) - n + 1):
        out.add(" ".join(toks[i : i + n]))
    # Unigrams as low-weight fallback — keeps Jaccard non-zero on
    # heavy-paraphrase pairs that share named entities.
    out.update(toks)
    return frozenset(out)


def jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / len(a | b)


@dataclass
class _Entry:
    text: str
    host: str
    ts: float
    shingles: frozenset


@dataclass
class Cluster:
    representative: str
    hosts: set[str]
    members: list[_Entry] = field(default_factory=list)
    first_seen_ts: float = 0.0
    last_seen_ts: float = 0.0

    def velocity_per_min(self) -> float:
        if self.last_seen_ts <= self.first_seen_ts:
            return 0.0
        span_min = max((self.last_seen_ts - self.first_seen_ts) / 60.0, 1.0 / 60.0)
        return len(self.hosts) / span_min


class CorroborationStore:
    """Ring buffer + Jaccard cluster index for cross-source corroboration."""

    def __init__(
        self,
        *,
        window_secs: float = 1800.0,
        max_entries: int = 8192,
        jaccard_threshold: float = 0.20,
        min_hosts_for_corroboration: int = 3,
        max_hosts_for_full_score: int = 5,
    ) -> None:
        self._window = window_secs
        self._max_entries = max_entries
        self._jaccard = jaccard_threshold
        self._min_hosts = min_hosts_for_corroboration
        self._max_hosts = max(min_hosts_for_corroboration, max_hosts_for_full_score)
        self._entries: deque[_Entry] = deque(maxlen=max_entries)

    # ── ingest ────────────────────────────────────────────────────────────

    def add(self, text: str, host: str, ts: Optional[float] = None) -> None:
        if not text or not host:
            return
        ts = ts if ts is not None else time.time()
        sh = shingleset(text)
        if not sh:
            return
        self._entries.append(_Entry(text=text, host=host, ts=ts, shingles=sh))
        self._evict_old(ts)

    def _evict_old(self, now: float) -> None:
        cutoff = now - self._window
        while self._entries and self._entries[0].ts < cutoff:
            self._entries.popleft()

    # ── query ─────────────────────────────────────────────────────────────

    def lookup_score(self, text: str, host: str) -> tuple[float, int]:
        """Return (corroboration_score in [0,1], unique_host_count) for `text`.

        The host the headline came from is included in the unique-host
        count; corroboration requires >=2 other hosts past the source.
        """
        sh = shingleset(text)
        if not sh:
            return 0.0, 0
        hosts: set[str] = {host}
        for e in self._entries:
            if jaccard(sh, e.shingles) >= self._jaccard:
                hosts.add(e.host)
        n = len(hosts)
        if n < self._min_hosts:
            return 0.0, n
        if n >= self._max_hosts:
            return 1.0, n
        # Linear interp between min and max host count.
        span = self._max_hosts - self._min_hosts
        return (n - self._min_hosts) / span * 0.5 + 0.5, n

    def clusters(self) -> list[Cluster]:
        """Greedy single-pass clustering of the current ring buffer.

        O(N * K) where K is the current number of open clusters; the cap
        on _max_entries keeps this trivial in practice (~8 k headlines).
        """
        out: list[Cluster] = []
        now = time.time()
        self._evict_old(now)
        for e in self._entries:
            placed = False
            for c in out:
                if jaccard(e.shingles, c.members[0].shingles) >= self._jaccard:
                    c.members.append(e)
                    c.hosts.add(e.host)
                    c.last_seen_ts = max(c.last_seen_ts, e.ts)
                    # Keep the longest member text as the representative.
                    if len(e.text) > len(c.representative):
                        c.representative = e.text
                    placed = True
                    break
            if not placed:
                out.append(
                    Cluster(
                        representative=e.text,
                        hosts={e.host},
                        members=[e],
                        first_seen_ts=e.ts,
                        last_seen_ts=e.ts,
                    )
                )
        return [c for c in out if len(c.hosts) >= self._min_hosts]

    # ── debug ─────────────────────────────────────────────────────────────

    def size(self) -> int:
        return len(self._entries)

    def iter_recent(self, n: int = 50) -> Iterable[_Entry]:
        return list(self._entries)[-n:]
