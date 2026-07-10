# ujin ingest plans — one file, many jobs

An **ingest plan** is a single mounted file that declares a whole batch of
[jobs](JOBS.md) at once. Where the [workflows directory](WORKFLOWS.md) is
"one file = one job", a plan is "one file = a topology": common configuration is
factored out into `defaults`, reused through named `templates`, and multiplied
across a `matrix`. Mount one file, get every job it declares registered on
startup — same `source → transforms → sinks → schedule` shape, same engine, same
durability.

The loader is **generic**. ujin ships no built-in plan and the format carries no
site- or consumer-specific identifiers; you supply the plan the same way you
supply [marketplace profiles](MARKETPLACE.md).

## Mounting a plan

The plan's path comes from the `--plan` flag or the `$UJIN_INGEST_PLAN` env var
(mirroring how `$UJIN_MARKETPLACE_PROFILES` mounts marketplace profiles):

```bash
ujin jobs-serve --plan examples/ingest-plan.yaml
UJIN_INGEST_PLAN=/plans/ingest-plan.yaml ujin jobs-serve
```

```yaml
# docker-compose.yml (ujin-jobs)
environment:
  - UJIN_INGEST_PLAN=/plans/ingest-plan.yaml
volumes:
  - ./plans/ingest-plan.yaml:/plans/ingest-plan.yaml:ro
```

The plan is loaded **in addition to** (not instead of) a preloaded jobs file and
the workflows directory — all three coexist. With no `--plan` / `$UJIN_INGEST_PLAN`
set, nothing changes: a no-config deploy behaves exactly as before.

## Format

A plan is a mapping with three optional top-level keys plus the required `jobs`
list (or, for the simple case, just a bare top-level list of job entries):

```yaml
defaults:            # deep-merged into every job (job keys win)
  schedule: {mode: adaptive, base: 300}
  sinks: [{kind: sqlite, config: {}}]

templates:           # named fragments a job opts into with `use:`
  feed:
    source: {kind: rss}
    transforms: [{kind: dedupe, config: {key: id}}]

jobs:
  - id: status-page                 # plain job — inherits defaults
    source: {kind: http, config: {url: https://example.com/status}}

  - id: blog-feed
    use: feed                       # pull in the `feed` template
    source: {config: {url: "${BLOG_FEED_URL:-https://example.com/blog.xml}"}}

  - id: mirror
    use: feed
    matrix: {region: [us, eu, ap]}  # → mirror-0, mirror-1, mirror-2
    source: {config: {url: "https://${region}.example.com/feed.xml"}}
```

### `defaults` — shared configuration

A mapping deep-merged into every job. Nested mappings merge recursively; a job's
own keys win over the defaults. Lists are replaced wholesale, not concatenated.

### `templates` — reusable fragments

Named mappings under `templates:` (the alias `fragments:` also works). A job
opts in with `use: <name>` or `use: [<name>, <name>]`. Precedence, lowest to
highest: `defaults` < template(s) (in listed order) < the job's own keys.

### `matrix` — cartesian expansion

A `matrix:` on a job entry maps axis names to lists of values. The entry is
expanded into one job per combination of values (the cartesian product). Each
combination's values are available as `${name}` references anywhere in the job
body.

Ids stay deterministic: when a job has a `matrix` and its `id` contains no
`${…}` reference, the combination index is appended (`mirror-0`, `mirror-1`, …).
Put a `${axis}` in the `id` to control the naming yourself.

### `${VAR}` substitution

`${VAR}` and `${VAR:-default}` references resolve from the matrix variables
first, then the process environment. An unset variable with no default expands to
`""` (and logs a warning), mirroring shell semantics. A string that is **exactly**
one `${VAR}` token preserves the referenced value's type, so a numeric matrix
axis stays an int:

```yaml
matrix: {rows: [25, 50]}
source: {kind: api, config: {url: "https://api.example.com?rows=${rows}"}}
```

## Validation & failure handling

Malformed plans produce a **clear, actionable** error that names the offending
job or key, e.g.:

- `ingest plan not found: /plans/x.yaml`
- `ingest plan (…): job #2 must be a mapping, got list`
- `job #1 references unknown template 'feeed'; defined templates: ['feed']`
- `job #0: matrix axis 'region' must be a non-empty list`
- `ingest plan (…): job 'mirror-1' needs a 'source' with a 'kind'`

Loading is **non-fatal**, exactly like a bad workflow file. A bad or missing
plan is logged and reported on `/health` under the `plan` block — startup is
never blocked:

```jsonc
GET /health
{
  "ok": true,
  "jobs": 5,
  "plan": {
    "path": "/plans/ingest-plan.yaml",
    "loaded": ["status-page", "blog-feed", "mirror-0", "mirror-1", "mirror-2"],
    "failed": [],
    "error": null            // present only when the whole plan failed to load
  }
}
```

`loaded` lists the ids that registered; `failed` lists per-job registration
errors (e.g. an unknown `kind`); `error` is set only when the plan itself could
not be parsed. When no plan is configured, `path` is `null` and both lists are
empty.

## Programmatic use

```python
from ujin.jobs.plan import load_plan, parse_plan, PlanError

specs = load_plan("examples/ingest-plan.yaml")   # list[JobSpec]
specs = parse_plan(already_loaded_dict)           # from an in-memory mapping
```

Both raise `PlanError` (with the actionable message above) on a structural
problem. See [`examples/ingest-plan.yaml`](../examples/ingest-plan.yaml) for a
complete, runnable plan.
