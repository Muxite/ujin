# ujin real-world hardening — design and plan

Date: 2026-11-09 · Status: DRAFT, awaiting owner approval · Not committed

## 1. Intent

ujin is meant to be a **plug-and-play, config-driven scraper platform**: a user
points it at a new kind of source by editing YAML or posting instructions to the
API, never by editing ujin's Python. It ships as a container.

Today ujin is feature-rich and well tested *offline*, but:

- its purpose is muddled in the README (two products described as one),
- nothing proves the "no code edits" claim,
- nothing has been measured against the real web (every number comes from a fake origin),
- there is no way to validate a config before it runs.

This work closes those four gaps and turns the results into evidence the owner can
defend in an interview.

## 2. Goals / non-goals / anti-goals

**Goals**
1. Lead with one clear pitch: *"Config-driven scraper platform: change YAML or call the API, never ujin's code."* (owner decision)
2. A **two-tier real-API suite** that produces reproducible, committed results.
3. A **recipe gallery + JSON Schema + `ujin validate`** that makes the no-code claim testable.
4. Rewritten README and a restructured docs set a newcomer can follow in 5 minutes.
5. nexus runs on current ujin, with its per-host source config expressed as ujin YAML.
6. Fix the bugs and config gaps the above uncovers.

**Non-goals**
- No rewrite of the engine, adaptive core, or fallback chain.
- No new source kinds unless a gallery target proves one is unavoidable.
- No changes to the frozen consumer surfaces (awork / hct-site / wordle-max); `tests/test_consumer_contracts.py` must stay untouched.
- Restoring the orchestrator (systemd stays disabled).

