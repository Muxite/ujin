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
- A file with a `matrix:`/`for_each:` list fans **one template** out into many
  jobs — see [Fan out](#fan-out--one-template-many-jobs-matrix) below.
- Validation is eager: an unknown source/transform/sink kind lands in the
  `failed` list (see `GET /health`) instead of aborting startup. A file that
  fails to parse or whose `include:` can't be resolved is likewise logged and
  skipped (and surfaced in `failed`) rather than crashing the server.
- `${VAR}` / `${VAR:-default}` in the file are expanded against the process
  environment before parsing — e.g. `url: "${CROSSREF_URL}"` — so a mounted
  workflow file can reference a secret or per-deploy value without committing
  it. See [JOBS.md](JOBS.md) for the full expansion rules.

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

Run it locally without Docker:

```bash
ujin jobs-serve --workflows ./examples/workflows      # or set UJIN_WORKFLOWS_DIR
```

`GET /health` reports what was set up — `loaded` holds the ids that registered
cleanly; a file with an unknown/invalid kind lands in `failed` instead of
aborting startup:

```json
{ "ok": true, "status": "ok", "service": "ujin-jobs", "jobs": 2,
  "workflows": { "dir": "/workflows", "loaded": ["crossref-papers", "example-page"], "failed": [] } }
```

```json
{ "ok": true, "status": "ok", "service": "ujin-jobs", "jobs": 1,
  "workflows": { "dir": "/workflows", "loaded": ["crossref-papers"],
                 "failed": [{"id": "broken-page", "error": "unknown source kind: 'htttp'"}] } }
```

## Fan out — one template, many jobs (matrix)

Polling *N* near-identical sites shouldn't mean *N* near-identical files. Add a
`matrix:` (alias `for_each:`) list of parameter maps and ujin fans **one
template** out into one job per entry, substituting that entry's `{{ vars }}`
into `source`/`transforms`/`sinks`/`schedule`/`name`:

```yaml
# /workflows/status-pages.yaml   ->  status-pages-0, status-pages-1, status-pages-2
matrix:
  - { site: github,    host: "www.githubstatus.com",             every: 300 }
  - { site: gitlab,    host: "status.gitlab.com",                every: 600 }
  - { site: bitbucket, host: "bitbucket.status.atlassian.com",   every: 900 }

# `defaults:` is deep-merged UNDER every entry (per-entry keys win); nested maps
# merge key-wise, so each job inherits `mode`/`max` and overrides only `base`.
defaults:
  schedule: { mode: adaptive, max: 86400 }

# `use:` (alias `include:`) pulls a shared fragment; its keys are deep-merged
# under every fanned-out job. Here it supplies the shared `dedupe` transform.
use: fragments/sqlite-sink.yaml

name: "status-{{ site }}"
source:
  kind: http
  config:
    url: "https://{{ host }}/api/v2/status.json"
    headers:
      User-Agent: "${UJIN_UA:-ujin status watcher}"   # env layer runs *after* matrix
# (transforms come from the included fragment above; inline keys still win over it)
sinks:
  - kind: sqlite
  - kind: jsonl
    config: { path: "/data/status-{{ site }}.jsonl" }
schedule:
  base: "{{ every }}"          # per-entry cadence; `mode`/`max` come from defaults
```

This one file loads three jobs — `status-pages-0` … `status-pages-2` (named
`status-github`, `status-gitlab`, `status-bitbucket`):

```json
{ "ok": true, "jobs": 3,
  "workflows": { "dir": "/workflows",
                 "loaded": ["status-pages-0", "status-pages-1", "status-pages-2"],
                 "failed": [] } }
```

Rules:

- **Cardinality** — a list of *N* maps produces exactly *N* jobs. Ids default to
  `<stem>-<index>`; set `id: "{{ … }}"`/`name: "{{ … }}"` in the template to
  derive your own.
- **Typed substitution** — a value that is *exactly* one placeholder keeps the
  entry's type (`base: "{{ every }}"` with `every: 300` stays the integer `300`);
  a placeholder inside a longer string (`"https://{{ host }}/…"`) is spliced in
  as text.
- **Layer order** — `{{ matrix }}` vars are substituted **before**
  `${ENV}`/`${VAR:-default}`, so the two compose predictably: a matrix value may
  itself carry an env reference (`host: "${REGION:-us-east}.svc"`) that is then
  resolved.
- **Defaults & includes compose** — the file's `defaults:` map and any resolved
  `include:`/`use:` fragments are deep-merged under every fanned-out job, with
  per-entry keys winning (same precedence as a plain workflow).
- **Malformed → skipped, not fatal** — if `matrix:`/`for_each:` isn't a non-empty
  list of maps, or a `{{ var }}` has no value in an entry, that file raises a
  `WorkflowMatrixError`, is skipped, and lands in `GET /health`'s
  `workflows.failed` as `{ "id": <stem>, "file": <name>, "error": <str> }` — the
  other workflow files still load.

A ready-to-run copy ships at
[`examples/workflows/status-pages.yaml`](../examples/workflows/status-pages.yaml).

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
poll), `site-feeds.yaml` — which demonstrates `defaults:` plus an `include:` of
the shared `fragments/sqlite-sink.yaml` fragment — and `status-pages.yaml` (one
`matrix:` template fanned out over three status pages, composing that same
fragment and a top-level `defaults:` block). Copy them into your mounted
`./workflows` directory (fragments and all) to try them.
