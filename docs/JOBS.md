# ujin jobs — the unified control plane

One service, one configurable unit. A **job** is:

```
source  ->  transforms  ->  sinks      (on a schedule)
```

expressed entirely as data over REST/WS. The poller roles and the rich scrape
service both become *source kinds* under it, so almost any recurring data task —
"watch this API for new rows, filter them, post them somewhere, on an adaptive
timer" — is a single `POST /jobs` body. No code required for the common cases;
drop in Python only when a built-in doesn't cover it (see [PLUGINS.md](PLUGINS.md)).

Run it:

```bash
ujin jobs-serve                       # :8902, jobstore at $UJIN_JOBS_DB (./ujin-jobs.db)
ujin jobs-serve examples/jobs.crossref.yaml   # preload jobs from YAML
docker compose up ujin-jobs           # durable jobstore volume + ./plugins mounted
```

Jobs are persisted (sqlite) and **reloaded on restart** — the gap the old
in-memory `POST /targets` left open.

## The job spec

```jsonc
{
  "name": "crossref-quantum",
  "source":     { "kind": "api", "config": { "url": "...", "json_path": "message.items" } },
  "transforms": [ { "kind": "select", "config": { "where": { "type": "journal-article" } } } ],
  "sinks":      [ { "kind": "webhook", "config": { "url": "https://hooks/me" } },
                  { "kind": "jsonl",   "config": { "path": "/data/out.jsonl" } } ],
  "schedule":   { "mode": "adaptive", "base": 3600, "min": 600, "max": 86400, "jitter": "decorrelated" }
}
```

### Sources (`kind`)
| kind | config | notes |
|------|--------|-------|
| `http` | `url`, `render?` | fingerprints the page body (304-aware) |
| `site` | `url`, `selectors[]`, `render?` | fingerprints only selector-scoped regions |
| `rss` | `url` | fingerprints the set of entry URLs |
| `api` | `url`, `method?`, `json_path?`, `headers?`, `json_body?` | narrows JSON to a dotted path, then fingerprints that slice |
| `graphql` | `url`, `query`, `variables?`, `headers?`, `data_path?` | POSTs a GraphQL query; narrows the response to a dotted `data_path` and fingerprints that slice; GraphQL `errors`, non-200, and network failures are surfaced without crashing the poll loop |
| `command` | `argv[]` | fingerprints stdout |
| `scrape` | `url`, `mode?`, `force_refresh?` | full HTTP→obscura→sitemap→RSS chain + extraction |
| `browser` | `url`, `engine?`, `actions[]`, `extract?`, `results_selector?` | drive a real browser through an interaction recipe (`load_more`, scroll, click), then extract — see [BROWSER.md](BROWSER.md) |
| `plugin:<name>` | (plugin-defined) | a custom source — see PLUGINS.md |

### Transforms (`kind`) — run in order; one returning nothing drops the event
| kind | config | effect |
|------|--------|--------|
| `select` | `path?` (default `payload`), `where?{field:val}`, `fields?[]` | filter list items / project fields / drop non-matching events |
| `regex` | `field?`, `pattern`, `group?` | matches → `event.extracted` |
| `template` | `template` | `str.format` over the event → `event.message` |
| `dedupe` | `key?` (dotted), `max?` | drop already-seen items (by key) or whole events (by fingerprint) |
| `chunk` | `size` or `token_budget`, `path?` | fan a large list/text payload into one event per chunk (LLM-sized) — see [recipes/feed-an-llm-with-chunking.md](recipes/feed-an-llm-with-chunking.md) |
| `flatten` | `path?`, `index?` | fan a list payload into one event per item (inverse of accumulating) — see [LIST_TRANSFORMS.md](LIST_TRANSFORMS.md) |
| `sort` | `path?`, `key?` (dotted), `reverse?` | sort a list payload by a key; missing/uncomparable values never raise — they land last in ascending order, first under `reverse: true` |
| `limit` | `path?`, `count` (required), `from?` (`head`/`tail`) | cap a list payload to the first/last N items |
| `rename` | `path?`, `mapping` (required), `drop_missing?` | rename keys on a dict (or each dict in a list) |
| `aggregate` | `by` (required, dotted), `path?`, `out?`, `fields?[{field,op}]` | group a list by a key; emit one row per group with `count` and optional `sum`/`min`/`max`/`collect` aggregates — see [LIST_TRANSFORMS.md](LIST_TRANSFORMS.md) |
| `unique` | `path?`, `key?` (dotted) | drop duplicate items from a list by a dotted key (or whole-item identity when key omitted); first occurrence wins — see [LIST_TRANSFORMS.md](LIST_TRANSFORMS.md) |
| `fill` | `path?`, `fields?{dotted:val}`, `paths?[]`, `value?` | add default values for missing (None) dotted fields on dicts in a list, without overwriting existing values — see [LIST_TRANSFORMS.md](LIST_TRANSFORMS.md) |