**Anti-goals** (failure even if it "works")
- A benchmark nobody can rerun.
- Numbers that imply third-party adoption (the 3 consumers are the owner's own repos).
- Scraping that violates a site's ToS/robots for the sake of a headline number.
- Docs that grew longer but not clearer.

## 3. Decisions

| # | Decided | Chosen | Ruled out, and why |
|---|---------|--------|--------------------|
| 1 | Pitch | Config-driven scraper platform | Poller-first (fits the stated goal less); both-layers (hardest to make "very clear") |
| 2 | Evidence | Real-API success rates, throughput/latency, adaptive-cadence savings, nexus production stats (all four, owner) | — |
| 3 | nexus | Verify, bump pin, AND move per-host source config into ujin YAML (owner) | Verify-only (does not demo the no-code claim); leave alone (no end-to-end proof) |
| 4 | Real-API suite | Two tiers, committed, reproducible | Friendly-only (never exercises the fallback chain); ad-hoc (not defensible) |
| 5 | No-code proof | Gallery + JSON Schema + `ujin validate`, then onboard 10 new targets with zero Python edits and record effort | Docs-only (can hide real gaps) |
| 6 | Codex | Codex implements in worktrees; Claude reviews every diff and runs the gates | Tests/docs-only; reviewer-only |
| 7 | Commit policy | Nothing is committed until the owner says so (assumption, see §9) | — |

## 4. Current state (verified 2026-11-09)

- ujin master `f5e17d9`, version 0.49.0, ~390 commits, 3 branches.
- Working tree has owner's uncommitted edits: `docker-compose.yml` (modified), `workflows/ebay-3090.yaml` (new). **Leave untouched**; do not use eBay in published benchmarks.
- CLI has: doctor, init, serve, sweep, api, scrape, jobs, watch, learned, mcp, obs. **Missing:** validate, bench-real, schema export.
- Docs: 19 files, ~3,000 lines. Recipes: 7. Example workflows: 6.
- nexus: `services/ujin` submodule pinned at `f5e17d9`; compose runs `scrape-serve` on :8901; irene uses `POST /scrape` (links, combined) and `/feed`; seulgi uses `/feed` and `/health`. `irene/config.yaml` still has stale v1/v2-scraper comments and a per-host RSS pin map.
- awork-resume dossier (`backups/awork-resume-library/workspace/dossiers/project-ujin.md`) already holds 77 sourced claims: offline numbers only. **No real-API data exists.**

## 5. Workstreams

Each is a Codex-sized unit with its own worktree, acceptance check, and gates.

### W1 — Config validation (foundation; blocks W3, W4)
- Export a JSON Schema for workflow/job/ingest-plan files from the existing pydantic/dataclass models; publish at `docs/schema/workflow.schema.json`.
- `ujin validate <file|dir>`: parse, resolve defaults/includes/matrix, check kinds against the registry (including plugins), report **file:line** errors, exit non-zero on failure.
- Accept: every file in `examples/workflows` and `workflows/` validates; a corrupted copy of each fails with a useful line number.

### W2 — Real-API suite (`ujin bench-real`)
- Tier 1 (~25 friendly public APIs/feeds, headline numbers): e.g. Crossref, arXiv, GitHub, Hacker News, Wikipedia, SEC EDGAR, USGS, plus RSS feeds. Tier 2 (~8 hostile news/retail sites, reported separately, stresses HTTP→render→sitemap→RSS).
- Targets are YAML under `bench/real/`. Runner records per target: success, status mix, 429/5xx rate, latency p50/p95, which fallback hop won, bytes, extraction field coverage. Raw JSONL committed with run metadata (date, ujin commit, host, network).
- Politeness: honor robots, per-host rate caps, identifiable UA. Hostile-tier targets must be ones whose terms permit automated access of public pages; owner reviews the list before the first run.
- Accept: one command reruns Tier 1 end-to-end and regenerates the summary table.

### W3 — Gallery + gap hunt (the no-code proof)
- Ship ~15 recipes in `examples/gallery/` spanning API, RSS, HTML list, pagination, auth'd, browser, matrix.
- Onboard 10 *new* targets using YAML only. For each, log: time-to-first-data, YAML lines, and **every place a Python edit was needed**. Each such place becomes a bug/gap ticket and is fixed in config-space (new option, plugin hook, or better error), then the target is redone.
- Accept: 10/10 onboarded with 0 Python edits; the log is committed as evidence.

### W4 — Adaptive-cadence study (needs elapsed time; start early)
- Poll ~20 real feeds/APIs of known change rates under (a) fixed interval, (b) ujin adaptive, same request budget window, for ≥72 h. Measure requests saved at equal change-detection recall and detection delay.
- Accept: committed raw data + a script that regenerates the chart. Report honestly if the saving is small.

### W5 — nexus integration
- Express irene's per-host source map as ujin workflows/ingest-plan YAML; irene's ingest stage reads results from ujin instead of owning scraping config.
- Bump the submodule pin only after ujin's consumer-contract tests pass. Clean stale v1/v2 comments.
- Gather production stats from nexus data/logs (articles ingested, failure rate, uptime) — read-only.
- Accept: nexus compose stack boots, irene and seulgi tests pass, ingest produces articles from ujin-defined sources.
- Risk: touches irene's ingest stage; confirm irene/seulgi have a green baseline *before* changing anything.

### W6 — README and docs
- README rewritten around the §1 pitch: what it is, 60-second container quick start, "change the behavior without code" tour, architecture one-pager, measured results (from W2–W4, linked to raw data), honest limits.
- Docs reorganised by task (Run it / Configure it / Extend it / Operate it / Reference), with stale or duplicate pages merged. Every doc code block executed in CI where feasible.
- Containerisation pass: one `docker compose up` path, health checks, a documented volume layout for `/workflows` and `/plugins`, example `.env`.
- Written last for numbers, but the structure/pitch rewrite can start now.

### W7 — Resume evidence pack
- `docs/EVIDENCE.md` (or similar): each metric, how it was measured, command to reproduce, caveats. Formatted so it can be ingested into the awork-resume dossier.
- Respect `restrictions.yaml`: ujin never leads, no third-party-adoption implication, no version strings, obscura is "integrated/bundled" not authored. Interview-defence notes included for each claim.

## 6. Order

```
W1 validate ─┬─> W3 gallery/gap-hunt ─┐
             │                         ├─> W6 docs ─> W7 evidence
W2 bench-real┴─> (Tier1/2 runs) ───────┤
W4 cadence study (start ASAP, runs in background) ─┘
W5 nexus (after W1; baseline check can start now)
```

W2 and W1 can run in parallel. W4's clock should start as soon as W2's harness can poll.

## 7. Codex protocol

- One worktree per workstream; Codex gets a task file with scope, acceptance check, files it may and may not touch.
- Forbidden for Codex: editing `tests/test_consumer_contracts.py`, lowering the coverage floor (90%), loosening the 4× benchmark gate.
- Claude reviews each diff, runs `PYTHONPATH=$PWD pytest` in the worktree, coverage, consumer-contract tests, benchmarks; rejects or sends back. Nothing merges on Codex's say-so.
- Real-network runs (W2/W4) are launched by Claude after the owner approves the target list.

## 8. Success criteria

- `ujin validate` and JSON Schema exist and cover all config surfaces.
- Tier 1 + Tier 2 results committed and rerunnable with one command.
- 10 new targets onboarded with 0 Python edits, gaps fixed.
- Cadence study has ≥72 h of real data.
- nexus runs on ujin-defined sources; stale config removed.
- A new reader can state what ujin is in one sentence after the README's first screen.
- Coverage stays ≥90%; consumer-contract and benchmark gates stay green.

## 9. Stopping here — still open, deliberately

- Exact Tier 1/Tier 2 target lists — **OWNER REVIEW** before first run (ToS/robots risk).
- Output format of bench results (JSONL vs CSV), chart tooling — LATERAL.
- Schema generation approach (pydantic export vs hand-written) — LATERAL.
- Whether to commit/branch — assumed "no commits until told"; REVERSIBLE.
- Cadence-study duration beyond 72 h — REVERSIBLE.
- Publishing results (blog/README badges) — OUT OF SCOPE for now.
- Making the container publishable to a registry (GHCR/Docker Hub) — OUT OF SCOPE unless you want it.
