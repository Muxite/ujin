# Downstream consumers

ujin is vendored as a git submodule by three projects (and descends from
jennie's `scraper-v2`). Each pins a commit, so nothing breaks *until a
submodule bump* — these surfaces are the load-bearing ones, tripwired by
`tests/test_consumer_contracts.py`. Only gain fields/behavior here; renames
and removals require changing the consumer in the same motion.

| consumer | surfaces | where in their tree |
|---|---|---|
| **awork** | `from ujin import CallablePollable, PollEngine` (engine.add/run/sweep); `ujin.fetch.obscura.obscura_available`, `ObscuraFetcher`; `ujin.scrape.config.ScrapeConfig()` (zero required args), `await ujin.scrape.build.build_scrape_service(cfg, scorer=None) -> (ScrapeService, ScrapeComponents, aclose)`, `ScrapeService.scrape(url, *, mode="links"/"article", force_refresh=False, enrich_html_top_n=0, render="auto", actions=None) -> ScrapeResult` (reads `.links` → `NormalizedLink{url, text}`, `.article`); `ujin.sources.discover.discover_sources(http, homepage) -> DiscoveredSources{homepage, rss, sitemap}` (called with its own transient `HttpFetcher`); **`await ujin.jobs.build.build_job_manager(db_path, *, scrape_config=None, hub=None) -> (JobManager, JobStore, ScrapeService \| None, aclose)`** — the primary entry point, replacing a hand-wired JobStore+PollEngine+build_scrape_service+JobManager sequence with one call (`scrape_config` defaults to a zero-args `ScrapeConfig()`; a missing/broken scrape backend degrades `service` to `None`, never raises; `aclose()` closes both); `ujin.jobs.JobManager(engine, store, hub=None, scrape_service=None)` — `.create(spec) -> JobHandle`, `await .run_now(job_id) -> PollResult \| None`; `ujin.jobs.JobStore(path)` — `.results(job_id, limit=50) -> list[dict]`, `.close()`; `ujin.jobs.JobSpec(name, source, id=..., transforms=[], sinks=[], schedule=ScheduleSpec(), enabled=True)` + `SourceSpec(kind="scrape", config)` | `backend/awork/watch.py`, `backend/awork/blocks/sources/website.py`, `backend/awork/apply/ujin_seam.py` |
| **hct-site** | `POST :8901/scrape` body `{url, mode, force_refresh}` → response fields `url, kind, fingerprint, used_renderer, strategy_used, article, links, structured`; `GET :8901/health` (reads `status`); `from ujin import register` (`@register.source` / `@register.sink`); env `UJIN_URL`; Docker targets `ujin`, `ujin-full` | `backend/src/ujin_client.py`, `backend/ujin_plugins/hct_publications.py`, `docker-compose.yml` |
| **wordle-max** | `ujin jobs-serve` (:8902) incl. the workflow YAML shape (`source` / `transforms` / `sinks` / `schedule.cron`), webhook sink, plugin source kinds; env `UJIN_WORKFLOWS_DIR`, `UJIN_JOBS_DB`, `UJIN_BROWSER_ENABLED`; Docker target `ujin-browser` | `docker-compose.yml`, `ingest/workflows/amazon-categories.yaml` |
| **jennie** | nothing active — legacy `scraper-v2` (ports 9000/9001) is dormant and unmigrated; ujin's scrape service is its designated successor | `services/scraper-v2/` |

## Submodule bump checklist

1. `pytest` in ujin — consumer-contract tests green.
2. Push ujin master; note the new SHA.
3. In each consumer: `cd <submodule path> && git fetch && git checkout <sha>`,
   commit the pointer bump.
4. awork: import smoke (`python -c "from ujin import PollEngine, CallablePollable"`;
   `python -c "from ujin.scrape.config import ScrapeConfig; from ujin.scrape.build import build_scrape_service; from ujin.sources.discover import discover_sources; from ujin.jobs import JobManager, JobStore, JobSpec, SourceSpec; from ujin.jobs.build import build_job_manager"`)
   + its own suite (incl. `backend/awork/apply/ujin_seam.py`'s tests).
5. hct-site: `docker compose build ujin && docker compose up ujin` →
   `curl :8901/health` shows `status: ok`; run `backend/tests/test_ujin_client.py`.
6. wordle-max: rebuild `ujin:browser`, start `ingest`, confirm
   `GET :8902/health` lists `amazon-categories` under `workflows.loaded`
   (a registry regression here once broke every workflow transform — that is
   exactly what `test_builtin_transforms_buildable_through_registry` guards).

## Known consumer-relevant changes in 0.4.0

- `:8900 /stats` → `/metrics` (no consumer used it).
- Health responses gained `ok`/`service` everywhere; `:8901` kept `status`.
- `UJIN_API_KEY` now also guards :8900/:8901 — opt-in, keyless deployments
  (hct-site) are unaffected.
- Fixed: builtin transforms built through the registry (the path jobs-serve
  uses) crashed with `'BuildContext' object is not callable` — wordle-max's
  workflow would have failed on its next bump without this.