### Sinks (`kind`) — fan out concurrently; one failing sink never blocks the others
| kind | config | effect |
|------|--------|--------|
| `webhook` | `url`, `method?`, `headers?`, `hmac_secret?` | POST JSON; optional `X-Ujin-Signature: sha256=…` |
| `forward` | same as webhook | generalized forward to another HTTP service |
| `ws` | — | push the full event to `WS /jobs/events` clients |
| `jsonl` / `file` | `path` | append one JSON line per event |
| `stdout` | `prefix?` | print JSON (default, dependency-free) |
| `sqlite` | — | persist into the jobstore's `job_events` table (`GET /jobs/{id}/events`) |
| `csv` | `path`, `columns?`, `path_in_event?`, `header?`, `delimiter?` | append event rows to a CSV/TSV file (pure stdlib) — see [LIST_TRANSFORMS.md](LIST_TRANSFORMS.md) |
| `plugin:<name>` | (plugin-defined) | a custom sink |

### Schedule (`mode`)
- `adaptive` (default): registered with the engine; interval grows when nothing
  changes and shrinks on change (`base`/`min`/`max`/`grow`/`shrink`/`jitter`).
- `cron`: `cron: "*/5 * * * *"` (5-field, minute resolution).
- `once`: runs a single time; not re-run on restart.

## REST surface (`:8902`)

```
GET    /health                  {ok, jobs, plugins, workflows}
GET    /kinds                   available source/transform/sink kinds
GET    /metrics                 engine stats + per-job + plugin status
GET    /jobs                    list job summaries
POST   /jobs                    create (JobCreate) -> {id}   (400 on unknown kind)
GET    /jobs/{id}               full spec + runtime state
DELETE /jobs/{id}
POST   /jobs/{id}/run           run now (one-shot poll + pipeline)
POST   /jobs/{id}/pause | /resume
GET    /jobs/{id}/runs?limit=N  run history
GET    /jobs/{id}/events?limit=N  events persisted by the `sqlite` sink
GET    /jobs/{id}/content       latest obtained payload (the data ujin last got)
GET    /jobs/{id}/results?limit=N  recent buffer of obtained results (per change)
WS     /jobs/events             live change stream (all jobs)
POST   /plugins/reload          re-import plugins -> {loaded, failed}
```

File-driven jobs (**workflows**) also load from `UJIN_WORKFLOWS_DIR` on startup
(id = filename stem). See [WORKFLOWS.md](WORKFLOWS.md).

Every change also emits a compact `{"event":"change","job_id",...}` on
`WS /jobs/events`, independent of whether the job has a `ws` sink.

## Auth (optional)

Off by default — trust the network / run behind a reverse proxy. Set
`UJIN_API_KEY` to require `X-API-Key: <key>` or `Authorization: Bearer <key>` on
every route except `/health` (HTTP **and** WebSocket).

## Python SDK

```python
from ujin.jobs.client import JobsClient

with JobsClient("http://localhost:8902", api_key="...") as jc:   # api_key optional
    jid = jc.create({...})        # the spec above
    jc.run(jid)
    print(jc.runs(jid))
```

## The Crossref walkthrough

`examples/jobs.crossref.yaml` is the motivating case end-to-end: poll
`api.crossref.org/works?...`, narrow to `message.items`, filter to journal
articles + chosen fields + new DOIs, fan out to a webhook and a JSONL file, on an
adaptive jittered timer. It is pure configuration — the same shape covers any
JSON API with a known structure.
