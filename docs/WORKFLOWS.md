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
  `failed` list (see `GET /health`) instead of aborting startup. A file that
  fails to parse or whose `include:` can't be resolved is likewise logged and
  skipped (and surfaced in `failed`) rather than crashing the server.

## Shared defaults (`defaults:`)

A top-level `defaults:` block is **deep-merged into every job** in the file, with
per-job keys winning. Nested maps (`source`, `transforms`, `sinks`, `schedule`)
merge key-by-key — a job can override just `schedule.base` and inherit the rest.
Lists (e.g. `sinks`) are replaced wholesale, not concatenated.

```yaml
# /workflows/site-feeds.yaml   ->  workflows "site-feeds-0", "site-feeds-1"
defaults:
  schedule: { mode: adaptive, base: 300, min: 60, max: 3600 }
jobs:
  - source: { kind: rss, config: { url: "https://hnrss.org/frontpage" } }
  - source: { kind: rss, config: { url: "https://lobste.rs/rss" } }
    schedule: { base: 600 }      # overrides only base; mode/min/max inherited
```

## Reusable fragments (`include:` / `use:`)

`include:` (alias `use:`) pulls a **reusable fragment file** — a partial job
mapping (any subset of `source`/`transforms`/`sinks`/`schedule`) — into a job.
It works both at the **file level** (applied to every job) and **per job**, and
takes a single path or a list of paths:

```yaml
include: fragments/sqlite-sink.yaml         # file-level: all jobs get it
jobs:
  - source: { kind: rss, config: { url: "https://hnrss.org/frontpage" } }
  - source: { kind: rss, config: { url: "https://lobste.rs/rss" } }
    use: fragments/extra-transforms.yaml     # per-job: only this job
```

```yaml
# /workflows/fragments/sqlite-sink.yaml   (a partial job — not a workflow itself)
transforms:
  - kind: dedupe
    config: { key: link }
sinks:
  - kind: sqlite
```

Keep fragments in a **subdirectory** (e.g. `fragments/`) so the workflows-dir
glob (`*.y*ml`, non-recursive) doesn't load them as standalone workflows.

**Path resolution.** A relative fragment path is resolved against
`$UJIN_WORKFLOWS_DIR` **first**, then the **including file's own directory**; the
first existing file wins. An absolute path is used as-is. This means a fragment in
the mounted workflows root is reachable from any file, and a fragment next to a
file is reachable without configuring the env var.

