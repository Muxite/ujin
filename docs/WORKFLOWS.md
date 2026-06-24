# ujin workflows — setup → collect → serve

A **workflow** is a job you don't create over the API — you drop a definition file
into a mounted directory and ujin sets it up on startup, runs it, and hands back
whatever it obtained. It's the file-driven face of the [jobs](JOBS.md) control
plane: same `source → transforms → sinks → schedule` shape, same engine, same
durability. The intended use is **polling jobs / repeated tasks over similar
sites**: configure once, let the container collect, pull the results by id.

```
 setup                      collect                     serve
 ─────                      ───────                     ─────
 /workflows/*.yaml   ──▶    PollEngine + cron    ──▶    GET /jobs/{id}/content
 (+ /plugins/*.py)          (adaptive cadence)          GET /jobs/{id}/results
                                                        WS  /jobs/events
```

## Setup — the workflows directory

`UJIN_WORKFLOWS_DIR` (default `/workflows`) is scanned on startup. In the
container it's a mounted volume:

```yaml
# docker-compose.yml (ujin-jobs)
volumes:
  - ./workflows:/workflows    # workflow files  (id = filename stem)
  - ./plugins:/plugins        # custom capabilities (plugin:* kinds)
```

Each `*.yaml` / `*.yml` file is **one workflow**. The **filename stem is the
workflow id** (and default name), so `crossref-papers.yaml` becomes workflow
`crossref-papers`. Because the id is stable, re-deploying or restarting **upserts
the same workflow** rather than duplicating it.

```yaml
# /workflows/crossref-papers.yaml   ->  workflow id "crossref-papers"
source:
  kind: api
  config:
    url: "https://api.crossref.org/works?query=quantum&rows=50"
    json_path: message.items
transforms:
  - kind: dedupe
    config: { key: DOI }
sinks:
  - kind: sqlite           # record change events durably
schedule:
  mode: adaptive
  base: 3600
  min: 600
  max: 86400
```

- A file with no top-level `jobs:` key is the single workflow (the whole mapping).
- Set `id:` inside the file to override the stem.
- A file may also hold a `jobs: [...]` list / top-level list; entries without an
  `id` fall back to `<stem>-<index>` so ids stay deterministic.
- Validation is eager: an unknown source/transform/sink kind lands in the
  `failed` list (see `GET /health`) instead of aborting startup.

Run it locally without Docker:

```bash
ujin jobs-serve --workflows ./examples/workflows      # or set UJIN_WORKFLOWS_DIR
```

`GET /health` reports what was set up:

```json
{ "ok": true, "jobs": 2,
  "workflows": { "dir": "/workflows", "loaded": ["crossref-papers", "example-page"], "failed": [] } }
```

## Defaults, fragments, and matrix fan-out

When you run **many similar workflows**, three optional, purely-additive conveniences
keep the files DRY: a `defaults:` block, `include:`/`use:` fragments, and a
`matrix:`/`for_each:` fan-out (they compose in that order). A workflow that uses none
loads byte-for-byte as before, so existing single-file workflows, `{jobs: [...]}`/list
forms, filename-stem ids, and `${VAR}`/`${VAR:-default}` substitution are unchanged.

### `defaults:` — shared keys, deep-merged into each job

A top-level `defaults:` mapping is **deep-merged under every job** in the file,
with per-job keys winning. Nested maps (`source`, `schedule`, …) merge recursively;
**lists replace** (no concatenation) when a job sets them, and are inherited whole
when a job omits them.

```yaml
# /workflows/site-feeds.yaml  ->  ids "site-feeds-0", "site-feeds-1"
defaults:
  source: { kind: api, config: { method: GET, json_path: items } }
  transforms: [ { kind: dedupe, config: { key: id } } ]
  schedule:  { mode: adaptive, base: 3600 }
jobs:
  - name: news
    source: { config: { url: "https://feeds.example.com/news" } }   # kind/method inherited
  - name: jobs
    source: { config: { url: "https://feeds.example.com/jobs" } }
    schedule: { base: 600 }          # overrides only `base`; `mode` inherited
```

