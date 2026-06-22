"""Built-in transforms: select, regex, template, dedupe, chunk, flatten, sort,
limit, rename, aggregate.

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


class FlattenTransform:
    """Fan a list payload into one downstream event per item.

    The inverse of accumulating: where a source yields a list (API rows, RSS
    entries, scraped links), ``flatten`` emits a separate event for each element
    so per-item transforms/sinks see one item at a time. A non-list target
    passes through unchanged as a single event.

    config:
      path:  dotted path to the list (default "payload")
      index: when set (e.g. "item_index"), each emitted event gets this field
             carrying the item's 0-based position in the original list
    Each output event has the single item written back at ``path``.
    """

    def __init__(self, cfg: dict):
        self.path = cfg.get("path", "payload")
        self.index = cfg.get("index")

    async def apply(self, event: dict) -> "dict | list[dict] | None":
        target = dotted_get(event, self.path)
        if not isinstance(target, list):
            return event  # nothing to flatten — pass through unchanged
        out: list[dict] = []
        for i, item in enumerate(target):
            ev = copy.deepcopy(event)
            dotted_set(ev, self.path, item)
            if self.index:
                ev[self.index] = i
            out.append(ev)
        return out  # empty list drops the event (nothing to emit)


class SortTransform:
    """Sort a list payload in place by a dotted key (or natural order).

    config:
      path:    dotted path to the list (default "payload")
      key:     dotted path within each item to sort on (optional; sorts the raw
               items when omitted)
      reverse: descending when true (default false)
    Items missing the key (or non-comparable) never raise; they sort to the end of
    ascending order, which means they appear *first* when ``reverse`` is true
    (the whole order is reversed). A non-list target passes through unchanged.
    """

    def __init__(self, cfg: dict):
        self.path = cfg.get("path", "payload")
        self.key = cfg.get("key")
        self.reverse = bool(cfg.get("reverse", False))

    def _sort_key(self, item: Any):
        val = dotted_get(item, self.key) if self.key else item
        # (missing-flag, type-name, value) keeps mixed/None payloads from raising
        # TypeError on comparison and pushes missing values to the end.
        if val is None:
            return (1, "", "")
        return (0, type(val).__name__, val)

    async def apply(self, event: dict) -> dict | None:
        target = dotted_get(event, self.path)
        if not isinstance(target, list):
            return event
        try:
            ordered = sorted(target, key=self._sort_key, reverse=self.reverse)
        except TypeError:
            # values of the same type that still don't compare — fall back to str
            ordered = sorted(
                target,
                key=lambda it: str(dotted_get(it, self.key) if self.key else it),
                reverse=self.reverse,
            )
        dotted_set(event, self.path, ordered)
        return event


class LimitTransform:
    """Cap a list payload to the first/last N items.

    config:
      path:   dotted path to the list (default "payload")
      count:  max items to keep (required, >= 0)
      from_:  "head" (default) keeps the first N, "tail" keeps the last N.
              Accepted under config key ``from``.
    A non-list target passes through unchanged.
    """

    def __init__(self, cfg: dict):
        self.path = cfg.get("path", "payload")
        if "count" not in cfg:
            raise ValueError("limit transform requires 'count'")
        self.count = max(0, int(cfg["count"]))
        self.from_ = cfg.get("from", "head")
        if self.from_ not in ("head", "tail"):
            raise ValueError("limit 'from' must be 'head' or 'tail'")

    async def apply(self, event: dict) -> dict | None:
        target = dotted_get(event, self.path)
        if not isinstance(target, list):
            return event
        if self.from_ == "tail":
            limited = target[-self.count:] if self.count else []
        else:
            limited = target[: self.count]
        dotted_set(event, self.path, limited)
        return event


class RenameTransform:
    """Rename keys on dict items (applies across a list of dicts too).

    config:
      path:    dotted path to operate on (default "payload")
      mapping: {old_key: new_key, ...} (required)
      drop_missing: when true, materialize the new key (as ``None``) even when
                    the source key is absent; when false (default), absent source
                    keys are simply left alone.
    Operates on a dict target, or each dict in a list target. Existing keys not
    in ``mapping`` are preserved; a non-dict (and non-list-of-dicts) target
    passes through unchanged.
    """

    def __init__(self, cfg: dict):
        self.path = cfg.get("path", "payload")
        self.mapping = dict(cfg.get("mapping") or {})
        if not self.mapping:
            raise ValueError("rename transform requires a non-empty 'mapping'")
        self.drop_missing = bool(cfg.get("drop_missing", False))

    def _rename(self, item: Any) -> Any:
        if not isinstance(item, dict):
            return item
        out = dict(item)
        for old, new in self.mapping.items():
            if old in out:
                out[new] = out.pop(old)
            elif self.drop_missing:
                out[new] = None
        return out

    async def apply(self, event: dict) -> dict | None:
        target = dotted_get(event, self.path)
        if isinstance(target, list):
            target = [self._rename(it) for it in target]
        else:
            target = self._rename(target)
        dotted_set(event, self.path, target)
        return event


class AggregateTransform:
    """Group a list payload by a dotted key and compute per-group stats.

    config:
      by:     dotted path within each item to group on (required)
      fields: list of {field: dotted-path, op: sum|min|max|collect}
              per-group ``count`` is always emitted; these add named aggregates
              keyed as ``<field-label>_<op>`` (last segment of the field path)
      path:   dotted path to the list (default "payload")
      out:    dotted path to write the grouped result (default: same as ``path``)
    Non-list or empty payloads pass through unchanged.
    """

    def __init__(self, cfg: dict):
        self.path = cfg.get("path", "payload")
        self.out = cfg.get("out", self.path)
        if "by" not in cfg:
            raise ValueError("aggregate transform requires 'by'")
        self.by = cfg["by"]
        self.by_label = self.by.split(".")[-1]
        self.fields = list(cfg.get("fields") or [])

    async def apply(self, event: dict) -> dict | None:
        target = dotted_get(event, self.path)
        if not isinstance(target, list) or not target:
            return event

        # Collect items per group, preserving first-seen insertion order.
        groups: dict = {}  # hashable key -> [group_value, items]
        order: list = []
        for item in target:
            gval = dotted_get(item, self.by) if isinstance(item, dict) else None
            try:
                hkey = gval
                hash(hkey)
            except TypeError:
                hkey = str(gval)
            if hkey not in groups:
                groups[hkey] = [gval, []]
                order.append(hkey)
            groups[hkey][1].append(item)

        result = []
        for hkey in order:
            gval, items = groups[hkey]
            row: dict = {self.by_label: gval, "count": len(items)}
            for fspec in self.fields:
                field = fspec["field"]
                op = fspec["op"]
                col = f"{field.split('.')[-1]}_{op}"
                vals = []
                for it in items:
                    if isinstance(it, dict):
                        v = dotted_get(it, field)
                        if v is not None:
                            vals.append(v)
                if op == "sum":
                    row[col] = sum(vals) if vals else 0
                elif op == "min":
                    row[col] = min(vals) if vals else None
                elif op == "max":
                    row[col] = max(vals) if vals else None
                elif op == "collect":
                    row[col] = vals
            result.append(row)

        dotted_set(event, self.out, result)
        return event


BUILTIN_TRANSFORMS = {
    "select": SelectTransform,
    "regex": RegexTransform,
    "template": TemplateTransform,
    "dedupe": DedupeTransform,
    "chunk": ChunkTransform,
    "flatten": FlattenTransform,
    "sort": SortTransform,
    "limit": LimitTransform,
    "rename": RenameTransform,
    "aggregate": AggregateTransform,
}


def build_transform(kind: str, cfg: dict):
    try:
        factory = BUILTIN_TRANSFORMS[kind]
    except KeyError:
        raise ValueError(f"unknown transform kind: {kind!r}") from None
    return factory(cfg or {})
