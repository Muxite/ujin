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

Each entry in `failed` is `{"id": "<stem>", "error": "<message>"}` — the workflow
file stem and the first error (bad YAML, missing include, unknown kind, …). One bad
file never prevents the others from loading.

## Defaults, fragments, and matrix fan-out

When you run **many similar workflows**, three optional, purely-additive conveniences
keep the files DRY: a `defaults:` block, `include:`/`use:` fragments, and a
`matrix:`/`for_each:` fan-out (they compose in that order). A workflow that uses none
loads byte-for-byte as before, so existing single-file workflows, `{jobs: [...]}`/list
forms, filename-stem ids, and env substitution are unchanged.

### Environment variable substitution

Three `${…}` forms are expanded at file-read time (before YAML parsing),
letting you keep secrets out of the file itself:

| Form | Behaviour |
|------|-----------|
| `${VAR}` | Expands to the value of `VAR`, or `""` (with a log warning) when unset. |
| `${VAR:-default}` | Expands to the value of `VAR`, or `default` when `VAR` is unset. |
| `${VAR:?message}` | Expands to the value of `VAR`; raises an actionable error (naming `VAR` and including `message`) when `VAR` is unset **or** empty. |

Use `${VAR:?message}` for required configuration — the workflow file lands in
the `failed` list (reported by `GET /health` and `ujin plan validate`) instead
of loading with a silently empty value:

```yaml
# /workflows/feeds.yaml
source:
  kind: http
  config:
    url: "https://api.example.com/"
    headers:
      Authorization: "Bearer ${API_TOKEN:?set API_TOKEN to your ingest token}"
```

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
  left verbatim (a stray `{{ x }}` is not fatal). The env substitution syntax
  (`${VAR}`, `${VAR:-default}`, `${VAR:?message}`) is unrelated and still
  expands first, at file-read time — see the table below.
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

#### File-backed matrix — `matrix: { from: <path> }`

Instead of an inline list, the matrix value may be a **`{ from: <path> }`** mapping
naming an external **data file** that supplies the list of variable maps. This keeps
the set of rows (the saved searches, the regions, …) in its own file so it can
change without editing the job definition — or be mounted / version-controlled
separately:

```yaml
# /workflows/marketplace-search.yaml
matrix:
  from: data/marketplace-searches.yaml   # a list of variable maps, one per job

id: "search-{{ slug }}"
source:
  kind: api
  config: { url: "https://api.example.com/search?q={{ query }}&min_price={{ floor }}" }
```

```yaml
# /workflows/data/marketplace-searches.yaml — a NON-EMPTY top-level list of maps
- { slug: laptop,  query: "gaming laptop", floor: 500 }
- { slug: gpu,     query: "rtx 4090",      floor: 800 }
- { slug: monitor, query: "4k monitor",    floor: 200 }
```

- **Path resolution.** The `from:` path resolves exactly like an `include:`/`use:`
  fragment — relative to the **containing file's directory**, then
  `$UJIN_WORKFLOWS_DIR`. Keep data files in a **subdirectory** (e.g. `data/`) so the
  non-recursive startup scan never loads them as standalone workflows.
- **Equivalent to inlining.** The loaded rows drive the same fan-out, `{{ var }}`
  substitution, and stable-id rules as an inline list — `matrix: { from: rows.yaml }`
  produces byte-for-byte the same jobs as pasting `rows.yaml`'s contents under
  `matrix:`. `${VAR}` is expanded in the data file at read time, as everywhere else.
- **Errors are contained.** A missing/unreadable data file, or one that is not a
  non-empty list of variable maps, fails just that workflow with an actionable error
  in the `failed` list (`GET /health`); other workflows still load.

See `examples/matrix-from-file.yaml` (+ `examples/data/marketplace-searches.yaml`)
for a runnable file-backed matrix.

#### Cross-product matrix — axes fanned into combinations

Instead of an explicit list of rows, the matrix value may be a **mapping of
axis-name → list-of-values**. ujin fans it into the **cartesian product** — one
workflow per *combination* of axis values. Two axes of sizes M and N produce M·N
jobs:

```yaml
# /workflows/region-status.yaml
matrix:
  region: [us-east, us-west, eu-central]   # 3 values
  tier:   [free, pro]                      # 2 values   -> 3 * 2 = 6 jobs

id: "status-{{ region }}-{{ tier }}"
source:
  kind: api
  config: { url: "https://api.example.com/{{ region }}/status?tier={{ tier }}" }
```

This resolves to six jobs — `status-us-east-free`, `status-us-east-pro`,
`status-us-west-free`, `status-us-west-pro`, `status-eu-central-free`,
`status-eu-central-pro` — each with both `{{ region }}` and `{{ tier }}` substituted.

- **Disambiguation.** A matrix mapping is read as cross-product axes **unless it
  carries a `from:` key**, which selects the file-backed form above. `from` is
  therefore reserved — you cannot name an axis `from`.
- **Stable, order-independent ids.** Omit `id:` and each combination falls back to
  `<stem>-<value>-<value>…` built with the **axis names sorted** and each axis's
  values kept in the order written — so reordering the axes in the file leaves every
  combination with the same id, and repeated `ujin plan render` runs are identical.
  An explicit `id:` template referencing the axis variables (as above) gives friendly
  ids. Values are slugified into the id (`4k monitor` → `4k-monitor`); two values
  that collapse to the same token are rejected as a duplicate id.
- **Same substitution + composition.** `{{ var }}` substitution, whole-value type
  preservation, and the `defaults:`/`include:` composition order are identical to the
  inline form.
- **Errors are contained.** An empty axes mapping, an empty or non-list axis value,
  or a colliding generated id fails *that* workflow with an actionable error in the
  `failed` list (`GET /health`); other workflows still load.

See `examples/matrix-cross-product.yaml` for a runnable cross-product plan.

#### Pruning and extending — `exclude:` / `include:`

When the matrix value is a **mapping** (cross-product axes or `{ from: <path> }`) it
may carry two optional, strictly-additive filter keys — `exclude:` and `include:` —
that refine the generated fan-out. They are reserved keys, so you cannot name an axis
`exclude` or `include`.

```yaml
# /workflows/region-status.yaml
matrix:
  region: [us-east, us-west, eu-central]   # 3 values
  tier:   [free, pro]                      # 2 values   -> 6 base combinations
  exclude:
    - { region: eu-central, tier: pro }    # drop this ONE combination -> 5 jobs
  include:
    - { region: ap-south, tier: canary }   # append one explicit extra -> 6 jobs

id: "status-{{ region }}-{{ tier }}"
source:
  kind: api
  config: { url: "https://api.example.com/{{ region }}/status?tier={{ tier }}" }
```

- **`exclude:`** is a list of **partial** variable maps. A generated row is dropped
  when it agrees with an exclude entry on **every key that entry specifies** — so the
  full `{ region: eu-central, tier: pro }` above removes a single combination, while a
  partial `{ tier: pro }` would remove **every** `*-pro` combination. An entry that
  names a key no row has (or a value none holds) matches nothing and is a harmless
  no-op. Exclude runs over the whole fan-out, so it prunes a cross-product, a
  `{ from: <path> }` list, or an inline list alike.
- **`include:`** is a list of **full** variable maps appended to the fan-out — extra
  jobs the axes (or data file) don't generate, substituted exactly like any other row.
- **Pruning an inline list.** A bare `matrix: [ … ]` list cannot carry sibling keys,
  so to pair an explicit list with `exclude:` write the rows under `include:` in a
  mapping form — `matrix: { include: [ … ], exclude: [ … ] }` resolves to the same
  jobs as the inline list, minus the excluded rows.
- **Stable ids.** Surviving cross-product/file rows keep their usual slug/index
  suffixes; appended `include:` rows are index-suffixed. As always, give `id:` a
  template referencing the per-row variables so each job gets a deterministic id and
  reloads upsert rather than duplicate (especially when combining `include:` with a
  base form, whose default `<stem>-<index>` ids can otherwise collide).
- **Errors are contained.** A malformed `exclude:`/`include:` (not a list of variable
  maps), or filters that prune the fan-out to zero jobs, fail *that* workflow with an
  actionable `ujin: …` error in the `failed` list (`GET /health`); other workflows
  still load.