Both jobs inherit the `api` source kind, the `dedupe` transform, and the adaptive
schedule; `news` polls hourly while `jobs` overrides `base` to 600s.

### `include:` / `use:` — reference a fragment file

`include:` (alias `use:`) pulls in a **fragment file** so a whole job, or a
sub-section — a sink, a schedule, or a transform pipeline — can be shared. The
result is identical to inlining the fragment. Fragment paths resolve relative to
the **including file's directory**, then `$UJIN_WORKFLOWS_DIR`.

```yaml
sinks:
  - include: fragments/webhook-sink.yaml   # a mapping fragment -> one sink
  - kind: sqlite
transforms:
  - include: fragments/clean.yaml          # a list fragment -> spliced into the pipeline
  - kind: limit
    config: { n: 5 }
schedule:
  include: fragments/adaptive-hourly.yaml
  base: 600                                 # keys alongside an include override the fragment
```

- A mapping with `include:` is deep-merged **over** the fragment (local keys win;
  several paths apply left-to-right).
- In a list, an item whose `include:` expands to a **list** is spliced in (so a
  transform-pipeline fragment drops straight into `transforms:`); one that expands
  to a mapping is inserted as a single item.
- Keep fragments in a **subdirectory** (e.g. `fragments/`): the startup scan is
  **non-recursive** and only reads top-level `*.yaml`/`*.yml`, so a fragment in a
  subdirectory is never loaded as a standalone workflow.
- A **missing or cyclic** include fails just that workflow with an actionable
  error in the `failed` list (see `GET /health`); other workflows still load.

See `examples/workflows/site-feeds.yaml` (+ `examples/workflows/fragments/`) for a
runnable end-to-end example.

### `matrix:` / `for_each:` — fan one template into many

A workflow mapping (or a `jobs:` list entry) may carry a **`matrix:`** key — alias
**`for_each:`** — a list of variable maps. ujin loads it as **one workflow per
entry**, substituting each entry's variables into every `{{ var }}` placeholder
across the `source`, `transforms`, `sinks`, and `schedule`. One definition, N
saved searches:

```yaml
# /workflows/marketplace-search.yaml
matrix:
  - { slug: laptop,  query: "gaming laptop", floor: 500 }
  - { slug: gpu,     query: "rtx 4090",      floor: 800 }
  - { slug: monitor, query: "4k monitor",    floor: 200 }

id: "search-{{ slug }}"          # -> search-laptop, search-gpu, search-monitor
source:
  kind: api
  config:
    url: "https://api.example.com/search?q={{ query }}&min_price={{ floor }}"
    json_path: results
sinks:
  - kind: sqlite
  - kind: jsonl
    config: { path: "/data/{{ slug }}.jsonl" }
schedule:
  mode: adaptive
  base: 1800
```

- **Substitution.** `{{ var }}` (whitespace-tolerant) is replaced from the entry's
  map. A string that is *exactly* one placeholder keeps the variable's native type
  (`min_price: "{{ floor }}"` → the integer `500`, not `"500"`); a placeholder
  embedded in a larger string interpolates `str(value)`. An unknown variable is
  left verbatim (a stray `{{ x }}` is not fatal). The `${VAR}` / `${VAR:-default}`
  env syntax is unrelated and still expands first, at file-read time.
- **Stable, distinct ids.** Give `id:` a template that references a per-entry
  variable (`id: search-{{ slug }}`) so each job gets a deterministic id and
  **reloading the file upserts the same N jobs** instead of duplicating them.
  Omit `id:` and ids fall back to `<stem>-<index>` (`marketplace-search-0`, `-1`,
  …). Ids that collide (e.g. a static `id:` with no varying variable) are rejected
  and the file lands in the `failed` list.