**Nested includes.** A fragment may itself `include:` another fragment (resolved
relative to that fragment's location); nesting is unbounded but **cycle-guarded** —
a fragment that (transitively) includes itself raises a clear `WorkflowIncludeError`.

**Precedence** (lowest → highest):

```
defaults:  <  file-level include  <  per-job include  <  inline job keys
```

So a job's own keys always win, an included fragment overrides the file defaults,
and a per-job include overrides a file-level one. A missing fragment, a cyclic
include, or a malformed directive raises `WorkflowIncludeError` — fatal for an
explicit `--config` file, logged-and-skipped for a workflows-directory file.

## Fan out with a matrix

A `matrix:` (alias `for_each:`) key turns **one template into one job per entry**.
Each entry is a map of variables substituted into the whole tree
(source/transforms/sinks/schedule) with the usual `${VAR}` / `${VAR:-default}`
syntax. Every generated job gets a distinct, deterministic id derived from the
template id + the entry, so re-loading the file **upserts the same jobs**:

```yaml
# /workflows/status.yaml
matrix:
  - NAME: alpha
    HOST: alpha.example.com
    INTERVAL: 2
  - NAME: beta
    HOST: beta.example.com          # INTERVAL omitted -> ${INTERVAL:-5}
source:
  kind: http
  config: { url: "https://${HOST}/status" }
sinks:
  - kind: jsonl
    config: { path: "./out/status-${NAME}.jsonl" }
schedule:
  mode: cron
  cron: "*/${INTERVAL:-5} * * * *"
```

This loads two workflows, `status-alpha` and `status-beta`.

### A mapping of axes (cartesian product)

Instead of a list of rows, `matrix:`/`for_each:` may be a **mapping of axes** —
each key names a variable and maps to a list of values. The template fans out over
the cartesian product of the axes (first axis varies slowest, last fastest):

```yaml
# /workflows/regions.yaml
id: watch-${REGION}-${ENV}
for_each:
  REGION: [us, eu]
  ENV: [prod, dev]
source:
  kind: http
  config: { url: "https://${REGION}.${ENV}.example.com/status" }
```

This loads four workflows: `watch-us-prod`, `watch-us-dev`, `watch-eu-prod`,
`watch-eu-dev`.

Notes:

- Ids default to `<template-id>-<slug-of-entry>`. The slug prefers a conventional
  identity variable (`id`/`name`, case-insensitive — e.g. `NAME`), else a slug of
  all the entry's values; distinct entries whose slugs collide are disambiguated
  by index so ids stay distinct.
- Set an explicit `id:` with a var to steer it (`id: svc-${NAME}` ->
  `svc-alpha`, `svc-beta`).
- **Type is preserved for whole-scalar references.** A value that is *exactly*
  `${VAR}` keeps the matrix value's native type — `base: ${INTERVAL}` with
  `INTERVAL: 300` stays the integer `300`. Partial interpolation
  (`.../${NAME}/x`) is always a string.
- `for_each:` is an exact alias for `matrix:`.
- A malformed matrix (neither a list nor a mapping of axes, an entry that isn't a
  variable map, or an axis whose value isn't a list) fails with a
  `WorkflowMatrixError` naming the template; it lands in the `failed` list (see
  `GET /health`) instead of aborting startup — valid workflows still load.
- A **duplicate generated id** — two entries carrying the same explicit `id`/`name`
  identity (or a var-driven `id:` template that substitutes to the same string) —
  is likewise a `WorkflowMatrixError` naming the template and the offending id, and
  lands in `failed` without preventing other files from loading. (A collision that
  is only a slugification artifact of *distinct* values is disambiguated silently,
  not rejected.)
- Matrix fan-out composes **after** `defaults:`/`include:`/`use:` resolution, so a
  matrix template inherits shared defaults and fragments — see below. Env-only
  workflows with no `matrix:`/`for_each:` key load exactly as before.

Both `defaults:` and `include:`/`use:` (above) apply to a matrix template exactly
as they do to a plain job — they're resolved once per template, **before** the
matrix fans out, so every generated job inherits the shared settings:

```yaml
defaults:
  source:
    kind: http
    config: { timeout: 30 }
  schedule: { mode: once }
jobs:
  - id: watch-${NAME}
    matrix:
      - { NAME: alpha, URL: https://a.example.com }
      - { NAME: beta,  URL: https://b.example.com }
    source:
      config: { url: ${URL} }        # merged with defaults.source.config.timeout
```

Both expanded jobs (`watch-alpha`, `watch-beta`) get `source.kind: http`,
`source.config.timeout: 30`, and `schedule.mode: once` from `defaults`, with each
entry's `url` layered on top. `defaults:` also merges into a single top-level
`matrix:` template that has no wrapping `jobs:` list (the one-file matrix form).
See [`examples/workflows/matrix-defaults.yaml`](../examples/workflows/matrix-defaults.yaml)
for a runnable matrix + defaults example, and
[`examples/workflows/matrix-status.yaml`](../examples/workflows/matrix-status.yaml)
for a plain matrix fan-out.

Run it locally without Docker:

```bash
ujin jobs-serve --workflows ./examples/workflows      # or set UJIN_WORKFLOWS_DIR
```

`GET /health` reports what was set up:

```json
{ "ok": true, "jobs": 2,
  "workflows": { "dir": "/workflows", "loaded": ["crossref-papers", "example-page"], "failed": [] } }
```

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

## Examples

`examples/workflows/` ships runnable workflow files:
`crossref-papers.yaml` (adaptive API poll), `example-page.yaml` (a minimal HTTP
poll), and `site-feeds.yaml` — which demonstrates `defaults:` plus an `include:`
of the shared `fragments/sqlite-sink.yaml` fragment. Copy them into your mounted
`./workflows` directory (fragments and all) to try them.

## Validating a workflow

Check files before starting the server (requires the `yaml` extra):

```sh
ujin validate examples/workflows examples/jobs.crossref.yaml
ujin validate my-workflow.yaml --plugins ./plugins --format json
ujin validate --print-schema
```

Validation exits 0 when every file is valid, or 1 with `path:line: message`
diagnostics. JSON output is a list of `{path, line, message}` objects (empty on
success). Directories use the server's top-level YAML glob; fragments in
subdirectories are loaded through includes. Defaults, includes, templates and
matrix expansion use the server loaders. Files declaring named `templates` or
`fragments` use ingest-plan semantics; other files use workflow semantics.
Validation checks resolved job structure, schedule modes and registered source,
transform and sink kinds without building or running their factories. It does
not test URLs, credentials or factory-specific configuration. `--plugins`
imports the directory's Python modules to register their kinds, as at startup.

For editor autocomplete, associate YAML files with
[`schema/workflow.schema.json`](schema/workflow.schema.json), for example with a
YAML language server modeline (adjust the relative path for your file):

```yaml
# yaml-language-server: $schema=../../docs/schema/workflow.schema.json
```

The schema permits partial job entries because defaults and fragments can
supply required fields; the CLI checks those fields after resolution. Diagnostic
lines use original YAML marks, with best-effort lookup for inherited or expanded
values. Regenerate the committed schema with
`ujin validate --print-schema > docs/schema/workflow.schema.json`.