- **Additive.** A matrix block with neither key resolves byte-for-byte as before.

See `examples/matrix-exclude.yaml` for a runnable plan that prunes with `exclude:`
and extends with `include:`.

### `vars:` — default values for `{{ var }}` placeholders

A top-level **`vars:`** mapping (variable name → default value) supplies a fallback
for any `{{ var }}` placeholder that nothing else sets. It keeps a workflow runnable
with zero configuration while letting a deploy override a single value from the
environment — without editing the file:

```yaml
# /workflows/regional-feed.yaml
vars:
  region: us-east          # fallback for every {{ region }} left unset
  rows: 50                 # an exact-placeholder value keeps its int type

id: "feed-{{ region }}"
source:
  kind: api
  config:
    url: "https://feeds.example.com/{{ region }}?rows={{ rows }}"
    json_path: items
```

Run as-is and the job queries `us-east`; set the environment variable `region` to
`eu-west` (e.g. in the container) and it switches with no file edit.

- **Precedence.** For a `{{ NAME }}` placeholder: a `matrix:`/`for_each:` per-entry
  value wins, then the process environment (variable `NAME`, if set), then the
  file-level `vars: NAME` default; an otherwise-unset placeholder is still left
  verbatim. So a `vars:` default only fills a variable nothing else provides, and a
  matrix entry that sets the same name overrides it per job.
- **Whole-value types preserved.** A string that is *exactly* one placeholder keeps
  the default's native type (`rows: "{{ rows }}"` → the integer `50`), exactly like
  matrix substitution; an embedded placeholder interpolates `str(value)`.
- **Unrelated to `${VAR}`.** The `${VAR}` / `${VAR:-default}` read-time env syntax
  (the table above) is independent and still expands first, at file-read time — even
  if a `vars:` entry happens to share the name; `vars:` feeds the later `{{ var }}`
  pass only.
- **Additive.** A workflow with no `vars:` key (or an empty one) resolves
  byte-for-byte as before, and `ujin plan validate` rejects a non-mapping `vars:`
  with a clean `ujin: …` error.

See `examples/vars-defaults.yaml` for a runnable `vars:` example (a matrix job whose
`region`/`rows` fall back to the defaults while a per-entry var would override them).

### `profiles:` — named overlays over `defaults:`

A top-level **`profiles:`** mapping (profile name → overlay mapping) declares named
variations of a file's base `defaults:`. Selecting one with `--profile NAME` (or
`$UJIN_PLAN_PROFILE`; the flag wins) **deep-merges that overlay over `defaults:`**
before the per-job merge, so every job inherits the overlaid value unless it pins
the key itself. Per-job keys still win, and `matrix:`/`for_each:` fan-out,
`${VAR}`, and `{{ var }}` substitution behave exactly as without a profile:

```yaml
# /workflows/feeds.yaml
defaults:
  schedule: { mode: poll, base: 60 }
profiles:
  fast:
    schedule: { base: 5 }    # tighten every non-overriding job's poll to 5s
  slow:
    schedule: { base: 600 }  # back off to 10 minutes
jobs:
  - name: home
    source: { kind: http, config: { url: "https://example.com/" } }
  - name: status
    schedule: { mode: poll, base: 9 }   # pins its own cadence; profile is ignored
    source: { kind: http, config: { url: "https://example.com/status" } }
```

```bash
ujin plan render  ./workflows/ --profile fast    # home polls every 5s; status keeps 9s
UJIN_PLAN_PROFILE=slow ujin jobs-serve           # env fallback (a flag would override)
```

- **Merge order.** profile overlay over `defaults:` → that result deep-merged under
  each job. Lists replace wholesale; nested mappings merge key-by-key. Per-job keys
  always win over the profile, so the established precedence
  (per-job > profile > `defaults:`) holds.
- **Leniency in a directory.** A workflow file with no `profiles:` key is left
  unchanged regardless of the selected profile, so you can roll out
  `profiles:` one file at a time. Only a file that *declares* `profiles:` but
  *omits* the selected name exits non-zero with a clean `ujin: …` message; other
  files in the directory still load.
