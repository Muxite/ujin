# List reshaping transforms + the CSV sink

Five additive job kinds for the common "narrow a list, reorder it, fan it out,
and write it somewhere tabular" shape — all pure stdlib, all wired through the
same registry as the existing kinds. They slot anywhere in a job's
`transforms` / `sinks` arrays (see [JOBS.md](JOBS.md)).

| kind | category | one-liner |
|------|----------|-----------|
| `flatten` | transform | fan a list payload into one event per item |
| `sort`    | transform | sort a list payload by a dotted key |
| `limit`   | transform | keep the first/last N items |
| `rename`  | transform | rename dict keys (across a list too) |
| `csv`     | sink      | append event rows to a CSV/TSV file |

All five are discoverable at `GET /kinds` and build through `ujin.registry`.

## `flatten` — one event per list item

Where a source yields a list (API rows, RSS entries, scraped links), `flatten`
emits a *separate* event per element so per-item transforms/sinks see one item
at a time. The inverse of `chunk` (which groups). A non-list target passes
through unchanged; an empty list drops the event.

```yaml
transforms:
  - kind: flatten
    config:
      path: payload        # default; dotted paths like "payload.items" work too
      index: item_index    # optional: stamp each event with its 0-based position
```

`payload: [a, b, c]` → three events, `payload` = `a`, `b`, `c` (with
`item_index` 0/1/2). Each event is deep-copied, so downstream mutation is
isolated.

## `sort` — order a list by a key

```yaml
transforms:
  - kind: sort
    config:
      path: payload      # default
      key: score         # dotted path within each item; omit for natural order
      reverse: true      # descending (default false)
```

Items missing the key go to the **end of ascending order**. `sort` reverses that
whole order for `reverse: true`, so under `reverse: true` the key-less items
appear **first** — keep that in mind for the "top-N by score" idiom below. Mixed
or uncomparable values never raise (grouped by type, with a `str()` fallback) so
a heterogeneous payload can't crash the pipeline.

## `limit` — take the top/bottom N

```yaml
transforms:
  - kind: sort
    config: { key: score, reverse: true }
  - kind: limit
    config:
      count: 10          # required; a negative value clamps to 0
      from: head         # "head" (first N, default) or "tail" (last N)
```

`sort` + `limit` is the idiomatic "top-N by score". `count` is clamped at 0
(a negative `count` empties the list); a `count` larger than the list keeps
everything. Because `sort … reverse: true` floats key-less items to the front
(see `sort` above), a row missing the sort key can occupy a top-N slot — drop
such rows with a `select` `where` filter before `limit` if that matters.

## `rename` — remap dict keys

```yaml
transforms:
  - kind: rename
    config:
      mapping: { DOI: id, title: name }   # required: {old: new}
      # drop_missing: false  # true -> materialize new keys (as null) even when
      #                      #         the source key is absent
```

Applies to a dict payload or each dict in a list payload. Keys not in `mapping`
are preserved; non-dict items in a list pass through untouched.

## `csv` sink — append rows to a file

Resolves a list/dict from the event and writes one CSV row per dict — pure
stdlib, no extra dependency. The header is written once when the file is created;
the column set is locked on first use so appends stay aligned.

```yaml
sinks:
  - kind: csv
    config:
      path: /data/out.csv     # required
      columns: [id, name]     # optional: explicit order; omit to infer from row 1
      path_in_event: payload  # default; where the rows live in the event
      header: true            # write a header row on file creation (default true)
      delimiter: ","          # "\t" for TSV, etc.
```

Missing columns become empty cells, unknown keys are ignored, and non-dict items
are skipped. An event with no dict rows is a silent no-op (the file is not even
created).

## End-to-end: top-5 fresh items to a webhook *and* a CSV

```yaml
source:
  kind: api
  config:
    url: "https://api.example.com/items"
    json_path: items
transforms:
  - kind: dedupe
    config: { key: id }                     # only newly-seen ids
  - kind: sort
    config: { key: score, reverse: true }   # hottest first
  - kind: limit
    config: { count: 5 }                    # top 5
  - kind: rename
    config: { mapping: { id: item_id } }    # match the downstream schema
sinks:
  - kind: csv
    config: { path: /data/top.csv, columns: [item_id, score] }
  - kind: webhook
    config: { url: "https://hooks.example.com/ingest" }
schedule:
  mode: adaptive
  base: 3600
```

To fan the same list out as one webhook call per item instead, drop a
`flatten` before the sinks.
