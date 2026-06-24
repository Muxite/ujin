# ujin INGEST-PLAN — many jobs from one file

An **INGEST-PLAN** is a single YAML/JSON file that declares **many jobs at once**.
It's the file-driven counterpart to the [workflows](WORKFLOWS.md) directory for
when you'd rather mount *one file* than a folder: same `source → transforms →
sinks → schedule` job shape, same engine, same durability, and the same additive
conveniences (`defaults:`, `include:`/`use:`, `matrix:`/`for_each:`).

A plan is **strictly additive**: one that uses none of `defaults`/`include`/
`matrix` loads to exactly the jobs of the equivalent plain list, and a deploy with
no plan configured behaves byte-for-byte as before.

## Pointing ujin at a plan

Resolve the plan from an env var or a CLI flag (the flag wins):

```bash
# env var (mountable as a volume in a container)
UJIN_INGEST_PLAN=/data/ingest-plan.yaml ujin jobs-serve

# --plan flag — overrides $UJIN_INGEST_PLAN
ujin jobs-serve --plan ./ingest-plan.yaml
```

The plan's jobs are created on startup and are visible like any other job — via
`GET /jobs`, `GET /jobs/{id}`, and reflected in `GET /health` (see
[Reporting](#reporting-and-errors)).

### `--env-file PATH` — inject secrets without committing them

All three plan commands accept `--env-file PATH` to load `KEY=VALUE` pairs from a
dotenv-style file and feed them into `${VAR}` / `${VAR:-default}` / `${VAR:?message}`
substitution when the plan is resolved:

```bash
# .env file (keep out of source control)
# API_TOKEN=secret123
# BACKEND_URL=https://api.example.com

ujin plan validate ./ingest-plan.yaml --env-file .env
ujin plan render   ./ingest-plan.yaml --env-file .env
ujin jobs-serve    --plan ./ingest-plan.yaml --env-file .env
```

**Precedence rule: process environment variables always win.** The file only supplies
values for variables that are *not* already set in the environment, so exporting a
variable before running the command overrides whatever is in the file:

```bash
API_TOKEN=override ujin plan render ./ingest-plan.yaml --env-file .env
# → API_TOKEN resolves to "override", not the value from .env
```

**File format:** one `KEY=VALUE` per line; blank lines and lines starting with `#`
are skipped; keys must be valid identifiers (`[A-Za-z_][A-Za-z0-9_]*`). A missing
or unparseable file exits immediately with a clean `ujin: …` message and no
traceback.

## Schema

The top level is **either** a list of job mappings **or** a mapping with a
`jobs:` list plus optional `defaults:` and `vars:` mappings:

```yaml
# list form — the simplest plan
- id: papers
  source: { kind: api, config: { url: "https://api.example.com/works" } }
- id: prices
  source: { kind: api, config: { url: "https://api.example.com/prices" } }
```

```yaml
# mapping form — adds shared `defaults:`
defaults:
  source: { kind: api, config: { method: GET, json_path: items } }
  sinks:
    - kind: sqlite
  schedule: { mode: adaptive, base: 3600 }
jobs:
  - id: papers
    source: { config: { url: "https://api.example.com/works" } }
  - id: prices
    source: { config: { url: "https://api.example.com/prices" } }
```

Each job is the same declarative shape `JobSpec.from_dict` accepts for the
[jobs API](JOBS.md) and [workflow files](WORKFLOWS.md):
`source` / `transforms` / `sinks` / `schedule`, with environment substitution
expanded from the environment at load time. Three forms are supported:

| Form | Behaviour |
|------|-----------|
| `${VAR}` | Expands to the value of `VAR`, or `""` (with a log warning) when unset. |
| `${VAR:-default}` | Expands to the value of `VAR`, or `default` when `VAR` is unset. |
| `${VAR:?message}` | Expands to the value of `VAR`; raises an actionable error (naming `VAR` and including `message`) when `VAR` is unset **or** empty. |

Use `${VAR:?message}` for secrets or required configuration that must be
present at load time — the plan (or workflow) lands in the `failed` list
reported by `GET /health` and `ujin plan validate` rather than loading with
a silently empty value:

```yaml
source:
  kind: http
  config:
    url: "https://api.example.com/"
    headers:
      Authorization: "Bearer ${API_TOKEN:?set API_TOKEN to your ingest token}"
```

### `defaults:` — shared keys, deep-merged under every job

A top-level `defaults:` mapping is **deep-merged under each job** before the job's
own keys are applied, so per-job keys win. Nested mappings (`source`,
`schedule`, …) merge key-by-key; lists (`transforms`, `sinks`) replace. A plan
without `defaults:` skips this step entirely. This is the same merge used by
workflow files.

### `include:` / `use:` — reference a fragment file

Any mapping (or list item) may carry an `include:` (alias `use:`) key naming a
fragment file to splice in. Fragment paths resolve **relative to the plan's
directory first, then `$UJIN_WORKFLOWS_DIR`** — exactly as for workflow files — so
the same fragments work from either. Use it for a shared sink, a schedule, or a
transform pipeline:

```yaml
defaults:
  sinks:
    - include: fragments/webhook-sink.yaml   # defined once, reused by every job
  schedule:
    include: fragments/adaptive-hourly.yaml
jobs:
  - id: papers
    source: { kind: api, config: { url: "https://api.example.com/works" } }
```

