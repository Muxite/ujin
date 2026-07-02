# ujin ingest plans — declare many jobs from one file

An **ingest plan** is a single external file that declares many [jobs](JOBS.md)
at once: shared `defaults:` deep-merged into every job, plus an optional per-job
`matrix:` that fans one job template out into one job per combination. It's a
companion to file-driven [workflows](WORKFLOWS.md) — same `source → transforms →
sinks → schedule` job shape underneath — but scoped to *one file describing many
jobs* rather than *one file per job*. ujin ships no plan content: the consuming
program owns the plan file; ujin ships only the loader, schema, and this generic
example.

## Loading a plan

```bash
ujin jobs-serve --plan examples/ingest-plan.yaml
# or
UJIN_INGEST_PLAN=examples/ingest-plan.yaml ujin jobs-serve
```

`--plan` takes precedence over `$UJIN_INGEST_PLAN`; if neither is set, no plan is
loaded (a no-op — an existing no-config deploy is unaffected). A missing or
malformed plan is **not fatal**: it's reported under `GET /health`, and
`jobs-serve` keeps running with whatever else it loaded (preloaded jobs,
`--workflows`, jobs created over the API).

```json
{ "ok": true, "jobs": 6,
  "plan": { "source": "examples/ingest-plan.yaml", "loaded": 6, "failed": 0, "error": null } }
```

- `source` — the resolved path (or `null` if no plan was configured).
- `loaded` — jobs successfully created from the plan.
- `failed` — jobs the plan declared but that failed to create (e.g. an unknown
  source kind); each failure is logged with its job id.
- `error` — set when the plan file itself couldn't be loaded (missing, unreadable,
  or invalid YAML/schema); `loaded`/`failed` stay `0` in that case.

## Schema

```yaml
defaults:              # optional; deep-merged into every job below
  sinks: [{kind: sqlite}]
  schedule: {mode: adaptive, base: 300, min: 60, max: 3600}

jobs:                  # required; a list of job entries
  - id: status-${name}           # optional; supports "${var}"/"${ENV_VAR}" substitution
    matrix:                      # optional; fans this entry out into several jobs
      name: [github, npm, pypi]  # mapping of axis -> [values] -> cartesian product
    source:
      kind: http
      config:
        url: "https://status.${name}.example.com/"
```

- **`defaults:`** is a mapping deep-merged onto every job entry (job values win on
  conflict). Nested mappings merge key-by-key; lists (e.g. `sinks:`) are replaced
  wholesale by a job's own list, not concatenated.
- **`jobs:`** is a list of job entries. Each entry is the same shape as a
  [workflow file](WORKFLOWS.md) (`id`, `name`, `source`, `transforms`, `sinks`,
  `schedule`, `enabled`) plus an optional `matrix:`.
- **`matrix:`** fans one job entry out into one job per combination:
  - A **mapping** of `axis: [values]` expands to the cartesian product of all
    axes (`{name: [a, b], region: [us, eu]}` → 4 combos).
  - A **list** of mappings is taken as explicit rows — one job per row, no
    product (use this when combinations aren't a full cartesian sweep, or a
    combo needs its own extra field like a differing interval).
  - An **empty** matrix (`{}` or `[]`) produces zero jobs for that entry.
  - Omitting `matrix:` entirely produces exactly one job from the entry.

## Substitution

Two passes run per generated job, in order:

1. **Matrix variables** — `${name}` is replaced by the combo's `name` value.
   A scalar that is *exactly* one reference (e.g. `base: ${base}`) is
   type-preserving — a numeric/boolean matrix value stays numeric/boolean, not
   stringified. A reference embedded in more text (e.g. a URL) is substituted
   textually. A `${...}` name the matrix combo doesn't have is left untouched.
2. **Environment variables** — `${VAR}` / `${VAR:-default}` left over after the
   matrix pass expand from the process environment (unset + no default → `""`,
   logged once), the same rule file-driven workflows use.

A job with no `matrix:` still goes through both passes with an empty combo, so
`${ENV_VAR}` references still resolve.

## Job ids

- If a job's `id:` (after substitution) is set and distinct from every other job
  its entry produces, that id is used as-is — e.g. `status-${name}` with
  `matrix: {name: [github, npm]}` yields `status-github` and `status-npm`.
- Otherwise (no `id:`, or the template didn't vary per combo, or it collided),
  ids fall back to `<stem>-<index>`, where `<stem>` is the entry's plain
  (non-templated) `id:` if it has one, else `job-<entry-index>`.
- `name:` defaults to the final `id:` when the entry doesn't set one.

Because ids are derived from the plan file's own content, re-loading the same
plan (a restart, a redeploy) **upserts the same jobs** rather than duplicating
them — the same behavior file-driven workflows give you.

## Example

`examples/ingest-plan.yaml` loads as 6 jobs: one plain `homepage` job (no
matrix), three `status-*` jobs from a mapping matrix, and two `feed-*` jobs from
a list-of-rows matrix. Try it:

```bash
ujin jobs-serve --plan examples/ingest-plan.yaml
curl localhost:8902/health   # -> "plan": {"loaded": 6, "failed": 0, ...}
curl localhost:8902/jobs
```
