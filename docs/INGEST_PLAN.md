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
`source` / `transforms` / `sinks` / `schedule`, with `${VAR}` / `${VAR:-default}`
substitution expanded from the environment at load time.

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

## Stable ids

Every resulting job gets a **stable id**, so re-loading the same plan **upserts**
the same jobs rather than duplicating them:

- an explicit `id:` wins (for a matrix entry, an `id:` *template* such as
  `feed-{{ slug }}` yields a distinct id per entry);
- otherwise the id is `<plan-stem>-<index>` (`ingest-plan-0`, `ingest-plan-1`, …);
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
  ]
}
```

A missing or unreadable path exits non-zero immediately with a clean `ujin: …` error
and no traceback. The `--json` flag covers that case too:

```json
{"ok": false, "error": "ujin: path not found: ./missing.yaml", "resolved": [], "failed": []}
```

## Example

A ready-to-mount plan demonstrating `defaults:` + a shared `include:` + a
`matrix:` fan-out across several jobs ships at
[`examples/ingest-plan.yaml`](../examples/ingest-plan.yaml) (it reuses the
fragments under `examples/workflows/fragments/`).
