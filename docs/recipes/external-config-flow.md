# Recipe: end-to-end external-config flow

**What this shows:** how the three additive config layers — `defaults:`, `include:` fragments, and `matrix:` fan-out — compose inside a single mounted **INGEST-PLAN** file, and how to validate and inspect the result without starting the server.

References: [INGEST_PLAN.md](../INGEST_PLAN.md), [WORKFLOWS.md](../WORKFLOWS.md), [JOBS.md](../JOBS.md).

---

## Scenario

You want to poll three content feeds — `tech`, `science`, `business` — plus one
static API endpoint. All four jobs share the same source settings, transform
pipeline, and schedule. You want to keep the configuration in a single file that
you can mount as a volume and validate in CI.

---

## 1. Shared fragments

Fragments live in a subdirectory (`fragments/`) so the workflows-dir startup scan
does not accidentally load them as standalone workflows (the scan is
**non-recursive**).

```bash
mkdir -p fragments
```

**`fragments/adaptive-hourly.yaml`** — reused schedule:

```yaml
# fragments/adaptive-hourly.yaml
mode: adaptive
base: 3600
min: 600
max: 86400
jitter: decorrelated
```

**`fragments/webhook-sink.yaml`** — reused sink (endpoint from env):

```yaml
# fragments/webhook-sink.yaml
kind: webhook
config:
  url: "${INGEST_URL:-http://localhost:8000/ingest}"
```

---

## 2. The ingest-plan

**`ingest-plan.yaml`:**

```yaml
# ingest-plan.yaml — four jobs from one file.
#
# Run:   ujin jobs-serve --plan ./ingest-plan.yaml
# Check: ujin plan validate ./ingest-plan.yaml
# Peek:  ujin plan render ./ingest-plan.yaml
#
# Loaded ids: crossref, feed-tech, feed-science, feed-business

defaults:                      # deep-merged UNDER every job; per-job keys win
  source:
    kind: api
    config:
      method: GET
      json_path: items
  transforms:
    - kind: dedupe
      config: { key: id }
  sinks:
    - include: fragments/webhook-sink.yaml   # resolved relative to this file
    - kind: sqlite
  schedule:
    include: fragments/adaptive-hourly.yaml  # the shared hourly schedule

jobs:
  # Plain job — inherits all defaults; only its URL differs.
  - id: crossref
    source:
      config:
        url: "https://api.crossref.org/works?rows=50"

  # Matrix job — fans into one job per feed slug.
  # `id:` is a template so each gets a stable, distinct id.
  - id: "feed-{{ slug }}"
    matrix:
      - { slug: tech }
      - { slug: science }
      - { slug: business }
    source:
      config:
        url: "https://feeds.example.com/{{ slug }}?rows=50"
    sinks:                     # per-feed override: keep the webhook + add a jsonl file
      - include: fragments/webhook-sink.yaml
      - kind: jsonl
        config: { path: "/data/{{ slug }}.jsonl" }
```

### How the layers compose

| Layer | When it runs | Effect |
|---|---|---|
| `include:` fragments | file-load time | inline the fragment in-place |
| `defaults:` deep-merge | per-job | shared keys merged *under* each job (job wins) |
| `matrix:` fan-out | per-entry | one expanded job per variable map |

They run in that order: fragments first, then defaults merged in, then matrix
expands the result. A file that uses none of them loads identically to a plain
list of jobs.

---

## 3. Validate without starting the server

```bash
ujin plan validate ./ingest-plan.yaml
```

Expected output (exit 0):

```
ok  crossref
ok  feed-tech
ok  feed-science
ok  feed-business
```

For CI, use `--json`:

```bash
ujin plan validate ./ingest-plan.yaml --json
```

```json
{
  "ok": true,
  "resolved": ["crossref", "feed-tech", "feed-science", "feed-business"],
  "failed": []
}
```

A non-zero exit means at least one job failed — the `failed` list names it with
an actionable `ujin: …` message. Other valid jobs still appear in `resolved`.

You can validate a whole workflows **directory** the same way:

```bash
ujin plan validate ./workflows/
```

---

## 4. Inspect the fully-resolved jobs

`ujin plan render` prints every fully-expanded `JobSpec` — after all layers — so
you can see exactly what variables were substituted, what defaults were merged in,
and what the server would receive:

```bash
ujin plan render ./ingest-plan.yaml
```

Each job is a YAML document prefixed with `--- # <id>`:

```yaml
--- # crossref
id: crossref
source:
  kind: api
  config:
    method: GET
    json_path: items
    url: https://api.crossref.org/works?rows=50
transforms:
- kind: dedupe
  config: {key: id}
sinks:
- kind: webhook
  config: {url: 'http://localhost:8000/ingest'}
- kind: sqlite
  config: {}
schedule:
  mode: adaptive
  base: 3600
  min: 600
  max: 86400
  jitter: decorrelated
...

--- # feed-tech
id: feed-tech
source:
  kind: api
  config:
    method: GET
    json_path: items
    url: https://feeds.example.com/tech?rows=50
...
```

For a JSON array (same shape as `GET /jobs`):

```bash
ujin plan render ./ingest-plan.yaml --json | python3 -c \
  "import sys,json; specs=json.load(sys.stdin); print(len(specs), 'jobs')"
# 4 jobs
```

---

## 5. Mount and serve

```bash
# env var (container-friendly)
UJIN_INGEST_PLAN=/data/ingest-plan.yaml ujin jobs-serve

# or the --plan flag (overrides the env var)
ujin jobs-serve --plan ./ingest-plan.yaml
```

`GET /health` gains a `plan` block listing what loaded:

```json
{
  "plan": {
    "path": "./ingest-plan.yaml",
    "loaded": ["crossref", "feed-tech", "feed-science", "feed-business"],
    "failed": []
  }
}
```

Restart (or redeploy) the server: because ids are stable, re-loading the plan
**upserts** the same four jobs rather than duplicating them.

---

## 6. The same layers work in a workflows directory

A workflows directory supports the identical layers — `defaults:`, `include:`,
and `matrix:` — per file. The ingest-plan is just the single-file alternative:

```bash
# directory-driven (one file per workflow, fragments in a subdirectory)
ujin jobs-serve --workflows ./workflows

# single-file (all jobs in one YAML, fragments alongside it)
ujin jobs-serve --plan ./ingest-plan.yaml
```

See [WORKFLOWS.md](../WORKFLOWS.md) for the directory-driven version of this
example (`examples/workflows/site-feeds.yaml` + `marketplace-search.yaml`), and
[INGEST_PLAN.md](../INGEST_PLAN.md) for the full reference.