- **Unknown name.** If every file in the directory omits `profiles:`, selecting any
  profile name is a harmless no-op (no file fails). When at least one file declares
  `profiles:` and the selected name is absent from that file, that file lands in the
  `failed` list with an actionable `ujin: …` message listing the names it recognises.
- **Additive.** A file with no `profiles:` key, and with no profile selected,
  resolves byte-for-byte as before.

See `examples/profiles.yaml` for a runnable `profiles:` example, and
[INGEST_PLAN.md](INGEST_PLAN.md#profiles--named-overlays-over-defaults) for the
full reference including selection flags, env fallback, and precedence rules.

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
  "failed": [],
  "warnings": [],
  "strict": false
}
```

A missing or unreadable path exits non-zero with a clean `ujin: …` message (no
traceback). Works equally for an INGEST-PLAN file — `ujin plan validate` accepts
either a file or a directory. See [INGEST_PLAN.md](INGEST_PLAN.md) for details.

### Selecting a subset with `--only` / `--exclude`

Both `plan validate` and `plan render` accept `--only PATTERN` and `--exclude PATTERN`
(fnmatch-style globs, comma-separated or repeatable) to filter which workflows are
processed. Patterns match the **fully-expanded job id** — after `matrix:`/`for_each:`
fan-out — so they work on individual expanded ids, not just file stems:

```bash
ujin plan validate ./workflows/ --only 'search-*'             # ids starting with "search-"
ujin plan validate ./workflows/ --only 'search-*' --exclude search-old
ujin plan render   ./workflows/ --only 'search-laptop,search-gpu'
ujin plan render   ./workflows/ --only 'search-*' --json
```

`--exclude` subtracts from the `--only` set; when both are omitted all workflows are
processed unchanged. A `--only` pattern that matches no resolved id exits non-zero
with a clean `ujin: …` message. `jobs-serve --plan` accepts the same flags and
reflects only the selected jobs in `GET /health`; see
[INGEST_PLAN.md](INGEST_PLAN.md#job-id-selection----only-and---exclude).

### Validation & common mistakes — misspelled keys

After `include:`/`defaults:` resolution the loader checks each workflow against the
recognized keys — `source`, `transforms`, `sinks`, `schedule`, `id`, `name`, `enabled`
plus the additive `defaults:`, `matrix:`/`for_each:`, and `include:`/`use:` — and a
`jobs:` list for the multi-job form. A key outside that set — a typo such as
`shedule:` for `schedule:` or `sinkz:` for `sinks:` — would otherwise be **silently
ignored** (the workflow loads with that setting missing). `ujin plan validate` surfaces
it as an advisory `WARN` line naming the file, the job id when applicable, and the
unknown key, with a `did you mean …?` hint:

```
ok  site-feeds-0
WARN  ujin: workflow site-feeds.yaml job site-feeds-0: unrecognized key 'shedule'; did you mean 'schedule'?
```

Warnings are advisory by default — the workflow still loads and the exit code is
unaffected, so the change is additive. Pass `--strict` to promote them to failures
(`FAIL`, non-zero exit) for a CI gate; with `--json` they appear in a `warnings` array.

## Inspecting fully-resolved jobs

Use `ujin plan render` to print every fully-expanded `JobSpec` — after
`defaults:` merge, fragment resolution, `matrix:` fan-out, and variable
substitution — in human-readable YAML, or as a JSON array with `--json`:

```bash
ujin plan render ./workflows/                  # YAML, one document per job
ujin plan render ./workflows/ --json           # JSON array (same shape as GET /jobs)
```

This is useful for verifying that matrix templates expand as expected and that
defaults are merged the way you intended before committing to a deploy. Each YAML
document is prefixed with `--- # <id>` so you can grep for a specific job id. The
`--json` output uses the same spec shape as the REST `/jobs` endpoint. See
[INGEST_PLAN.md](INGEST_PLAN.md) for full details and examples.

Add `--explain` to annotate each job with its provenance — the workflow **file** it
came from and, for a `matrix:`/`for_each:`-expanded job, the matrix entry index and
variable map that produced it. Provenance is collected only under `--explain`, so the
default render is byte-for-byte unchanged:

```bash
ujin plan render ./workflows/ --explain          # YAML with `# source:` / `# matrix:` comments
ujin plan render ./workflows/ --explain --json   # each element pairs spec + provenance
```

```
--- # search-gpu
# source: workflows/marketplace-search.yaml
# matrix: entry 1 -> {slug: gpu, query: rtx 4090, floor: 800}
name: search-gpu
...
```

`--only`/`--exclude` also apply to `plan render` — see [above](#selecting-a-subset-with---only----exclude).

## Diffing two workflow directories — `plan diff`

Use `ujin plan diff OLD NEW` to compare two snapshots of a workflows directory
(or a plan file) before you deploy a change. Both sides resolve through the same
loaders as `plan validate`/`plan render`, then are compared keyed by **final job id**:

```bash
ujin plan diff ./workflows/ ./workflows-next/               # human-readable
ujin plan diff ./workflows/ ./workflows-next/ --json        # machine-readable (CI)
ujin plan diff ./workflows/ ./workflows-next/ --exit-code   # exit non-zero if any diff
ujin plan diff ./workflows/ ./workflows-next/ --check       # alias for --exit-code
```

The human form marks each affected job with `+` (added in `NEW`), `-` (removed), or
`~` (changed — same id, different resolved spec), followed by a summary count on
stderr. An identical pair prints `no changes`.

**Exit-status behavior:** `plan diff` exits **zero** whenever both inputs resolve
successfully — even when differences exist. Pass `--exit-code` (alias `--check`) to
flip this for CI: the command exits **non-zero** when any jobs differ, zero when the
plans are identical. `--exit-code` does not affect `--json` output or the diff content
itself; the flag only changes the exit code.

`--json` emits a structured object with distinct `added`/`removed`/`changed`
groupings; added and removed entries carry the job's resolved `spec`, changed entries
carry both `old` and `new`:

```bash
ujin plan diff ./workflows/ ./workflows-next/ --json | python3 -c \
  "import sys,json; d=json.load(sys.stdin); \
   print(len(d['added']),'added', len(d['removed']),'removed', len(d['changed']),'changed')"
```

`--env-file` is accepted consistently with the other plan subcommands. An unreadable
or schema-invalid input exits non-zero with a clean `ujin: …` message regardless of
`--exit-code`. See [INGEST_PLAN.md](INGEST_PLAN.md#diffing-two-plans--plan-diff) for
the full reference including the `--json` response shape.

## Auto-reloading the plan file (`--watch-plan`)

When deploying via an INGEST-PLAN file (`--plan`), you can have `jobs-serve` watch
the file and reload it automatically on every save — no restart, no manual
`POST /plan/reload`:

```bash
ujin jobs-serve --plan ./ingest-plan.yaml --watch-plan           # default poll: 2.0 s
ujin jobs-serve --plan ./ingest-plan.yaml --watch-plan --watch-interval 5
# or via env:
UJIN_WATCH_PLAN=1 UJIN_WATCH_INTERVAL=5 ujin jobs-serve --plan ./ingest-plan.yaml
```

On a detected change the file is re-resolved through the full loader
(`defaults:`/`include:`/`matrix:`/`${VAR}`) and the resulting jobs are upserted and
pruned against the running set — equivalent to `POST /plan/reload?prune=true` fired
automatically. A malformed save is skipped (jobs keep running; the error surfaces in
`GET /health`). This flag is specific to the `--plan` file; the workflows directory
(`--workflows`) does not have an equivalent watcher and requires a restart to pick up
new files. See
[INGEST_PLAN.md](INGEST_PLAN.md#auto-reloading-on-file-change---watch-plan).

## Examples

`examples/workflows/` ships runnable workflow files:
`crossref-papers.yaml` (adaptive API poll), `example-page.yaml` (a minimal HTTP
poll), `site-feeds.yaml` (multiple jobs sharing a `defaults:` block and reusable
`fragments/`), and `marketplace-search.yaml` (a `matrix:` template fanned into three
saved searches). Copy them into your mounted `./workflows` directory to try them.
