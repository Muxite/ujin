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

## Schema

The top level is **either** a list of job mappings **or** a mapping with a
`jobs:` list plus an optional `defaults:` mapping:

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

## Stable ids

Every resulting job gets a **stable id**, so re-loading the same plan **upserts**
the same jobs rather than duplicating them:

- an explicit `id:` wins (for a matrix entry, an `id:` *template* such as
  `feed-{{ slug }}` yields a distinct id per entry);
- otherwise a **single-entry plan** gets just `<plan-stem>` (e.g. `ingest-plan`),
  and a **multi-entry plan** gets `<plan-stem>-<index>` (`ingest-plan-0`,
  `ingest-plan-1`, …) — the same single-vs-multi asymmetry as a single-file
  workflow vs a `jobs:` list;
- a matrix entry without an explicit `id:` template appends a per-entry suffix
  (`<base>-<n>`).

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
aborts). Reload only **upserts** — it never deletes jobs dropped from the file. If the
server was started with [`--only`/`--exclude`](#job-id-selection----only-and---exclude)
selectors, reload applies the **same** filters, so it never loads jobs the startup
filter excluded; unlike startup, a `--only` pattern that now matches nothing is reported
(`{"reloaded": false, "detail": "ujin: --only …: no job ids matched", …}`) rather than
aborting the running server. When **no plan is configured** the endpoint is a safe no-op
that changes nothing and
returns `{"reloaded": false, "detail": "no plan configured", "loaded": [], "failed": []}`.
Like every mutating jobs route, `/plan/reload` is gated by the optional API-key auth
(`UJIN_API_KEY`); only `/health` stays open.

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
FAIL  feed-{{ slug }}: ujin: plan job feed-{{ slug }}: matrix must be a list, got str
ujin: 1 failure(s); 1 job(s) resolved
```

With `--json` the response is a single JSON object on stdout:

```json
{
  "ok": false,
  "resolved": ["papers"],
  "failed": [
    {"id": "feed-{{ slug }}", "error": "ujin: plan job feed-{{ slug }}: matrix must be a list, got str"}
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
