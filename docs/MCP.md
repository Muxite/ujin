# MCP server — ujin as a tool for agents

`ujin mcp-serve` exposes the scrape orchestrator and the job control plane as
[Model Context Protocol](https://modelcontextprotocol.io) tools, so agents
(Claude Code, Claude Desktop, any MCP client) can scrape pages, inspect
backend capabilities, and manage polling jobs directly.

It reuses the exact wiring of the HTTP services — one in-process
`ScrapeService` (same env config as `:8901`) and one `JobManager` over the
same `UJIN_JOBS_DB` SQLite store as `:8902` — no HTTP hop, no duplicated
logic.

## Install & run

```bash
pip install 'ujin[mcp]'

ujin mcp-serve                  # stdio (the default agents expect)
ujin mcp-serve --http --port 8903   # streamable HTTP transport
```

### Claude Code

```bash
claude mcp add ujin -- ujin mcp-serve
```

Or in `.mcp.json`:

```json
{
  "mcpServers": {
    "ujin": {
      "command": "ujin",
      "args": ["mcp-serve"],
      "env": {
        "UJIN_JOBS_DB": "/data/ujin-jobs.db",
        "OBSCURA_URL": "http://obscura:9222"
      }
    }
  }
}
```

## Tools

### Scraping

| Tool | What it does |
|---|---|
| `scrape_url(url, mode="links", render="auto", force_refresh=False)` | One-shot scrape through the full HTTP → obscura → sitemap/RSS chain. `mode`: `links` / `article` / `structured` / `combined`. `render` pins a backend. Cached unless `force_refresh`. |
| `scrape_feed(url)` | Parse an RSS/Atom feed into items. |
| `discover_site(homepage)` | Find a site's RSS feeds + sitemaps (link tags, robots.txt, well-known paths). |
| `get_capabilities()` | The backend matrix (http/obscura/playwright/selenium) with **live availability** — call this before pinning `render="obscura"`/`"browser"`. |
| `get_metrics()` | Per-host fetch counters + latency percentiles for the session. |

### Jobs (persistent polling pipelines)

| Tool | What it does |
|---|---|
| `list_jobs()` | All jobs with state/schedule/counters. |
| `get_job(job_id)` | Full spec + runtime summary. |
| `create_job(spec)` | Validate + persist a job (`{name, source, transforms?, sinks?, schedule?}` — same shape as the `POST /jobs` body and workflow YAML). |
| `run_job(job_id)` | Poll once now; returns `ok/changed/fingerprint`. |
| `pause_job(job_id)` / `resume_job(job_id)` | Toggle the schedule. |
| `get_job_results(job_id, limit=20)` | Recent changed-poll payloads (the collect buffer). |

## Typical agent flows

**Read a JS-heavy page:**
`get_capabilities()` → obscura available? → `scrape_url(url, mode="article")`
(auto-escalates) → use `article.text`.

**Stand up a monitor:**
`discover_site("https://example.com")` → pick the feed →
`create_job({name, source: {kind: "rss", config: {url}}, sinks: [{kind: "webhook", config: {url}}]})`
→ `run_job(id)` to prime → `get_job_results(id)` later.

## Notes

- The jobs the MCP server sees are the ones in `UJIN_JOBS_DB`. Point it at the
  same path as a running `jobs-serve` to *inspect* shared state, but don't run
  both with schedules active against one SQLite file — prefer one writer.
- Workflow YAML loading (`UJIN_WORKFLOWS_DIR`) stays the responsibility of
  `jobs-serve`; the MCP server reads whatever jobs are persisted.
- Errors come back as soft `{"error": ...}` payloads for unknown ids, and as
  MCP tool errors for exceptions (e.g. fetch failures), so agents can branch.