### `matrix:` / `for_each:` — fan one entry into many jobs

A job entry carrying `matrix:` (alias `for_each:`), a list of variable maps, is
fanned into **one job per map**, with each map's variables substituted into every
`{{ var }}` placeholder across the source, transforms, sinks, and schedule:

```yaml
jobs:
  - id: "feed-{{ slug }}"
    matrix:
      - { slug: tech }
      - { slug: science }
    source:
      kind: api
      config: { url: "https://feeds.example.com/{{ slug }}?rows=50" }
    sinks:
      - kind: jsonl
        config: { path: "/data/{{ slug }}.jsonl" }
```

A whole-value placeholder keeps the variable's native type
(`min_price: "{{ floor }}"` → the integer `500`); an embedded one interpolates
`str(value)`; an unknown variable is left verbatim. `defaults:` are merged in
*before* matrix runs, so a `{{ var }}` placeholder living in a shared default
resolves per entry.

The matrix value may also be a **`{ from: <path> }`** mapping naming an external
**data file** that supplies the list of variable maps, instead of an inline list:

```yaml
jobs:
  - id: "search-{{ slug }}"
    matrix:
      from: data/marketplace-searches.yaml   # a non-empty list of variable maps
    source:
      kind: api
      config: { url: "https://api.example.com/search?q={{ query }}" }
```

