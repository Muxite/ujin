"""Built-in transforms: select (filter/reshape), regex, template, dedupe, chunk.

Each is a small class exposing ``async apply(event) -> dict | list[dict] | None``
(the :class:`ujin.jobs.pipeline.Transform` protocol — a list return fans out into
several downstream events). ``build_transform(kind, cfg)`` maps a kind string to an
instance; the plugin registry extends this with ``plugin:*`` kinds.

Dotted paths ("payload", "payload.title", "regions.main") address nested event
fields with a stdlib walker — no JSONPath dependency for the common case.
"""
from __future__ import annotations

import copy
import re
from collections import OrderedDict
from typing import Any


def dotted_get(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            cur = cur[idx] if idx < len(cur) else None
        else:
            return None
    return cur


def dotted_set(obj: dict, path: str, value: Any) -> None:
    parts = path.split(".")
    cur = obj
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _project(item: Any, fields: list[str]) -> Any:
    if isinstance(item, dict):
        return {f: item.get(f) for f in fields}
    return item


def _matches(item: Any, where: dict) -> bool:
    if not isinstance(item, dict):
        return False
    return all(item.get(k) == v for k, v in where.items())


class SelectTransform:
    """Filter + reshape a target value in the event.

    config:
      path:   dotted path to operate on (default "payload")
      where:  {field: value, ...} — keep only matching items (list targets)
      fields: [name, ...] — project each dict (or list-of-dicts) to these keys
    Writes the result back at ``path``.
    """

    def __init__(self, cfg: dict):
        self.path = cfg.get("path", "payload")
        self.where = cfg.get("where")
        self.fields = cfg.get("fields")

    async def apply(self, event: dict) -> dict | None:
        target = dotted_get(event, self.path)
        if isinstance(target, list):
            if self.where:
                target = [it for it in target if _matches(it, self.where)]
            if self.fields:
                target = [_project(it, self.fields) for it in target]
        else:
            if self.where and not _matches(target, self.where):
                return None  # whole event filtered out
            if self.fields:
                target = _project(target, self.fields)
        dotted_set(event, self.path, target)
        return event


class RegexTransform:
    """Extract regex matches from a field into ``event['extracted']``.

    config: field (dotted, default "payload"), pattern (required), group (int=0).
    """

    def __init__(self, cfg: dict):
        self.field = cfg.get("field", "payload")
        self.pattern = re.compile(cfg["pattern"])
        self.group = cfg.get("group", 0)

    async def apply(self, event: dict) -> dict | None:
        text = dotted_get(event, self.field)
        if text is None:
            event["extracted"] = []
            return event
        event["extracted"] = [m.group(self.group) for m in self.pattern.finditer(str(text))]
        return event


class TemplateTransform:
    """Render ``event['message']`` from a ``str.format`` template over the event.

    config: template (required), e.g. "{job_id} changed -> {fingerprint}".
    """

    def __init__(self, cfg: dict):
        self.template = cfg["template"]

    async def apply(self, event: dict) -> dict | None:
        try:
            event["message"] = self.template.format(**event)
        except Exception:  # noqa: BLE001
            event["message"] = self.template
        return event


class DedupeTransform:
    """Drop already-seen items / events using a bounded LRU.

    config:
      key:     dotted path within each list item to dedupe on (e.g. "DOI").
               When set and payload is a list, filters out seen items; drops the
               event entirely if nothing new remains.
      max:     LRU capacity (default 4096).
    With no ``key``, dedupes whole events by fingerprint.
    """

    def __init__(self, cfg: dict):
        self.key = cfg.get("key")
        self.max = int(cfg.get("max", 4096))
        self._seen: "OrderedDict[str, None]" = OrderedDict()

    def _mark(self, value: str) -> bool:
        """Return True if newly seen, False if already known."""
        if value in self._seen:
            self._seen.move_to_end(value)
            return False
        self._seen[value] = None
        if len(self._seen) > self.max:
            self._seen.popitem(last=False)
        return True

    async def apply(self, event: dict) -> dict | None:
        if self.key is None:
            fp = event.get("fingerprint")
            if fp is None:
                return event
            return event if self._mark(str(fp)) else None

        payload = event.get("payload")
        if isinstance(payload, list):
            fresh = [it for it in payload if self._mark(str(dotted_get(it, self.key)))]
            if not fresh:
                return None
            event["payload"] = fresh
            return event
        # scalar/dict payload: dedupe on its key value
        val = dotted_get(payload, self.key) if isinstance(payload, dict) else payload
        return event if self._mark(str(val)) else None


class ChunkTransform:
    """Fan one event into several, each carrying a slice of a list/text payload.

    Splits an oversized payload into LLM-digestible pieces so downstream sinks
    (e.g. a webhook to an LLM) receive one chunk at a time instead of the whole
    set at once.

    config:
      path:         dotted path to chunk (default "payload")
      size:         items per chunk (list payload) OR chars per chunk (str payload)
      token_budget: alternative to size — approx tokens/chunk (~4 chars/token);
                    for lists, packs items until the budget is hit
    Each output event gets ``chunk_index`` / ``chunk_total`` and the sliced value
    written back at ``path``. A non-list/non-str payload (or empty) passes through
    unchanged as a single event.
    """

    def __init__(self, cfg: dict):
        self.path = cfg.get("path", "payload")
        self.size = cfg.get("size")
        self.token_budget = cfg.get("token_budget")
        if self.size is None and self.token_budget is None:
            self.size = 100  # sensible default

    @staticmethod
    def _approx_tokens(value: Any) -> int:
        # cheap, provider-agnostic estimate: ~4 chars per token
        return max(1, len(str(value)) // 4)

    def _split(self, target: Any) -> list:
        if isinstance(target, list):
            if not target:
                return []
            if self.token_budget:
                out, cur, cur_tok = [], [], 0
                for item in target:
                    tok = self._approx_tokens(item)
                    if cur and cur_tok + tok > self.token_budget:
                        out.append(cur)
                        cur, cur_tok = [], 0
                    cur.append(item)
                    cur_tok += tok
                if cur:
                    out.append(cur)
                return out
            size = max(1, int(self.size))
            return [target[i:i + size] for i in range(0, len(target), size)]
        if isinstance(target, str):
            if not target:
                return []
            width = (self.token_budget * 4) if self.token_budget else max(1, int(self.size))
            return [target[i:i + width] for i in range(0, len(target), width)]
        return []  # not chunkable

    async def apply(self, event: dict) -> "dict | list[dict] | None":
        target = dotted_get(event, self.path)
        chunks = self._split(target)
        if not chunks:
            return event  # nothing to chunk — pass through unchanged
        total = len(chunks)
        out: list[dict] = []
        for i, ch in enumerate(chunks):
            ev = copy.deepcopy(event)
            dotted_set(ev, self.path, ch)
            ev["chunk_index"] = i
            ev["chunk_total"] = total
            out.append(ev)
        return out


BUILTIN_TRANSFORMS = {
    "select": SelectTransform,
    "regex": RegexTransform,
    "template": TemplateTransform,
    "dedupe": DedupeTransform,
    "chunk": ChunkTransform,
}


def build_transform(kind: str, cfg: dict):
    try:
        factory = BUILTIN_TRANSFORMS[kind]
    except KeyError:
        raise ValueError(f"unknown transform kind: {kind!r}") from None
    return factory(cfg or {})