- **Composes with `defaults:` / includes.** Matrix expansion runs *after* a
  top-level `defaults:` block and any `include:`/`use:` fragments are resolved, so
  variables substitute into the already-merged result.
- **Additive.** A file with no `matrix:`/`for_each:` key loads to exactly the same
  workflow(s) as before — this feature is opt-in per file.

See `examples/workflows/marketplace-search.yaml` for a runnable matrix template.

## Collect — the container does the job

Each workflow is driven by the same [adaptive engine](../README.md#how-it-works)
as any job: the interval grows while a site is quiet and shrinks when content
changes, smoothed by a global token bucket + per-host concurrency. `adaptive`,
`cron`, and `once` schedule modes all apply. Run a workflow immediately with
`POST /jobs/{id}/run`; pause/resume with `POST /jobs/{id}/pause|resume`.

## Serve — hand out what ujin obtained

Ask for the collected data by workflow id, over REST or WebSocket:

| Endpoint | Returns |
|---|---|
| `GET /jobs/{id}/content` | the **latest** obtained payload (the body/data from the last poll, changed or not), with `ok/changed/fingerprint/ts/status`. `payload` is `null` until the first poll. |
| `GET /jobs/{id}/results?limit=N` | the **recent buffer** — one entry per *changed* poll (`{ts, fingerprint, payload}`), newest first, capped per workflow. |
| `GET /jobs/{id}/runs` | run history (metadata: ok/changed/error/strategy). |
| `GET /jobs/{id}/events` | persisted change events (from a `sqlite` sink). |
| `WS /jobs/events` | live change notices across all workflows. |

```bash
curl -X POST localhost:8902/jobs/crossref-papers/run
curl localhost:8902/jobs/crossref-papers/content     # latest obtained data
curl localhost:8902/jobs/crossref-papers/results     # recent buffer
```

`content` reuses what ujin already fetched (handy when the origin is rate-limited
or anti-bot), mirroring the poller's [`GET /content`](../README.md#http-services).

## Adding capabilities

Workflows are assembled from **capabilities** — the source/transform/sink kinds in
the registry. Two ways to extend the menu:

- **Drop a plugin** into `/plugins` and reference it as `plugin:<name>` — see
  [PLUGINS.md](PLUGINS.md). No rebuild; `POST /plugins/reload` to refresh.
- **Edit ujin in-tree** — add a built-in kind via `@register.source/transform/
  sink` and `pip install -e .`. Sibling projects in active development are
  expected to grow ujin this way; see [CAPABILITIES.md](CAPABILITIES.md).

## Validating a workflows directory without starting the server

Use `ujin plan validate` to load and resolve a workflows directory (or a single
plan file) using the **same loaders** as `jobs-serve` — identical ids, identical
errors — without starting the server:

```bash
ujin plan validate ./workflows/                # human-readable; exit 0 = all ok
ujin plan validate ./workflows/ --json         # machine-readable (CI)
```

Resolved workflows are printed as `ok  <id>`; files that fail to parse land in
`FAIL  <id>: <error>` with an actionable `ujin: …` message. Exit code is 0 when
all workflows resolve and non-zero when any fail.

The `--json` flag emits a single JSON object to stdout:

```json
{
  "ok": true,
  "resolved": ["crossref-papers", "example-page"],
  "failed": []
}
```

A missing or unreadable path exits non-zero with a clean `ujin: …` message (no
traceback). Works equally for an INGEST-PLAN file — `ujin plan validate` accepts
either a file or a directory. See [INGEST_PLAN.md](INGEST_PLAN.md) for details.

## Examples

`examples/workflows/` ships runnable workflow files:
`crossref-papers.yaml` (adaptive API poll), `example-page.yaml` (a minimal HTTP
poll), `site-feeds.yaml` (multiple jobs sharing a `defaults:` block and reusable
`fragments/`), and `marketplace-search.yaml` (a `matrix:` template fanned into three
saved searches). Copy them into your mounted `./workflows` directory to try them.
