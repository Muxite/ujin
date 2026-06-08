# workflows/

Drop ujin **workflow** files here. This directory is bind-mounted into the jobs
container at `/workflows` (see `docker-compose.yml`) and scanned on startup.

Each `*.yaml` / `*.yml` file is one workflow (`source → transforms → sinks →
schedule`, the same shape as a job). The **filename stem is the workflow id**, so
`crossref-papers.yaml` becomes workflow `crossref-papers`, queryable at:

```
GET  /jobs/crossref-papers/content   # the data ujin last obtained
GET  /jobs/crossref-papers/results   # recent buffer
POST /jobs/crossref-papers/run       # run now
```

See `examples/workflows/` for runnable examples and `docs/WORKFLOWS.md` for the
full lifecycle. Custom capabilities go in `../plugins` as `plugin:*` kinds.