The `from:` path resolves exactly the way an `include:`/`use:` fragment does —
relative to **this plan's directory**, then `$UJIN_WORKFLOWS_DIR` — and the loaded
rows drive the same fan-out, substitution, and stable-id rules as an inline list, so
`matrix: { from: rows.yaml }` produces byte-for-byte the same jobs as inlining
`rows.yaml`'s contents. A missing/unreadable data file, or one that is not a
non-empty list of variable maps, fails *that* job (see [errors](#reporting-and-errors))
rather than aborting the plan. The shipped `examples/matrix-from-file.yaml`
(+ `examples/data/marketplace-searches.yaml`) is a runnable file-backed plan.

The matrix value may instead be a **mapping of axis-name → list-of-values**, fanned
into the **cartesian product** — one job per *combination* of axis values. Two axes
of sizes M and N produce M·N jobs:

```yaml
jobs:
  - id: "status-{{ region }}-{{ tier }}"
    matrix:
      region: [us-east, us-west, eu-central]   # 3 values
      tier:   [free, pro]                       # 2 values   -> 6 jobs
    source:
      kind: api
      config: { url: "https://api.example.com/{{ region }}/status?tier={{ tier }}" }
```

A matrix mapping is read as cross-product axes **unless it carries a `from:` key**
(which selects the file-backed form), so `from` is reserved as an axis name. Each
combination gets a stable id derived from its values — with the axis names sorted, so
it is independent of the order the axes are written and identical across repeated
`ujin plan render` runs (see [Stable ids](#stable-ids)). An empty axes mapping, an
empty or non-list axis value, or a colliding generated id fails *that* job (see
[errors](#reporting-and-errors)) without aborting the rest of the plan. The shipped
`examples/matrix-cross-product.yaml` is a runnable cross-product plan.

When the matrix value is a **mapping** it may also carry two optional, strictly-additive
filter keys (reserved, so never axis names):

```yaml
jobs:
  - id: "status-{{ region }}-{{ tier }}"
    matrix:
      region: [us-east, us-west, eu-central]
      tier:   [free, pro]
      exclude:
        - { region: eu-central, tier: pro }    # drop one combination -> 5 jobs
      include:
        - { region: ap-south, tier: canary }   # append one extra job  -> 6 jobs
    source:
      kind: api
      config: { url: "https://api.example.com/{{ region }}/status?tier={{ tier }}" }
```

- **`exclude:`** is a list of **partial** variable maps; a generated row is dropped when
  it agrees with an entry on **every key that entry specifies** (`{ tier: pro }` drops
  every `*-pro` job, `{ region: eu-central, tier: pro }` drops just one). An entry naming
  a key no row has matches nothing. Exclude runs over the whole fan-out — cross-product,
  `{ from: <path> }` list, or inline list alike.
- **`include:`** is a list of **full** variable maps appended to the fan-out, substituted
  like any other row. To pair an explicit inline list with `exclude:`, write the rows
  under `include:` in a mapping (`matrix: { include: [ … ], exclude: [ … ] }`).
- A malformed `exclude:`/`include:` (not a list of variable maps), or filters that leave
  zero jobs, fail *that* job (see [errors](#reporting-and-errors)) without aborting the
  plan. A matrix block with neither key resolves byte-for-byte as before. The shipped
  `examples/matrix-exclude.yaml` is a runnable pruned-and-extended plan.

### `vars:` — default values for `{{ var }}` placeholders

A top-level **`vars:`** mapping (variable name → default value) backfills any
`{{ var }}` placeholder the environment and matrix leave unset, so a plan stays
runnable with zero configuration while a deploy can override one value from the
environment without editing the file:

```yaml
vars:
  region: us-east          # fallback for every {{ region }} left unset
  rows: 50                 # an exact-placeholder value keeps its int type
jobs:
  - id: "feed-{{ region }}-{{ slug }}"
    matrix:
      - { slug: news }
      - { slug: jobs }
    source:
      kind: api
      config: { url: "https://feeds.example.com/{{ region }}/{{ slug }}?rows={{ rows }}" }
```

Both jobs query `us-east`; set the environment variable `region` to `eu-west` and
both switch with no file edit, while a matrix entry that set its own `region` would
win for that job.

- **Precedence** for a `{{ NAME }}` placeholder: a `matrix:`/`for_each:` per-entry
  value wins, then the process environment (variable `NAME`, if set), then the
  file-level `vars: NAME` default; an otherwise-unset placeholder is left verbatim. A
  `vars:` default therefore only fills a variable nothing else provides.
- A string that is *exactly* one placeholder keeps the default's native type
  (`rows: "{{ rows }}"` → the integer `50`), like matrix substitution. The `${VAR}` /
  `${VAR:-default}` env syntax is unrelated and still expands first, at read time.
- **Additive.** A plan with no `vars:` key (or an empty one) resolves byte-for-byte as
  before, and `ujin plan validate` rejects a non-mapping `vars:` with a clean
  `ujin: …` error (see [errors](#reporting-and-errors)).

The shipped `examples/vars-defaults.yaml` is a runnable `vars:` plan.

### `profiles:` — named overlays over `defaults:`

A top-level **`profiles:`** mapping (profile name → overlay mapping) declares named
variations of the plan's base `defaults:`. Selecting one **deep-merges that overlay
over `defaults:`** *before* the per-job merge, so every job that does not set the key
itself inherits the overlaid value, while per-job keys, `matrix:`/`for_each:` fan-out,
and `${VAR}` / `{{ var }}` substitution behave exactly as without a profile:

```yaml
defaults:
  schedule: { mode: poll, base: 60 }   # base cadence: every 60s
profiles:
  fast:
    schedule: { base: 5 }              # tighten the loop to 5s
  slow:
    schedule: { base: 600 }            # back off to 10 minutes
jobs:
  - id: home
    source: { kind: http, config: { url: "https://example.com/" } }
  - id: status
    schedule: { mode: poll, base: 9 }  # this job pins its own cadence
    source: { kind: http, config: { url: "https://example.com/status" } }
```

Select a profile with the **`--profile NAME`** flag or the **`$UJIN_PLAN_PROFILE`**
environment variable (the flag wins; neither set means no overlay):

```bash
# with no profile: home polls every 60s, status every 9s
ujin plan render ingest-plan.yaml

# --profile fast: home now polls every 5s; status keeps its own 9s (per-job wins)
ujin plan render ingest-plan.yaml --profile fast

# env fallback — the same overlay without a flag (a flag would override this)
UJIN_PLAN_PROFILE=slow ujin jobs-serve --plan ingest-plan.yaml
```

`--profile` is accepted by `ujin plan validate`, `ujin plan render`, and
`ujin jobs-serve`; `jobs-serve` validates the name before binding a port.

- **Merge order.** profile overlay over `defaults:` → that result deep-merged under
  each job. A profile key replaces the same key in `defaults:` (lists replace
  wholesale, nested mappings merge key-by-key), but a per-job key still wins over the
  profile, so the established precedence (per-job > profile > `defaults:`) holds.
- **Selection.** `--profile NAME` overrides `$UJIN_PLAN_PROFILE`; with neither set no
  overlay is applied and resolution is byte-for-byte unchanged.
- **Unknown name.** Selecting a profile absent from `profiles:` exits non-zero with a
  clean `ujin: …` message that lists the available profiles — no Python traceback (see
  [errors](#reporting-and-errors)). A directory of workflow files is lenient: a file
  with no `profiles:` key is left unchanged, so only a file that declares profiles but
  not the selected name fails.
- **Additive.** A plan with no `profiles:` key, and with no profile selected, resolves
  byte-for-byte as before; `plan validate` reports a non-mapping `profiles:` block as a
  clean error.

The shipped `examples/profiles.yaml` is a runnable `profiles:` plan.

## Stable ids

Every resulting job gets a **stable id**, so re-loading the same plan **upserts**
the same jobs rather than duplicating them:

- an explicit `id:` wins (for a matrix entry, an `id:` *template* such as
  `feed-{{ slug }}` yields a distinct id per entry);
- otherwise a **single-entry plan** gets just `<plan-stem>` (e.g. `ingest-plan`),
  and a **multi-entry plan** gets `<plan-stem>-<index>` (`ingest-plan-0`,
  `ingest-plan-1`, …) — the same single-vs-multi asymmetry as a single-file
  workflow vs a `jobs:` list;
- a matrix entry without an explicit `id:` template appends a per-row suffix — the
  row index for an inline/file-backed list (`<base>-<n>`), or the combination's
  slugified values for a cross-product axes mapping (`<base>-<value>-<value>…`, axis
  names sorted), so the id is deterministic and order-independent.

A derived id that collides with another job in the same plan fails *that* job (see
below) — give the entry an explicit unique `id:`.

## Load order

On startup `jobs-serve` loads jobs in this order (later sources upsert earlier
jobs sharing an id):

1. jobs reloaded from the durable store (`$UJIN_JOBS_DB`);
2. the optional positional `jobs.yaml` preload (`ujin jobs-serve jobs.yaml`);
3. the workflows-dir scan (`--workflows` / `$UJIN_WORKFLOWS_DIR`);
4. the **INGEST-PLAN** (`--plan` / `$UJIN_INGEST_PLAN`).

## Reporting and errors

When a plan is configured, `GET /health` gains a `plan` block listing what loaded
and what failed (the key is omitted entirely when no plan is configured):

```json
{
  "plan": {
    "path": "/data/ingest-plan.yaml",
    "loaded": ["crossref", "feed-tech", "feed-science", "feed-business"],
    "failed": []
  }
}
```

Errors **never abort startup**:

- A **file-level** problem (missing/unreadable file, invalid YAML, a top level
  that is neither a mapping nor a list, a missing/cyclic `include:`) fails the
  whole plan into `plan.failed` as one record, with a `ujin: …`-style message
  naming the file.
- A **per-job** problem (a bad `matrix:`, a colliding id, a spec that won't build)
  fails *just that job* — the remaining valid jobs still load — and is reported in
  `plan.failed` naming the offending job.

### Actionable validation errors

Each malformed-input case produces a `ujin: …` message that names the offending
file, job id, and key — never a bare Python traceback:

| Malformed input | Error scope | Message contains |
|---|---|---|
| `matrix:`/`for_each:` is a scalar (e.g. `42` or `"bad"`) | per-job | `must be a list of variable maps or a mapping of axis-name -> value-list, got <type>` |
| `matrix:` axis value is not a non-empty list | per-job | `` `matrix:` axis '<name>' must be a non-empty list of values `` |
| `include:`/`use:` path does not resolve | file-level | `include fragment not found: '<path>' (looked in: …)` |
| Top-level document is a bare scalar (workflow file) | file-level | `top-level must be a job mapping or a list of jobs, got <type>` |
| Top-level document is a bare scalar (plan file) | file-level | `must be a list of jobs or a mapping with a \`jobs:\` list, got <type>` |
| Job mapping is missing its `source:` key | per-job | `missing required 'source:' key` |

`ujin plan validate` surfaces these as `FAIL  <job-id>: ujin: …` lines (or in the
`failed` array with `--json`) and exits non-zero. The error always names the
originating file so you can locate the problem immediately:

```
FAIL  bad-job: ujin: plan job bad-job in plan.yaml: missing required 'source:' key
FAIL  plan     ujin: plan.yaml: include fragment not found: 'missing.yaml' (looked in: …)
```

Use `ujin plan validate --strict` to also promote schema warnings (unrecognized or
misspelled keys) to failures — useful in CI.

## Introspecting the active plan (`GET /plan`)

To see exactly which jobs the running server's active plan and workflows directory
currently resolve to — without mutating anything — read the plan introspection
endpoint:

```bash
curl localhost:8902/plan
```

`GET /plan` re-runs the **same** loaders and provenance plumbing as
`ujin plan render --explain` (`defaults:`/`include:`/`matrix:`/`${VAR}` fully
expanded), so each returned job names its originating source file and, for a
`matrix:`/`for_each:`-expanded job, the matrix entry that produced it. It is purely
read-only — it never creates, mutates, prunes, or reloads a job, so `GET /jobs` is
identical before and after:

```json
{
  "plan_path": "/data/ingest-plan.yaml",
  "workflows_dir": "/workflows",
  "jobs": [
    {"id": "crossref", "provenance": {"source": "/data/ingest-plan.yaml", "matrix": null}},
    {"id": "feed-tech", "provenance": {"source": "/data/ingest-plan.yaml",
                                        "matrix": {"key": "matrix", "index": 0, "vars": {"slug": "tech"}}}},
    {"id": "feed-science", "provenance": {"source": "/data/ingest-plan.yaml",
                                           "matrix": {"key": "matrix", "index": 1, "vars": {"slug": "science"}}}}
  ],
  "failed": []
}
```

`plan_path` is `null` when no `--plan`/`$UJIN_INGEST_PLAN` is configured (jobs then
come only from `workflows_dir`); with neither configured, `jobs` is empty. Files or
jobs that fail to resolve are reported in `failed` (`{id, error}`) exactly as the
[`GET /health` plan block](#reporting-and-errors) lists them. If the server was
started with [`--only`/`--exclude`](#job-id-selection----only-and---exclude), the
plan jobs are filtered by the same selectors, matching what actually loaded. This is
the live counterpart to the offline `ujin plan render --explain` CLI.

## Reloading a plan without restarting

When the plan file changes, re-apply it in-process instead of restarting the server:

```bash
curl -X POST localhost:8902/plan/reload
```

`POST /plan/reload` re-reads the configured plan path (`--plan` / `$UJIN_INGEST_PLAN`),
re-runs the **identical** resolution and error handling as startup
(`defaults:`/`include:`/`matrix:`/`${VAR}`), and upserts the resolved jobs by their
stable ids into the running job manager — so added or changed jobs become visible via
`GET /jobs` and the refreshed `GET /health` plan block with no downtime. The response
mirrors the startup plan-status shape, plus a `reloaded` flag:

```json
{
  "reloaded": true,
  "path": "/data/ingest-plan.yaml",
  "loaded": ["crossref", "feed-tech", "feed-science"],
  "failed": [{"id": "feed-business", "error": "ujin: plan job feed-business: …"}]
}
```

A single malformed job is reported in `failed` while the rest reload (a reload never
aborts). By default reload only **upserts** — it never deletes jobs dropped from the file
(see [prune mode](#pruning-jobs-dropped-from-the-plan) to opt into removal). If the
server was started with [`--only`/`--exclude`](#job-id-selection----only-and---exclude)
selectors, reload applies the **same** filters, so it never loads jobs the startup
filter excluded; unlike startup, a `--only` pattern that now matches nothing is reported
(`{"reloaded": false, "detail": "ujin: --only …: no job ids matched", …}`) rather than
aborting the running server. When **no plan is configured** the endpoint is a safe no-op
that changes nothing and
returns `{"reloaded": false, "detail": "no plan configured", "loaded": [], "failed": []}`.
Like every mutating jobs route, `/plan/reload` is gated by the optional API-key auth
(`UJIN_API_KEY`); only `/health` stays open.

### Pruning jobs dropped from the plan

By default a reload **upserts only** — a job you delete from the plan file keeps running
until you remove it explicitly. Pass `?prune=true` (or a `{"prune": true}` JSON body) to
also **remove** plan-owned jobs whose ids are no longer declared by the re-read plan:

```bash
curl -X POST 'localhost:8902/plan/reload?prune=true'
# or, equivalently:
curl -X POST localhost:8902/plan/reload -H 'content-type: application/json' \
     -d '{"prune": true}'
```

The removed ids are returned in a `pruned` list alongside `loaded`/`failed`, and the
pruned jobs disappear from `GET /jobs` and the refreshed `/health` plan block:

```json
{
  "reloaded": true,
  "path": "/data/ingest-plan.yaml",
  "loaded": ["crossref", "feed-tech"],
  "failed": [],
  "pruned": ["feed-science"]
}
```

ujin tracks each running job's **provenance** — whether it came from the mounted
INGEST-PLAN, the positional `jobs.yaml` preload, the workflows dir, or the REST API — so
prune only ever removes **plan-owned** jobs. Jobs created via `POST /jobs` or the
positional preload are **never** pruned, even when their ids are absent from the plan.
Prune is also skipped when the plan fails to read at the file level (a missing or
malformed file), so a transient read error never wipes plan jobs — that reload reports
the failure and `pruned` is `[]`. Without the option the response omits `pruned` entirely
and nothing is removed, byte-for-byte the upsert-only behaviour.

### Previewing a reload (dry run)

To see what a reload *would* do before committing to it, add `?dry_run=true` (or a
`{"dry_run": true}` JSON body, accepted exactly like `prune`):

```bash
curl -X POST 'localhost:8902/plan/reload?dry_run=true'
# combine with prune to also preview removals:
curl -X POST 'localhost:8902/plan/reload?prune=true&dry_run=true'
# or, equivalently, via a JSON body:
curl -X POST localhost:8902/plan/reload -H 'content-type: application/json' \
     -d '{"dry_run": true, "prune": true}'
```

A dry run resolves the plan through the **identical** loader as a real reload
(`defaults:`/`include:`/`matrix:`/`${VAR}`) and the same `--only`/`--exclude` filters,
but **mutates nothing** — the job manager is untouched, so `GET /jobs` and the
`/health` plan block are identical immediately before and after the call. Instead of
upserting, the response classifies each resolved job id:

```json
{
  "reloaded": false,
  "dry_run": true,
  "path": "/data/ingest-plan.yaml",
  "added": ["feed-business"],
  "updated": ["feed-tech"],
  "unchanged": ["crossref", "feed-science"],
  "loaded": [],
  "failed": [],
  "removed": ["feed-defunct"]
}
```

`added` are ids not currently running (a real reload would create them), `updated`
are running jobs whose resolved spec differs (would be re-registered), and
`unchanged` are running jobs the re-read plan leaves identical (a no-op upsert).
`removed` is present only with `prune` and lists the plan-owned ids a real prune
would delete — but no job is removed. `reloaded` stays `false`; nothing was applied.
A malformed job still surfaces in `failed`, and the same guards apply: a file-level
read error reports no removals, and `--only` matching nothing is reported rather than
fatal. With **no plan configured** the dry run is a non-error no-op returning
`{"reloaded": false, "dry_run": true, "detail": "no plan configured", "loaded": [], "failed": []}`.

### Auto-reloading on file change (`--watch-plan`)

Instead of calling `POST /plan/reload` by hand after every edit, you can have
`jobs-serve` watch the plan file and reload it for you. It is **off by default** —
without the flag/env no watcher runs and startup is byte-identical to today.

```bash
# enable via flag (default poll interval: 2.0s)
ujin jobs-serve --plan ./ingest-plan.yaml --watch-plan

# tune the poll interval (seconds)
ujin jobs-serve --plan ./ingest-plan.yaml --watch-plan --watch-interval 5

# or via env (flag wins over env if both are set)
UJIN_WATCH_PLAN=1 UJIN_WATCH_INTERVAL=5 ujin jobs-serve --plan ./ingest-plan.yaml
```

When enabled, a single background task stats the plan file every interval and, on a
change, re-runs the **identical** resolution as `POST /plan/reload?prune=true` (same
`defaults:`/`include:`/`matrix:`/`${VAR}` loader, the same `--only`/`--exclude`
filters, and the manager's provenance-aware upsert/prune). So a saved edit takes
effect with **no restart and no manual POST**: a newly-added job id appears in
`GET /jobs` and the `/health` plan block, a changed job is re-registered, and a job
removed from the file is pruned.

The same safety guarantees as the manual prune apply:

- **Plan-owned only** — jobs created via `POST /jobs` or the positional `jobs.yaml`
  preload are never pruned, even when absent from the plan.
- **A bad save never wipes jobs** — a malformed or transiently-unreadable file is
  skipped: the running job set is kept and the parse error surfaces in the `/health`
  plan block's `failed` list (and the logs). The next valid save reloads normally.
- **Race-free with the manual endpoint** — the reload is applied atomically, and
  re-applying the same ids is idempotent, so the watcher and a concurrent
  `POST /plan/reload` never conflict or double-apply harmfully.

The watcher mirrors `POST /plan/reload`, which is plan-only: `--watch-plan` with no
`--plan`/`$UJIN_INGEST_PLAN` configured is inactive (logged once at startup).

## Validating a plan without starting the server

Use `ujin plan validate` to check a plan file (or a workflows directory) with the
**identical loaders** as `jobs-serve` — so the resolved ids and error messages match
exactly what the server would produce — without binding a port or starting the engine:

```bash
ujin plan validate ./ingest-plan.yaml          # human-readable
ujin plan validate ./ingest-plan.yaml --json   # machine-readable (CI)
```

Output on success (exit 0):

```
ok  papers
ok  prices
ok  feed-tech
ok  feed-science
```

Output when a job fails (exit non-zero):

```
ok  papers
FAIL  feed-bad: ujin: plan job feed-bad in ingest-plan.yaml: `matrix:` must be a non-empty list of variable maps
ujin: 1 failure(s); 1 job(s) resolved
```

Every failure message names the offending source file (and job id for per-job
errors) so you can locate the problem without guessing which file triggered it.

With `--json` the response is a single JSON object on stdout:

```json
{
  "ok": false,
  "resolved": ["papers"],
  "failed": [
    {"id": "feed-bad", "error": "ujin: plan job feed-bad in ingest-plan.yaml: `matrix:` must be a non-empty list of variable maps"}
  ],
  "warnings": [],
  "strict": false
}
```

A missing or unreadable path exits non-zero immediately with a clean `ujin: …` error
and no traceback. The `--json` flag covers that case too:

```json
{"ok": false, "error": "ujin: path not found: ./missing.yaml", "resolved": [], "failed": [], "warnings": [], "strict": false}
```

### Validation & common mistakes — misspelled keys

After `include:`/`defaults:` resolution the loader checks every mapping against the
set of recognized keys: at the top level `jobs` / `defaults` (plus `include:`/`use:`,
which are resolved away), and per job `source`, `transforms`, `sinks`, `schedule`,
`id`, `name`, `enabled` plus the additive `matrix:`/`for_each:` and `include:`/`use:`.
A key outside that set — a typo such as `defaluts:` for `defaults:`, or `shedule:` for
`schedule:` — would otherwise be **silently ignored** (YAML happily keeps it; the job
loads with that setting missing). The validator surfaces it instead, naming the file,
the job id when applicable, and the unknown key, with a `did you mean …?` hint:

```
ok  crossref
WARN  ujin: plan ingest-plan.yaml: unrecognized top-level key 'defaluts'; did you mean 'defaults'?
WARN  ujin: plan job feed-tech in ingest-plan.yaml: unrecognized key 'shedule'; did you mean 'schedule'?
```

These are **advisory by default**: the warning is printed (and logged on the server),
but the job still loads and the exit code is unaffected — so the change is additive and
no previously-valid plan becomes invalid. Pass `--strict` to promote every such warning
to a failure (printed as `FAIL`, non-zero exit) for a CI gate; with `--json`, warnings
appear in a `warnings` array and `--strict` is reflected in `ok` and a `strict` field:

```bash
ujin plan validate ./ingest-plan.yaml --strict        # CI: unknown keys fail the build
ujin plan validate ./ingest-plan.yaml --strict --json
```

## Inspecting fully-resolved jobs

Use `ujin plan render` to print every fully-expanded `JobSpec` — after all five
resolution layers (`defaults:` deep-merge, `include:`/`use:` fragments,
`matrix:`/`for_each:` fan-out, `${VAR}` substitution) — in human-readable YAML,
using the **same loaders** as `jobs-serve` and `plan validate`:

```bash
ujin plan render ./ingest-plan.yaml          # YAML, one document per job
ujin plan render ./workflows/               # also works for a workflows directory
ujin plan render ./ingest-plan.yaml --json  # JSON array (same shape as GET /jobs)
```

Human-readable output (each job is a YAML document prefixed with `--- # <id>`):

```
--- # crossref
id: crossref
name: crossref
source:
  kind: api
  config:
    url: https://api.crossref.org/works?rows=50
...

--- # feed-tech
id: feed-tech
name: feed-tech
source:
  kind: api
  config:
    url: https://feeds.example.com/tech?rows=50
...
```

A matrix-bearing template prints one document per matrix row — you can see exactly
what variables were substituted and which defaults were merged in.

The `--json` flag emits a JSON array whose length equals the number of resolved jobs,
with each element using the same spec shape as `GET /jobs/{id}` on the running server:

```bash
ujin plan render ./ingest-plan.yaml --json | python3 -c \
  "import sys,json; specs=json.load(sys.stdin); print(len(specs), 'jobs')"
```

`ujin plan render` exits zero when all jobs resolve and non-zero with a clean
`ujin: …` message (no traceback) on a missing/unreadable path or any failed job.

### Concise overview — `--summary`

Add `--summary` to print a compact human-readable overview instead of the full
expanded specs — useful for a quick sanity-check before deploying:

```bash
ujin plan render ./ingest-plan.yaml --summary
ujin plan render ./workflows/       --summary   # also works for a workflows directory
```

Output format: a job count on the first line, then one indented line per resolved
job showing its id, source kind (and url if present), schedule, and number of sinks:

```
3 jobs
  crossref    api:https://api.crossref.org/works?rows=50  adaptive/60s  0 sinks
  feed-tech   api:https://feeds.example.com/tech?rows=50  adaptive/60s  1 sink
  feed-science  api:https://feeds.example.com/science?rows=50  adaptive/60s  1 sink
```

`--summary` reuses the same loaders as the normal render path, so resolution
(`defaults:`, `include:`/`use:`, `matrix:`/`for_each:`, profiles, `${VAR}`) is
identical. It composes with `--only`/`--exclude`, `--env-file`, and `--profile`;
the default `ujin plan render` output (no flag) is byte-for-byte unchanged.

### Provenance — `--explain`

Add `--explain` to annotate each rendered job with **where it came from**: the
originating plan/workflow source file and, for a `matrix:`/`for_each:`-expanded
job, the matrix entry index and the exact variable map that produced it. This makes
it easy to answer "which file and which matrix row created `feed-science`?" without
re-reading the template by hand. Provenance is threaded through the loaders **only**
when `--explain` is set, so the default render output is byte-for-byte unchanged.

```bash
ujin plan render ./ingest-plan.yaml --explain          # YAML with `# …` provenance comments
ujin plan render ./ingest-plan.yaml --explain --json   # machine-readable provenance
```

In YAML form each job gains `#`-comment lines above its body — harmless to a YAML
parser, readable to a human:

```
--- # feed-science
# source: examples/ingest-plan.yaml
# matrix: entry 1 -> {slug: science}
name: feed-science
source:
  kind: api
  config:
    url: https://feeds.example.com/science?rows=50
...
```

With `--explain --json` each array element pairs the resolved spec with a
`provenance` object (`source`, plus a `matrix` block of `key`/`index`/`vars` for
fanned-out jobs) instead of being a bare spec:

```bash
ujin plan render ./ingest-plan.yaml --explain --json | python3 -c \
  "import sys,json; rows=json.load(sys.stdin); \
   print(rows[2]['spec']['id'], '<-', rows[2]['provenance'])"
# feed-science <- {'source': 'examples/ingest-plan.yaml', 'matrix': {'key': 'matrix', 'index': 1, 'vars': {'slug': 'science'}}}
```

`--explain` composes with `--json`, `--only`/`--exclude` (provenance survives
selection — it is keyed to the final expanded id), and `--env-file`.

## Diffing two plans — `plan diff`

Use `ujin plan diff OLD NEW` to see what changed between two plans (or two
workflows directories) **before** you reload or redeploy. Both inputs are resolved
with the same loaders as `plan validate`/`plan render` — `defaults:` deep-merge,
`include:`/`use:` fragments, `matrix:`/`for_each:` fan-out, and `${VAR}`
substitution all expanded — then compared keyed by **final job id**:

```bash
ujin plan diff ./old-plan.yaml ./ingest-plan.yaml                    # human-readable
ujin plan diff ./old-workflows/ ./workflows/                         # also works for dirs
ujin plan diff ./old-plan.yaml ./ingest-plan.yaml --json             # machine-readable (CI)
ujin plan diff ./old-plan.yaml ./ingest-plan.yaml --exit-code        # exit 1 if any diff
ujin plan diff ./old-plan.yaml ./ingest-plan.yaml --check            # alias for --exit-code
```

The human form prints one line per affected job — `+` added in `NEW`, `-` removed
in `NEW`, `~` changed (present in both but with a differing resolved spec) — and a
summary count on stderr:

```
+ feed-business
- feed-tech
~ feed-science
ujin: 1 added, 1 removed, 1 changed
```

Diffing a plan against an identical copy prints `no changes`. The comparison
ignores each job's per-load `created_at` timestamp, so two files with identical
content diff empty.

`--json` emits the same information as a structured object with distinct
`added`/`removed`/`changed` groupings. Added and removed entries carry the job's
resolved `spec`; changed entries carry both `old` and `new` (each with the
`created_at` noise stripped), so a downstream tool can show a field-level diff:

```bash
ujin plan diff ./old-plan.yaml ./ingest-plan.yaml --json | python3 -c \
  "import sys,json; d=json.load(sys.stdin); \
   print(len(d['added']),'added', len(d['removed']),'removed', len(d['changed']),'changed')"
```

```json
{
  "added":   [{"id": "feed-business", "spec": { … }}],
  "removed": [{"id": "feed-tech", "spec": { … }}],
  "changed": [{"id": "feed-science", "old": { … }, "new": { … }}]
}
```

`plan diff` exits **zero** whenever both inputs resolve — including when there is
no difference. Pass `--exit-code` (alias `--check`) to flip this for CI: the
command then exits **non-zero** when there are any added/removed/changed jobs and
zero when the plans are identical. `--exit-code` does not affect `--json` output —
the structure is identical with or without the flag. `--env-file` is accepted
consistently with the other plan subcommands (it fills in `${VAR}`s not already
in the environment for both plans). An unreadable or schema-invalid `OLD`/`NEW`
input exits non-zero with a clean `ujin: …` message and no traceback, regardless
of `--exit-code`.

## Exporting the file-format schema — `plan schema`

Use `ujin plan schema` to print a machine-readable **JSON Schema** (draft 2020-12)
describing the INGEST-PLAN / workflow file format itself — the per-job shape
(`source` / `transforms` / `sinks` / `schedule`), the top-level `defaults:` block,
`include:`/`use:` fragment references, `matrix:`/`for_each:` fan-out, `vars:`
defaults, and `profiles:` overlays. Feed it to an editor for autocompletion and
linting, or to a CI check that a plan is structurally well-formed *before*
`plan validate` runs the loaders:

```bash
ujin plan schema                                   # print the schema to stdout
ujin plan schema --out ingest-plan.schema.json     # …or write it to a file
ujin plan schema | python3 -m json.tool >/dev/null && echo "valid JSON"
```

The document is a single deterministic JSON object with the usual JSON Schema keys
(`$schema`, `$id`, `type`, `properties`, `$defs`); repeated runs are byte-for-byte
identical, and `--out PATH` writes the same bytes to a file (a write error exits
non-zero with a clean `ujin: …` message and leaves stdout empty). It describes the
**unresolved** file — what you write on disk — so it is deliberately permissive: a
job mapping may omit keys that `defaults:` or the filename stem supply, and
`additionalProperties` stays open (the advisory unknown-key check in
[`plan validate --strict`](#validation--common-mistakes--misspelled-keys) owns typo
detection). It constrains the document shape and the *types* of recognized keys —
enough to reject a structurally-malformed plan (a scalar document, a non-list
`jobs:`, a string `source:`) while accepting every shipped example under
`examples/`. `plan schema` is read-only: it resolves no plan, never touches
`JobSpec.from_dict` or the loaders, and always exits 0 (apart from an `--out` write
error).

Validate a plan against the emitted schema with any draft-2020-12 validator —
e.g. Python's [`jsonschema`](https://pypi.org/project/jsonschema/):

```bash
ujin plan schema --out ingest-plan.schema.json
python3 - <<'PY'
import json, yaml, jsonschema
schema = json.load(open("ingest-plan.schema.json"))
plan = yaml.safe_load(open("examples/ingest-plan.yaml"))
jsonschema.Draft202012Validator(schema).validate(plan)   # raises on a malformed plan
print("ingest-plan.yaml conforms")
PY
```

## Job-id selection — `--only` and `--exclude`

All three plan-aware commands (`plan render`, `plan validate`, and `jobs-serve`)
accept `--only` and `--exclude` to filter which jobs are actually processed.
Selection runs **after full resolution** — after `defaults:` deep-merge,
`include:`/`use:` fragment inlining, `matrix:`/`for_each:` fan-out, and `${VAR}`
substitution — so patterns always match the **final expanded job id**.

```bash
# render only jobs whose id starts with "feed-"
ujin plan render  ./ingest-plan.yaml --only 'feed-*'

# comma-separated or repeatable patterns
ujin plan render  ./ingest-plan.yaml --only 'feed-tech,feed-science'
ujin plan render  ./ingest-plan.yaml --only 'feed-tech' --only 'feed-science'

# --exclude subtracts from the --only set
ujin plan render  ./ingest-plan.yaml --only 'feed-*' --exclude feed-tech

# validate only the selected subset
ujin plan validate ./ingest-plan.yaml --only 'feed-*' --json

# preload only matching jobs; /health plan.loaded reflects the filtered set
ujin jobs-serve   --plan ./ingest-plan.yaml --only 'feed-*'
```

Patterns are **fnmatch-style globs** (`*` matches any string, `?` matches one
character, `[seq]` matches a character in the set) matched case-sensitively
against each fully-expanded job id. When both flags are given, `--exclude`
subtracts from the `--only` set. A `--only` pattern that matches no resolved
job exits non-zero immediately with a clean `ujin: …` message and no traceback:

```
ujin: --only 'no-such-*': no job ids matched (5 job(s) resolved)
```

With no selector flags, all three commands are **byte-for-byte unchanged**.

## Example

A ready-to-mount plan demonstrating `defaults:` + a shared `include:` + a
`matrix:` fan-out across several jobs ships at
[`examples/ingest-plan.yaml`](../examples/ingest-plan.yaml) (it reuses the
fragments under `examples/workflows/fragments/`).
