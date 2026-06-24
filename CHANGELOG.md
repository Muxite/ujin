# Changelog

## 0.33.0 — 2026-06-24

- **docs(sync):** Audited README.md, `docs/WORKFLOWS.md`, and `docs/INGEST_PLAN.md` against the full shipped external-config surface — workflow `include:`/`use:` fragments, `defaults:` deep-merge, `matrix:`/`for_each:` fan-out (inline, file-backed `{ from: <path> }`, and cross-product axes), top-level `vars:` defaults, `${VAR}`/`${VAR:-default}`/`${VAR:?message}` env substitution, `UJIN_INGEST_PLAN`/`--plan` single-file loading, `ujin plan validate`/`render`/`diff` CLI subcommands with `--only`/`--exclude`/`--strict`/`--explain`/`--exit-code`/`--env-file`, `GET /plan` read-only introspection, `POST /plan/reload` with `?prune` and `?dry_run`, and `--watch-plan` auto-reload. All three docs surfaces were already in sync with the shipped code. Verified all five example YAML targets (`examples/ingest-plan.yaml`, `examples/vars-defaults.yaml`, `examples/matrix-cross-product.yaml`, `examples/matrix-from-file.yaml`, and `examples/workflows/`) resolve without failures via `ujin plan validate` (exit 0). CHANGELOG confirmed chronologically ordered with no duplicate version headers. No production code changed; test suite unaffected (97.96% coverage).
- **tests(plan-loader): add 20 offline edge-case tests for the plan loader and CLI.** Covers gaps not in prior test files: `include:` pointing to a non-existent file surfaced as a failed plan record; `EnvVarRequiredError` (from `${VAR:?message}` with an unset/empty variable) surfaced as a failed plan record rather than a startup crash; single-entry inline `matrix:`/`for_each:` fanning out to exactly one resolved `JobSpec`; `matrix: []` (empty inline list) in a plan job isolated to `failed[]` while sibling jobs still load; cross-product two-axis matrix with vars substituted into `source`, `sinks`, `transforms`, and `schedule`; deterministic cross-product ids verified stable across repeated loads and independent of axis declaration order; `ujin plan validate --json` `resolved` list containing fully-expanded fan-out ids; and `--only`/`--exclude` patterns applied over matrix-expanded ids via the validate CLI. All tests are strictly additive, hermetic, and deterministic — no network access, no live server.
- **test(registry): 100% line coverage for `ujin/registry.py`.** Added 16 offline, deterministic unit tests covering the `transform()`/`scorer()` decorator methods, the `inspect.signature` TypeError/ValueError fallback in `_build`, `build_transform`/`build_sink`/`build_scorer`, and every built-in source factory body (`http`, `rss`, `site`, `scrape` — both the missing-service ValueError path and the success path — `browser`, `graphql`, `amazon_search` single- and multi-term, `amazon_category`, and `marketplace_search`). All tests are hermetic (no network, monkeypatching for the signature-inspection fallback) and strictly additive — registry behavior and public API are unchanged.

## 0.32.0 — 2026-06-24

- **feat(jobs): top-level `vars:` defaults — fill `{{ var }}` placeholders the environment and matrix leave unset.** Workflow/job files and the INGEST-PLAN now accept an optional top-level `vars:` mapping (variable name → default value) that backfills any `{{ var }}` placeholder nothing else sets. Precedence for a `{{ NAME }}`: a `matrix:`/`for_each:` per-entry value wins, then the process environment (variable `NAME`, if set), then the file-level `vars: NAME` default; an otherwise-unset placeholder is still left verbatim, and the `${VAR}`/`${VAR:-default}` read-time env syntax is unrelated and unchanged. The defaults are env-overlaid and then threaded through the same include/`defaults:` merge and matrix expansion the file already uses, and a whole-value placeholder keeps the default's native type (`"{{ rows }}"` → the integer `50`) exactly like matrix substitution. Strictly additive — a file with no `vars:` key (or an empty one) resolves to byte-for-byte identical job specs as before (regression-guarded). `ujin plan validate` rejects a non-mapping `vars:` with a clean `ujin: …` message and no traceback, routing the offending file into the loader's `failed` list. Added a runnable `examples/vars-defaults.yaml`, 17 offline deterministic tests (`tests/test_plan_vars_defaults.py`) covering vars-resolves-when-unset, env-overrides-vars, matrix-entry-overrides-vars, the full matrix > env > vars chain, `${VAR:-default}` interaction, whole-value typing, unknown-placeholder-verbatim, the non-mapping error across the workflow/plan/CLI paths, and a no-`vars:` regression; documented the block in `README.md`, `docs/WORKFLOWS.md`, and `docs/INGEST_PLAN.md`.
- **docs(sync/0.30-0.31)**: Audited README and `docs/WORKFLOWS.md` against the shipped 0.30-0.31 surface. Added `--exit-code`/`--check` exit-status to the `ujin plan diff` entry in the README CLI table. Added three new sections to `docs/WORKFLOWS.md`: `--only`/`--exclude` job-id selection for `plan validate`/`plan render` on workflow directories (with usage examples); a `plan diff` section covering the `--exit-code`/`--check` CI gate and `--json` output shape; and an `--watch-plan` auto-reload reference for `jobs-serve --plan` file watching. No production code changed.

## 0.31.0 — 2026-06-24

- **feat(jobs): cross-product `matrix:`/`for_each:` — a mapping of axis-name → list-of-values fans out as the cartesian product.** The matrix/for_each expander gains a third input shape alongside the existing inline list-of-variable-maps and file-backed `matrix: {from: <path>}` forms: a mapping of axis-name → list-of-values, fanned into one job per *combination* of axis values (two axes of sizes M and N produce M·N jobs), with each combination's axis vars substituted into the resolved source/transforms/sinks/schedule/name/id. A matrix mapping is read as cross-product axes **unless it carries a `from:` key** (which still selects the file-backed form), so `from` is reserved as an axis name and all existing forms resolve byte-for-byte unchanged (regression-guarded). Each combination without an explicit `id:` template gets a stable, deterministic id derived from its slugified values with the axis names sorted — so it is independent of axis declaration order and identical across repeated `ujin plan render` runs. Per-job provenance reports the producing combination as `{key, index, vars}`, identical in shape to inline-matrix provenance, so `ujin plan render --explain` and `GET /plan` surface it. An empty axes mapping, an empty/non-list axis value, or a colliding generated id raises a clean actionable `ujin: …` error that routes the offending file into the loader's `failed` list (non-zero `ujin plan validate` exit, `failed` entry from the server loader) without aborting resolution of the other jobs. Added a runnable `examples/matrix-cross-product.yaml`, 26 offline deterministic tests (`tests/test_matrix_cross_product.py`), and documented the form in `README.md`, `docs/WORKFLOWS.md`, and `docs/INGEST_PLAN.md`.

## 0.30.0 — 2026-06-24

- **feat(jobs): `GET /plan` — read-only introspection of the active plan/workflows resolution, with per-job provenance.** A new control-plane endpoint returns the set of jobs the running server's active workflows directory and INGEST-PLAN currently resolve to, each paired with its provenance: the originating plan/workflow source file and, for `matrix:`/`for_each:`-expanded jobs, the matrix entry (`key`/`index`/`vars`) that produced it. It re-runs the **same** loaders and provenance plumbing that back `ujin plan render --explain` (`_load_workflows_dir`/`_load_ingest_plan` with `provenance=…`) rather than re-implementing resolution, so the reported ids and provenance match the offline CLI exactly. The route is **purely read-only** — it never creates, mutates, prunes, or reloads a job, so `GET /jobs` and every other endpoint behave exactly as before — and resolution mirrors startup: the workflows dir is scanned unfiltered while the plan honours `--only`/`--exclude` (a now-no-match `--only` degrades to "no jobs selected" instead of erroring). The response is a new `PlanIntrospection` Pydantic model (`plan_path`, `workflows_dir`, `jobs[].{id, provenance}`, `failed[]`); `plan_path` is `null` and `jobs` empty when no plan/workflows are configured, and unresolvable files/jobs surface in `failed` exactly as `GET /health` lists them. Strictly additive — no existing surface changes and a no-config deploy is unaffected. Added offline `TestClient` tests covering a plan exercising defaults/include/matrix fan-out, a workflows-directory plan, the empty/no-active-plan case, the read-only invariant, `--only` selection, and `failed` reporting; documented in `README.md`, `docs/JOBS.md`, and `docs/INGEST_PLAN.md` with request/response examples.
- **feat(plan): `ujin plan diff --exit-code` / `--check` — opt-in CI exit gate for `plan diff`.** Adds an opt-in `--exit-code` flag (alias `--check`) to `ujin plan diff OLD NEW`. When set, the command exits non-zero if the two resolved plans differ in any added/removed/changed job id, and exits zero when they are identical — making it suitable as a CI gate that fails a pipeline when the plan changes unexpectedly. Without the flag, `plan diff` behavior is unchanged: it exits zero whenever both inputs resolve regardless of differences, and `--json` output is byte-for-byte identical whether or not `--exit-code` is passed. Unresolvable inputs continue to produce the existing error exit (non-zero) independently of the flag. Added 7 offline tests covering identical plans (exit zero with and without the flag), differing plans (exit zero without the flag, nonzero with it), the `--check` alias, the `--json` output being unaffected, and that unresolvable inputs still exit non-zero; documented in `README.md` and `docs/INGEST_PLAN.md`.
- **docs(sync):** Repaired CHANGELOG.md — collapsed three duplicate `0.15.0` version headers and the interleaved `0.16.0` header into a single correctly-ordered history; also fixed a duplicate `0.6.0` header and the misplaced `0.8.0` section (which was listed after `0.7.0` rather than before it). Audited `README.md`, `docs/INGEST_PLAN.md`, `docs/WORKFLOWS.md`, and `docs/JOBS.md` against the full shipped external-config surface (`defaults:` deep-merge, `include:`/`use:` fragments, `matrix:`/`for_each:` fan-out, `$UJIN_INGEST_PLAN`/`--plan`, `--env-file`, `--only`/`--exclude` selectors, `--explain` provenance, `POST /plan/reload` with `?prune` and `?dry_run`, and `ujin plan validate`/`render`/`diff`). All surfaces were already in sync. Verified that the example snippets in `docs/INGEST_PLAN.md`, `docs/WORKFLOWS.md`, and `docs/JOBS.md` resolve and run as written against the shipped loaders. No runtime behaviour changed; test suite unaffected.

## 0.29.0 — 2026-06-24

- **`POST /plan/reload?dry_run=true` — opt-in preview of a reload without mutating anything.** The reload endpoint gains a dry-run mode — `?dry_run=true` query param or a `{"dry_run": true}` JSON body, accepted exactly like `prune` — that resolves the configured plan through the **identical** `_load_ingest_plan` resolution (`defaults:`/`include:`/`matrix:`/`${VAR}`) and `--only`/`--exclude` selection as a real reload, then reports the effect **without** touching the job manager: which plan job ids would be `added`, `updated` (resolved spec differs), or `unchanged`, plus — when combined with `prune` — which plan-owned ids would be `removed`. `reloaded` stays `false`, no job is upserted or pruned, and `GET /jobs` / the `/health` plan block are identical immediately before and after the call. Strictly additive: the default (no `dry_run`) reload, with or without `prune`, is byte-for-byte the 0.28 behaviour. With no plan configured the dry run is a non-error no-op. Still behind the optional `UJIN_API_KEY` auth like every mutating route. Added offline tests covering added/updated/unchanged, dry-run + prune, the JSON-body form, and the no-plan no-op; documented in `README.md`, `docs/INGEST_PLAN.md`, and `docs/JOBS.md` with runnable examples.
- **feat(plan): `ujin plan diff OLD NEW` — diff two plans/workflows dirs by resolved job id.** Resolves both inputs with the same loaders as `plan validate`/`plan render` (defaults deep-merge, `include:`/`use:` fragments, `matrix:`/`for_each:` fan-out, and `${VAR}` substitution all expanded), then reports, keyed by final job id, the jobs **added** in `NEW` (`+`), **removed** in `NEW` (`-`), and **changed** (`~`, present in both with a differing resolved spec). Exits zero whenever both inputs resolve — an identical pair prints `no changes`. The per-load `created_at` timestamp is ignored so identical content diffs empty. `--json` emits the same information as a structured object with distinct `added`/`removed`/`changed` groupings (added/removed carry each job's `spec`; changed carries `old` and `new`), and `--env-file` is accepted consistently with the other plan subcommands. An unreadable or schema-invalid `OLD`/`NEW` input exits non-zero with a clean `ujin: …` message and no traceback. Added 19 offline tests (`tests/test_plan_diff_cli.py`) covering added/removed/changed/identical, JSON groupings, workflows-dir and matrix-expanded inputs, `--env-file`, and error inputs; documented in `README.md` and `docs/INGEST_PLAN.md` with runnable examples.

## 0.28.0 — 2026-06-24

- **docs(sync): audit README and docs/ against cycles 0.24-0.27 and the marketplace-externalization surface; fix spurious INGEST_URL warning in example fragment.** Verified README feature list/intro and quickstart, `docs/INGEST_PLAN.md`, `docs/WORKFLOWS.md`, `docs/JOBS.md`, `docs/MARKETPLACE.md`, and `docs/recipes/external-config-flow.md` against the shipped schema (`include:`/`defaults:`/`matrix:`/`for_each:`, `matrix: {from: <path>}`, `$UJIN_WORKFLOWS_DIR` resolution, `--env-file`, `ujin plan validate`/`ujin plan render`, externalized marketplace profiles). All surfaces were already in sync. Fixed a doc–output mismatch: `examples/workflows/fragments/webhook-sink.yaml` used bare `${INGEST_URL}` in a comment (not in the actual config value), which was spuriously expanded by the env-var substitution pass at load time and logged a misleading warning on every `ujin plan validate examples/ingest-plan.yaml` run; changed the comment to plain text so the example now produces the clean output shown in the docs.
- **feat(plan-validate): richer error diagnostics — every failure names the offending source file and job id.** `ujin plan validate` (and the server-side loaders) now include the plan/workflow filename in every `ujin: …` error message: file-level plan failures carry `ujin: <filename>: …`, per-job plan failures carry `ujin: plan job <id> in <filename>: …`, and workflow-directory failures carry `ujin: workflow <filename>: …`. Valid inputs and the success path are byte-for-byte unchanged. `--json` failure records already named the job id; they now also include the filename in the `error` string. Added 8 offline tests covering distinct invalid cases (duplicate job id, malformed `defaults:` type, bad matrix row, missing `include:` target, unresolved `${VAR:?}` in a workflow file, per-job JSON error structure) and asserting that each names the offending file and job id.

## 0.27.0 — 2026-06-24

- **test(external-config): hermetic branch/line gap-fill for `ujin/jobs/app.py` and `ujin/cli.py` — both reach 100%.** Added `tests/test_external_config_coverage.py` (9 offline, deterministic tests) closing every remaining uncovered branch in the external-config loader and CLI paths: `_expand_matrix_entries` `prov.append(None)` for a plain entry in a mixed matrix+plain `jobs:` list (line 431); the `if matrix is not None:` False branch in `_specs_from_workflow_file` when provenance is collected for a non-matrix entry in a jobs-list workflow (line 648→650); `_version()` fall-through to `importlib.metadata` when `ujin.__version__` is `None` (line 47→51); `_cmd_jobs_serve` early return when `--env-file` is missing/malformed (lines 218–220) and the success path reaching `serve()` (line 219→222); `_explain_comment_lines` with an empty provenance dict so the `if src:` False branch fires (line 434→436); `_open_strategy_feedback_ro` re-raising `DatabaseError` as `SystemExit` on a non-database file (lines 601–602); and the `_log` change-event callback body in `_cmd_watch` (line 723). No production behaviour changes; total suite coverage rises from 97.80% to 97.93%.

- **test(product): hermetic branch coverage for `ujin/extract/product.py` — 80% → 96%.** Added `tests/test_product_coverage.py` (83 offline tests) covering: `extract_description` block/meta fallbacks and empty-bullet skipping; `_first_srcset` and the card-image srcset path; `_as_selectors` with None/tuple inputs; `_id_from_href` with None/empty href and additional marketplace patterns (Wayfair, IKEA, Google Shopping); `_currency_from_text` no-symbol default; `_from_jsonld` skip branches (non-dict block, wrong @type, non-dict offers, zero price, ImageObject, absent title/image); card with no valid image; Amazon detail-page price-selector iteration, no-gallery og:image fallback, no-image return-None, URL-without-/dp/, absent rating/review/byline; `_detail_from_jsonld` non-dict block, absent title, list offers, no-image-no-og, protocol-relative image, bad ratingValue, 14+ scalar specs; `_balanced_json` escaped chars and unclosed braces; `_aliexpress_modules` error paths (no-brace, unclosed JSON, invalid JSON, nested walk() shape); `_ali_price_cents` structured-amount fallback chain and formatted-string fallback; AliExpress detail no-title/no-price/no-image, review via `totalValidNumStr`, bad `averageStar`, 14-spec cap, variant fan-out, brand extraction. No production behaviour changes.
- **test(hardening): hermetic coverage for robots, scrape/build, extract/images, extract/contacts.** Added 22 offline tests in `tests/test_hardening.py` closing every uncovered branch in the four near-floor modules: `ujin/robots.py` (same-agent multi-group parse, unknown directive key, empty `Allow:` pattern, `re.error` in `_match`, and all three `_http_fetcher` paths via injected aiohttp fakes), `ujin/scrape/build.py` (`learn_strategy=True` StrategyFeedback construction and both strategy-close paths), `ujin/extract/images.py` (selectolax import failure, per-node inner except, outer-loop except, urljoin exception fallback, and srcset leading-comma edge case), and `ujin/extract/contacts.py` (empty/missing href, empty mailto and tel addresses, urlparse exception in `_is_social_href`, urljoin exception in `_resolve`, and parser.feed exception swallow). All four modules reach 100% line and branch coverage; total suite coverage rises to 96.67%.

## 0.26.0 — 2026-06-24

- **docs(sync): fix stale `plan validate --json` schema examples; guard `examples/matrix-from-file.yaml`.** The `--json` output of `ujin plan validate` includes `warnings` and `strict` fields that were absent from the JSON examples in `docs/INGEST_PLAN.md` (normal-case and missing-path cases) and `docs/WORKFLOWS.md`; corrected both to match actual CLI output. Added five offline tests to `tests/test_examples_plan_validate.py` covering the new 0.25 `examples/matrix-from-file.yaml` (file-backed matrix plan) — validate, render, `--json` forms, and expected ids — so example-file drift is caught at CI time.
- **docs(sync): fix stale `--json` example in `docs/recipes/external-config-flow.md`; add schema-guard tests.** The `plan validate --json` example in the external-config recipe was missing the `warnings` and `strict` fields present in all other doc examples and in the actual CLI output; corrected to include both. Added a note on `--strict` to the recipe. Added three offline tests to `tests/test_examples_plan_validate.py` asserting the `--json` output schema contains all documented fields (`warnings`, `strict`) and that the `strict` flag round-trips correctly — so the `--json` schema is pinned against future drift.
- **`--env-file PATH` for `plan validate`, `plan render`, and `jobs-serve`.** All three commands now accept `--env-file PATH` to load `KEY=VALUE` pairs (dotenv-style: blank lines and `#` comments skipped) and feed them into the existing `${VAR}` / `${VAR:-default}` / `${VAR:?message}` substitution when resolving the plan or workflows directory. Precedence: process environment variables win; the file only fills in variables not already set in the environment. A missing or unparseable file exits non-zero immediately with a clean `ujin: …` message and no traceback. Without `--env-file`, all three commands are byte-for-byte unchanged. Documented in `README.md` and `docs/INGEST_PLAN.md` with a runnable example.
- **`POST /plan/reload?prune=true` — opt-in removal of plan-owned jobs dropped from the file.** The job manager now tracks each running job's **provenance** (`plan`/`preload`/`workflow`/`api`), so a reload can tell INGEST-PLAN-owned jobs apart from API-created and positional-preload jobs. `POST /plan/reload` gains an opt-in prune mode — `?prune=true` query param or a `{"prune": true}` JSON body — that removes plan-owned jobs whose ids are no longer declared by the re-read plan and reports them in a `pruned` list alongside `loaded`/`failed`; pruned jobs disappear from `GET /jobs` and the refreshed `/health` plan block. Strictly additive: with no prune option the reload is upsert-only and the response omits `pruned` entirely — byte-for-byte the 0.24 behaviour. Jobs created via the REST API or the positional `jobs.yaml` preload are **never** pruned even when absent from the plan, and prune is skipped on a file-level read failure so a transient bad file never wipes plan jobs. Still behind the optional `UJIN_API_KEY` auth like every mutating route. Documented in `docs/INGEST_PLAN.md` and `docs/JOBS.md`.

## 0.25.0 — 2026-06-24

- **File-backed `matrix:`/`for_each:` — `matrix: { from: <path> }`.** A matrix/for_each value may now be a `{ from: <path> }` mapping naming an external data file that supplies the list of variable maps, in addition to the existing inline list. The data-file path resolves exactly the way an `include:`/`use:` fragment does — relative to the containing workflow/plan file's directory, then `$UJIN_WORKFLOWS_DIR` (reusing the fragment path-resolution helper) — and the loaded rows drive the same fan-out, `{{ var }}` substitution, and stable-id rules as an inline list, so `matrix: { from: rows.yaml }` produces byte-for-byte the same JobSpecs as inlining `rows.yaml`'s contents. Strictly additive: an inline `matrix:`/`for_each:` list and a file using neither are unchanged. A missing/unreadable data file, or one that is not a non-empty list of variable maps, raises a clear `ValueError` that routes the offending workflow/plan into the loader's `failed` list (`GET /health`) rather than aborting startup. New runnable example `examples/matrix-from-file.yaml` (+ `examples/data/marketplace-searches.yaml`); documented in `docs/WORKFLOWS.md` and `docs/INGEST_PLAN.md`.
- **test(plan-loader): hermetic coverage for include/defaults/matrix/env-var/selector/reload paths.** Added 62 offline, deterministic unit tests in `tests/test_plan_loader_coverage.py` covering: direct `_expand_env` branches (`${VAR}` unset-to-empty, `${VAR:-default}`, `${VAR:?message}` raising); `include:`/`use:` error paths (cyclic self-reference, A→B→A cycle, invalid type, list fragment with sibling keys); `_deep_merge` and `defaults:` precedence (per-job keys win); `matrix:`/`for_each:` fan-out errors (both keys present, non-list rows, non-dict row, duplicate generated id); `_select_jobs` edge cases (`only=[]`/`exclude=[]` early-return, exclude-without-only, wildcard patterns, empty-spec-list raise); `ujin plan validate` and `ujin plan render` CLI paths with `--only`/`--exclude`/`--json`/`--strict`; and `POST /plan/reload` add/update/upsert-only semantics. Lifts `ujin/jobs/app.py` to 100% branch coverage; no production behaviour changes.

## 0.24.0 — 2026-06-24

- **Job-id selection — `--only`/`--exclude` for `plan render`, `plan validate`, and `jobs-serve`.** Three CLI commands now accept `--only PATTERN[,PATTERN…]` and `--exclude PATTERN[,PATTERN…]` (fnmatch-style globs, comma-separated or repeatable) that filter which jobs are processed. Selection is applied **after** full resolution — `defaults:` deep-merge, `include:`/`use:` fragment inlining, `matrix:`/`for_each:` fan-out, and `${VAR}` substitution — so patterns always match the final expanded job id. `--exclude` subtracts from the `--only` set. A `--only` pattern that matches no job exits non-zero with a clean `ujin: …` message and no traceback. With no selector flags, all three commands are byte-for-byte unchanged. `jobs-serve --plan <file> --only <pattern>` preloads only the selected jobs and reflects the filtered set in `GET /health` under `plan.loaded`. Example: `ujin plan render ./ingest-plan.yaml --only 'feed-*' --exclude feed-tech`. See [docs/INGEST_PLAN.md](docs/INGEST_PLAN.md).
- **`POST /plan/reload` — re-read the INGEST-PLAN without restarting.** New authenticated control-plane endpoint on the jobs app (`:8902`) re-reads the configured plan path (`--plan` / `$UJIN_INGEST_PLAN`), re-applies the identical `defaults:`/`include:`/`matrix:`/`${VAR}` resolution as startup (reusing `_load_ingest_plan`), and upserts the resolved jobs by their stable ids into the running job manager — so added/changed jobs become visible via `GET /jobs` and the refreshed `GET /health` plan block with no downtime. Returns a JSON summary mirroring the startup plan-status shape (`{reloaded, path, loaded, failed}`); a single malformed job is reported in `failed` while the rest reload (a reload never aborts). Strictly additive and no-op-safe: with no plan configured it changes nothing and returns `{"reloaded": false, "detail": "no plan configured", …}`, and like every mutating route it sits behind the optional `UJIN_API_KEY` auth (`/health` stays open). Reload only upserts — it never deletes jobs dropped from the file. Documented in `docs/INGEST_PLAN.md` and `docs/JOBS.md`.
- **`${VAR:?message}` required-variable substitution.** The env-var expander now recognises a third form alongside `${VAR}` and `${VAR:-default}`: `${VAR:?message}` expands to the value of `VAR` when it is set and non-empty, and raises a clear actionable error (naming `VAR` and including the optional `message`) when `VAR` is unset or empty. The error surfaces as a `ujin: …` message in `ujin plan validate` (non-zero exit, no traceback) and as a `failed` entry in `GET /health` at serve time, rather than crashing startup for other jobs. `${VAR}` and `${VAR:-default}` behaviour is byte-for-byte unchanged.

## 0.23.0 — 2026-06-24

- **Plan/workflow schema validation — catch misspelled keys.** The plan and workflow loaders now check every resolved mapping (the INGEST-PLAN top level, each plan/workflow job, and each workflow file's top level) against an allowlist of recognized keys — `source`/`transforms`/`sinks`/`schedule`/`id`/`name`/`enabled` plus the additive `defaults`/`include`/`use`/`matrix`/`for_each` — *after* `include:`/`defaults:` resolution. An unrecognized key (e.g. `defaluts:` for `defaults:`) is reported with an actionable message naming the file, the job id when applicable, and the offending key, plus a `did you mean <closest>?` hint when a near match exists. Strictly additive and advisory by default: warnings are logged (and surfaced as `WARN` lines / a `warnings` array by `ujin plan validate`) but never change which jobs load, so a previously-valid plan/workflow is byte-for-byte unchanged. New `ujin plan validate --strict` flag promotes these warnings to failures (non-zero exit) for CI use.
- **docs(readme): surface external-config capabilities in README feature list and quickstart.** Expanded the intro from "two halves" to three parts, adding the declarative jobs control plane with `defaults:`, `include:`/`use:` fragments, `matrix:`/`for_each:` fan-out, and the single mounted INGEST-PLAN (`--plan`/`$UJIN_INGEST_PLAN`) as a top-level feature. Added a "Quick start — jobs and file-driven config" section with runnable `ujin plan validate` / `ujin plan render` commands and a concise description of the three additive config layers.
- **docs(recipes): add end-to-end external-config flow recipe.** New `docs/recipes/external-config-flow.md` walks through the full `defaults:` + `include:` fragments + `matrix:` fan-out pipeline inside a single mounted INGEST-PLAN, with runnable snippets for `ujin plan validate` and `ujin plan render`. Updated `docs/recipes/README.md` index. Added `tests/test_examples_plan_validate.py` to guard example-file freshness: the shipped `examples/ingest-plan.yaml` and `examples/workflows/` must continue to validate and render without errors.

## 0.22.0 — 2026-06-24

- **`ujin plan render` — offline plan inspection.** New `ujin plan render <path>` subcommand resolves and prints every fully-expanded `JobSpec` for an INGEST-PLAN file or a workflows directory — after `defaults:` deep-merge, `include:`/`use:` fragment resolution, `matrix:`/`for_each:` fan-out, and `${VAR}`/`${VAR:-default}` substitution — in human-readable YAML. `--json` emits the resolved specs as a JSON array using the same shape as the REST `/jobs` endpoint. Exits zero when all jobs resolve; non-zero with a clean `ujin: …` message (no traceback) on a missing/unreadable path or any failed job. Reuses the identical loaders as `jobs-serve` and `plan validate`. Strictly additive: `ujin plan validate` and all existing CLI behaviour unchanged.
- **test(coverage): close line/branch gaps in `jobs/app.py`, `scrape/routes_social.py`, and `mcp/__init__.py`.** Added targeted offline tests to cover the `_load_ingest_plan` branches where `failed=None` (file-level and per-job error paths), the `scrape_close()` exception swallow in the jobs lifespan shutdown, the three empty-input 400 guards in the social routes, the corroboration `breaking_score_floor` boundary values (floor=1.0 and floor=0.0), and the `__getattr__` AttributeError in `ujin.mcp`. All additions are tests-only with no behaviour change; overall coverage lifted from 96.00% to 96.16%.

## 0.21.0 — 2026-06-24

- **docs(sync): audit external-config surface; fix INGEST_PLAN.md stable-id rule;
  add `$UJIN_WORKFLOWS_DIR`/`--workflows` to README jobs paragraph; document
  `failed`-entry shape in WORKFLOWS.md.** Corrected the stable-ids section of
  `docs/INGEST_PLAN.md`: a single-entry plan derives id `<plan-stem>` (no index),
  not `<plan-stem>-0` — matching the single-file-vs-jobs-list asymmetry already
  documented in WORKFLOWS.md. Added `$UJIN_WORKFLOWS_DIR` and `--workflows`
  references to README's workflows paragraph so both directory-driven and
  single-file (`--plan`/`$UJIN_INGEST_PLAN`) paths are named alongside each other.
  Added the `{"id","error"}` failed-entry shape to the WORKFLOWS.md health-response
  example so the per-file error-reporting behaviour is fully described.
- **docs(jobs): sync JOBS.md REST surface with shipped ingest-plan loader.** Added
  the optional `plan` field to the `/health` response description and expanded the
  file-driven-jobs cross-reference to name the four external-config capabilities
  (`defaults:` deep-merge, `include:`/`use:` fragments, `matrix:`/`for_each:`
  fan-out, and `--plan`/`$UJIN_INGEST_PLAN` single-file ingest-plan) with links to
  WORKFLOWS.md and the new INGEST_PLAN.md.
### Added
- **`ujin plan validate` — offline plan/workflows validation.** New `ujin plan validate <path>` subcommand validates an INGEST-PLAN file or a workflows directory using the identical loaders as `jobs-serve` (so resolved ids and failures match exactly what the server would produce) without starting the server. Prints each resolved job id (`ok  <id>`) and each failure with an actionable `ujin: …` error message; exits zero when all jobs resolve and non-zero when any fail. A `--json` flag emits `{"ok", "resolved", "failed"}` for CI. A missing or unreadable path exits non-zero immediately with a clean `ujin: …` message (no traceback). Strictly additive: no existing behavior changed.
- **Coverage hardening.** Added 21 offline tests closing gaps in `ujin/adapt/interval.py`, `ujin/adapt/jitter.py`, `ujin/jobs/__init__.py`, `ujin/plugins/loader.py`, and `ujin/registry.py`; all five files now reach 100% line+branch coverage, lifting overall suite coverage from 95.56% to 95.99%.

## 0.20.0 — 2026-06-23

### Added
- **INGEST-PLAN — many jobs from one mounted file.** `jobs-serve` can now load a
  single YAML/JSON **plan** file that declares many jobs at once: a top-level list
  of jobs, or a mapping with a `jobs:` list plus an optional top-level `defaults:`
  block deep-merged under every job (per-job keys win). It reuses the same additive
  layers as workflow files — `include:`/`use:` fragments (resolved relative to the
  plan's directory then `$UJIN_WORKFLOWS_DIR`) and per-job `matrix:`/`for_each:`
  fan-out — and derives a **stable id** per job (explicit `id` wins; otherwise
  `<plan-stem>-<index>` with a per-entry matrix suffix), so re-loading the plan
  upserts rather than duplicates. Resolve the plan from the new `UJIN_INGEST_PLAN`
  env var or the new `jobs-serve --plan PATH` flag (the flag wins); it loads on
  startup alongside the positional `jobs.yaml` preload and the workflows-dir scan.
  A malformed plan (non-mapping/non-list, bad matrix, colliding ids, unreadable or
  cyclic include) yields an actionable `ujin: …` message naming the offending
  file/job and is reported in the `plan` block of `GET /health` (present only when
  a plan is configured) without aborting startup — valid jobs still load. Strictly
  additive: a plan using none of `defaults`/`include`/`matrix` loads to exactly the
  jobs of the equivalent plain list. New example `examples/ingest-plan.yaml`; see
  `docs/INGEST_PLAN.md`.
- **Tests: full offline coverage for `MultiPollable`, `AmazonSearchPollable`, and `MarketplaceSearchPollable`.** Added `tests/test_poll_multi.py` covering all `MultiPollable` branches (list/non-list/None payloads, concurrent polling, failing children, `changed` detection) and new coverage tests in `tests/test_amazon_coverage.py` and `tests/test_marketplace.py` closing every remaining uncovered branch in `ujin/poll/amazon.py` and `ujin/poll/marketplace.py`; all three modules now reach 100% line and branch coverage (total project coverage 95.51%, up from 94.82%).

## 0.19.0 — 2026-06-23

### Added
- **Workflow `defaults:` + reusable fragments.** Workflow files now accept an optional
  top-level `defaults:` mapping that is deep-merged under every job (per-job keys win;
  nested `source`/`schedule`/etc. maps merge recursively, lists replace), and an
  `include:`/`use:` mechanism that inlines a fragment file for a whole job or a
  sub-section (a sink, a transform pipeline, or a schedule). Fragment paths resolve
  relative to the including file's directory then `$UJIN_WORKFLOWS_DIR`; a missing or
  cyclic include fails just that workflow into the `failed` list (see `GET /health`)
  with an actionable error instead of aborting startup. Strictly additive — files using
  neither load byte-for-byte as before, including filename-stem ids and
  `${VAR}`/`${VAR:-default}` substitution. The workflows-dir scan is non-recursive,
  so fragments kept in a subdirectory (e.g. `fragments/`) are never loaded as
  standalone workflows. New example `examples/workflows/site-feeds.yaml`
  (+ `examples/workflows/fragments/`); see `docs/WORKFLOWS.md`.
- **`matrix:`/`for_each:` workflow templates** — a workflow file (or a `jobs:` list
  entry) may carry a `matrix:` key (alias `for_each:`), a list of variable maps, and
  ujin loads it as **one JobSpec per entry**, substituting each entry's variables into
  every `{{ var }}` placeholder across the source, transforms, sinks, and schedule. A
  whole-value placeholder keeps the variable's native type (`min_price: "{{ floor }}"` →
  the integer `500`); an embedded one interpolates `str(value)`; an unknown variable is
  left verbatim. Each generated job gets a **stable, distinct id/name** — an explicit
  `id:` template (e.g. `id: feed-{{ slug }}`) is honored after substitution, otherwise
  `<stem>-<index>` — so reloading the same template upserts the same N jobs instead of
  duplicating them (colliding ids are rejected). Expansion is a clearly-separated step
  that runs after defaults/include resolution and composes with it (vars substitute into
  the merged result). Strictly additive: a file with no matrix key loads to exactly the
  same JobSpec set as before. Example at `examples/workflows/marketplace-search.yaml`;
  documented in `docs/WORKFLOWS.md` and `README.md`.

### Added (generic marketplace engine)
- Absorbed the generic engine improvements from the marketplace development line, keeping
  them site-agnostic: browser fingerprint rotation / stealth-context hardening
  (`fetch/browser.py`), JSON-LD/schema.org `Product` detail extraction (`extract/product.py`),
  href-pattern `source_id` recovery + detail scraping (`poll/amazon.py`), and an opt-in
  per-host **detail-page cache** (`_SeenStore`; `marketplace_search` config `detail_cache`,
  `detail_cache_path`, `detail_cache_ttl_secs`) that skips re-fetching detail for source_ids
  seen within a TTL. No site profiles are baked in — see below.

### docs(sync/marketplace)
- **docs(sync/marketplace)**: Audited README and all `docs/` pages against the shipped marketplace externalization surface. `docs/JOBS.md` was missing the `marketplace_search` source kind entirely — added row with full config key reference and a "no profiles built in" callout. `docs/MARKETPLACE.md` was missing a config reference for job-source parameters (`terms_per_poll`, `max_results`, `engine`, `with_description`, `detail_cache`, `detail_cache_path`, `detail_cache_ttl_secs`) documented only in CHANGELOG — added "Full config reference" table. README and `docs/MARKETPLACE.md` already correctly described profile externalization; no other docs surfaces had drift. No production code changed; test suite unaffected (94.63% coverage, floor 90%).

### Changed (breaking for marketplace consumers)
- **Marketplace profiles are now externally supplied, not built in.** `ujin.poll.marketplace`
  keeps the generic engine (`MarketplaceSearchPollable`) but no longer ships the hardcoded
  `SITE_PROFILES` (amazon/newegg/ebay/walmart). Profiles are loaded from an inline mapping
  (`profiles=`), a file (`profiles_path=`), or `$UJIN_MARKETPLACE_PROFILES` (a YAML/JSON path,
  mountable as a volume) — inline overrides file. An unknown `profile` now raises `ValueError`
  instead of falling back to amazon. New `marketplace_search` source config keys: `profiles`,
  `profiles_path`. The four prior profiles ship as a reference at
  `examples/marketplace_profiles.yaml`; mount or pass it to retain previous behaviour. See
  `docs/MARKETPLACE.md`. This lets the specific scraping config live in the consuming app
  (e.g. wordle-max) rather than inside ujin.

## 0.18.0 — 2026-06-22

### Added
- **Coverage hardening (extract + browser)** — 116 new offline, deterministic unit tests close coverage gaps in `ujin/extract/links.py` (80% → 95%), `ujin/extract/article.py` (78% → 100%), `ujin/extract/structured.py` (82% → 99%), and `ujin/poll/browser.py` (80% → 100%); total suite coverage moves from ~90% to 97%. Tests target error/edge/fallback branches: `_is_boilerplate_text` variants, slop URL/text filters, photo-credit patterns, excluded-element detection (role/aria-label/class), `normalize_url` edge cases, `extract_article_lenient`, `_run_trafilatura` ImportError and generic-exception paths, JSON-LD/OpenGraph/microdata edge inputs, and all four `BrowserPollable` extract modes plus lazy-fetcher init.
- **docs(sync/0.17)**: Audited README and all `docs/` pages against the shipped 0.17 surface — corrected four omissions: (1) `docs/JOBS.md` transforms table was missing the `filter` kind added in 0.17; (2) `docs/LIST_TRANSFORMS.md` intro said "Eight" job kinds but nine are now documented (`filter` added); (3) `README.md` docs-index reference for LIST_TRANSFORMS omitted `filter`; (4) `docs/API.md` abridged `ScrapeResponse` JSON example omitted the `total`/`next_cursor` pagination fields. Also added "feed-URL extraction" to the README intro feature description to reflect the 0.17 `feeds` mode. No production code changed.

## 0.17.0 — 2026-06-22

### Added
- **Contact information extraction** — new `ujin.extract.extract_contacts(html, base_url=None) -> dict` collects email addresses (from `mailto:` hrefs and visible-text patterns), phone numbers (from `tel:` hrefs and international/NANP patterns in visible text), and social/profile links (from `<a>` hrefs pointing to known platforms or carrying `rel="me"`) into a normalized dict `{"emails": [...], "phones": [...], "links": [...]}` with each list de-duplicated in document order; relative hrefs in social links are resolved against `base_url`; text inside `<script>`/`<style>` tags is skipped; empty or malformed input returns `{}` rather than raising. Pure stdlib (`html.parser`, `re`). A new additive `contacts` scrape mode surfaces it: `POST /scrape {"mode":"contacts"}` (or `contacts` inside a `modes` multi-extract list) returns the dict in the new `ScrapeResponse.contacts` field. Strictly additive — every existing mode, field, and default is byte-for-byte unchanged. Documented in `README.md` and `docs/API.md`.
- **`filter` transform** — new built-in that keeps or drops items from a list payload by a configurable predicate over a dotted `key`, supporting operators `eq`, `ne`, `gt`, `lt`, `ge`, `le`, `in`, `contains`, `exists`, and `regex`/`matches`, plus a `negate`/`exclude` flag to invert selection; on a dict payload the whole event is kept or dropped; non-list/non-dict payloads and empty inputs pass through unchanged. Registered as `kind: filter`, discoverable at `GET /kinds`, and documented in `docs/LIST_TRANSFORMS.md`.
- **Declared feed discovery** — new `ujin.extract.extract_feeds(html, base_url=None) -> list[dict]` parses every `<link rel="alternate">` in the document head whose `type` is a recognized feed MIME type (`application/rss+xml`, `application/atom+xml`, `application/feed+json`) into a normalized dict with an absolute `href` (resolved against `base_url`), a lowercase `type`, and an optional `title` when present; identical hrefs are de-duplicated in document order; empty/malformed input returns `[]` rather than raising. Pure stdlib (`html.parser`). A new additive `feeds` scrape mode surfaces it: `POST /scrape {"mode":"feeds"}` (or `feeds` inside a `modes` multi-extract list) returns the dicts in the new `ScrapeResponse.feeds` field. Strictly additive — every existing mode, field, and default is byte-for-byte unchanged. Documented in `README.md` and `docs/API.md`.
- **docs(sync/final)**: Final docs-sync pass — two omissions corrected and full audit confirmed inline. (1) `docs/JOBS.md` browser source row was missing the `headless?` config key and the valid `extract` values (`links|article|structured|raw`) — both now listed, matching `BrowserPollable.__init__`. (2) `docs/BROWSER.md` job-source config example was missing `"headless": true` — added with a "set false for headed debugging" note, matching the same constructor default. All other doc surfaces verified against the 0.16.0 code: `docs/API.md` — `POST /scrape` field table (`render`, `actions`, `page_size`, `cursor`), `UJIN_LEARN_STRATEGY`/`UJIN_STRATEGY_DB` env vars, `html` multi-extract mode, and all seven response fields (`links`, `article`, `structured`, `tables`, `images`, `metadata`, `html`) present and correct; `docs/JOBS.md` — all 12 transform kinds (`select` `regex` `template` `dedupe` `chunk` `flatten` `sort` `limit` `rename` `aggregate` `unique` `fill`), all 9 source kinds (`http` `site` `rss` `api` `graphql` `command` `scrape` `browser` `plugin:<name>`), and all 8 sinks (`webhook` `forward` `ws` `jsonl`/`file` `stdout` `sqlite` `csv` `plugin:<name>`) documented; `docs/LIST_TRANSFORMS.md` — all 8 list-reshaping transforms + `csv` sink documented with YAML examples; `docs/ADAPTIVE.md` — `ujin learned` CLI, `--host`/`--strategy-db`/`--json` flags, `SiteStore.hosts()`, and all adaptive symbols referenced (`SiteStore`, `HostRecord`, `derive_signals`, `PolicySignals`, `SignalAdvisor`, `StrategyFeedback`, `StrategyOutcome`, `LearnedRateLimiter`, `RobotsPolicy`, `RobotsCache`) present and accurate; `docs/BROWSER.md` — exists with full content, all recipe actions match `BrowserFetcher` implementation; README — feature list (GraphQL + browser-driven recipes), adaptive-learning quickstart (`adaptive=True`, `site_store_path`, `respect_robots=True`, `robots_ttl`, `robots_fetcher`), CLI section (all 11 subcommands including `doctor`, `init`, `learned`), and all runnable examples (`from ujin import PollEngine, HttpPollable, CallablePollable, CommandPollable`; `from ujin.adapt import SiteStore, derive_signals, LearnedRateLimiter`) verified importable against current `ujin/__init__.py` and `ujin/adapt/__init__.py`.
- **docs(api)**: Reconciled `docs/API.md` against the shipped 0.16.0 surface: added `render`, `actions`, `page_size`, and `cursor` to the `POST /scrape` request-field table (all four are accepted by `ScrapeRequest` and referenced in prose, but were absent from the reference table); added `UJIN_LEARN_STRATEGY` and `UJIN_STRATEGY_DB` to the configuration table (both ship via `ScrapeConfig.from_env()` and were mentioned in `README.md` but missing from the API config reference).
- **docs(sync)**: Comprehensive audit of `README.md` and all `docs/` pages against the shipped 0.16.0 surface. Added "GraphQL endpoints" and "browser-driven interaction recipes" to the README intro feature list — both `GraphQLPollable` and `BrowserPollable` have shipped in `ujin.poll` (since 0.15.0 and 0.5.0 respectively) but were absent from the top-level feature pitch. Added `BrowserPollable` to the README "How it works → Roles" list alongside the other pollable classes.

## 0.16.0 — 2026-06-22

- **docs-sync** — audited all docs against the 0.15.0 shipped set; added `graphql` to the poller-control `kind` list in `docs/API.md`; added `unique`/`fill` to the `LIST_TRANSFORMS` reference in `README.md`; and added `aggregate`, `unique`, and `fill` rows to the transforms table in `docs/JOBS.md` so every built-in transform kind is discoverable from the job reference.
- **HTML head-metadata extraction** — new `ujin.extract.extract_metadata(html, base_url=None) -> dict` parses page-level head metadata into one flat, normalized summary: `title`, meta `description`, `canonical` URL, `language` (`<html lang>`), optional `author`/`published`/`modified`/`favicon`, plus the OpenGraph (`og:*`) and Twitter-card (`twitter:*`) fields collected under `og`/`twitter` sub-dicts with the prefix stripped. `canonical`, `favicon`, and og/twitter URL values are resolved against `base_url`; flat `title`/`description` fall back to `og:title`/`og:description` when absent; empty/malformed input returns `{}` rather than raising. Pure stdlib (`html.parser`), and a deliberately flat *complement* to `extract_structured` (it does not duplicate JSON-LD/microdata). A new additive `metadata` scrape mode surfaces it: `POST /scrape {"mode":"metadata"}` (or `metadata` inside a `modes` multi-extract list) returns the summary in the new `ScrapeResponse.metadata` field — under the `extracts` map for multi-extract requests. Strictly additive — every existing mode, field, and default is byte-for-byte unchanged. Documented in `README.md` and `docs/API.md`.

## 0.15.0 — 2026-06-22

### Added
- **HTML image extraction** — new `ujin.extract.extract_images(html, base_url=None) -> list[dict]` parses every `<img>` on a page into one normalized dict with an absolute `src`, an `alt` string, and an optional integer `width`/`height` and `title` when present. Relative srcs are resolved against `base_url`; lazy-load `data-src`/`data-original` and the first `srcset` candidate are honored (a `data:` placeholder is skipped whenever a real src exists for the same image); identical srcs are de-duplicated in document order; and empty/malformed input returns `[]` rather than raising. A new additive `images` scrape mode surfaces it: `POST /scrape {"mode":"images"}` (or `images` inside a `modes` multi-extract list) returns the dicts in the new `ScrapeResponse.images` field — under the `extracts` map for multi-extract requests. Strictly additive — every existing mode, field, and default is byte-for-byte unchanged. Documented in `README.md` and `docs/API.md`.
- **`unique` and `fill` transforms** — `unique` drops duplicate items from a list payload by a dotted `key` (or whole-item identity when key omitted), preserving first-occurrence order and passing non-list payloads through unchanged; `fill` ensures named dotted fields exist on a dict payload or each dict in a list-of-dicts, setting a per-path or shared default without overwriting existing non-None values and passing non-dict items through unchanged. Both are discoverable at `GET /kinds` and documented in `docs/LIST_TRANSFORMS.md`.
- **`graphql` source kind** — new `GraphQLPollable` POSTs a configured `query` (plus optional `variables`, `headers`) to a `url` and fingerprints events narrowed from a dotted `data_path` in the JSON response, reusing the same aiohttp client/timeout path as `ApiPollable`. A GraphQL `errors` array, non-200 status codes, and network exceptions are all surfaced as poll failures without crashing the poll loop. Registered as `kind: graphql` in YAML targets and the jobs control plane.

## 0.14.0 — 2026-06-22

### Added
- **`ujin learned` CLI + `SiteStore.hosts()`** — a new additive `SiteStore.hosts() -> list[str]` read method enumerates every host persisted in the store (sorted, never mutating), and a new `ujin learned [DB_PATH] [--host HOST] [--strategy-db PATH] [--json]` subcommand opens an existing `SiteStore` read-only and prints, per host, the learned state: recommended interval (via `ujin.adapt.derive_signals`), concurrency factor, penalty/backoff (health, cooldown, rate-limited), last observed status/latency, and any observed `Crawl-delay`; with `--strategy-db` it also shows `StrategyFeedback.recommend(host)`. Defaults to a human-readable table, `--json` emits machine-readable output, and `--host` filters to one host. A missing/empty DB path (or a missing `--strategy-db`) fails with a clean actionable `ujin: ...` message and non-zero exit (no traceback); an existing-but-empty store prints a friendly note. Strictly additive — every existing public name and CLI subcommand is unchanged. Documented in `README.md` and `docs/ADAPTIVE.md`.
- **test(cov-social-discover)**: Added 8 fully-offline unit tests closing remaining branch/line gaps in `ujin/sources/social/x_trends.py` (lines 48, 98, 100-102, 108, 111, 114) and `ujin/sources/discover.py` (lines 82-83, 106-107, 114, 123) — `x_trends.py` reaches 100% line coverage, `discover.py` reaches 98%; total suite coverage rises to 96%.

## 0.13.0 — 2026-06-22

- **perf(bench)**: Added `benchmarks/test_extract_throughput.py` measuring single-process CPU-bound extraction throughput for `extract_headline_links`, `extract_article`, `extract_structured`, and `extract_tables` (events/sec and ms/page); per-poll cost (all four extractors, fetch excluded) recorded in `baseline.json` at ~7.1 ms/page (~140 pages/sec ceiling). New **Multiprocessing (Track 3) gate** section in `docs/PERFORMANCE.md` reports the measured ceiling and explicit go/no-go recommendation: Track 3 is not justified for polling workloads (≈17 pages/sec, 14× below the ceiling) and is only warranted above ~140 pages/sec sustained fetch rate (full extraction) or ~815 pages/sec (links-only mode).
### Added
- **test(social-source-coverage)**: Added 10 fully-offline unit tests closing the remaining branch gaps in `ujin/sources/social/_syndication.py` — text/html response where `json.loads` succeeds but returns a non-dict (list/number) falls through to `_from_html` (branch 82→86); `_from_json` with empty results and a non-string `body` value returns `[]` directly (branch 114→116); `_from_html` skips tweet nodes with valid permalinks but whitespace-only text (line 134 `continue`); and `_from_html` stops collecting once the `count` ceiling is hit (line 137 `break`). All four target modules (`mastodon.py`, `twitter.py`, `_nitter.py`, `_syndication.py`) now report 100 % line coverage; total suite coverage rises from 95.48 % to 95.56 %.

## 0.12.0 — 2026-06-22

### Added
- **HTML table extraction** — new `ujin.extract.extract_tables(html) -> list[dict]` parses every `<table>` on a page into one dict per data row, keyed by the table's header cells (a first row carrying `<th>`) or positionally (`col0`/`col1`…) for header-less tables. `colspan`/`rowspan` are expanded so each logical cell lands in its grid slot, nested tables are parsed as their own rows (their text never leaks into the enclosing cell), and empty/malformed input returns `[]` rather than raising. A new additive `tables` scrape mode surfaces it: `POST /scrape {"mode":"tables"}` (or `tables` inside a `modes` multi-extract list) returns the rows in the new `ScrapeResponse.tables` field — under the `extracts` map for multi-extract requests. Strictly additive — every existing mode, field, and default is byte-for-byte unchanged.
- **`aggregate` transform** — new built-in kind that groups a list payload by a dotted `by` key and emits one dict per group with `count` plus optional `sum`/`min`/`max`/`collect` aggregates over configurable dotted `fields`; supports a separate `out` path; non-list and empty payloads pass through unchanged. Discoverable at `GET /kinds`.
- **test(poll-coverage)**: Added 10 offline unit tests covering previously-uncovered error/edge branches in the live poll subsystem — empty-argv `ValueError` (command.py:25), `asyncio.TimeoutError` timeout path (command.py:42-44), generic subprocess exception (command.py:47-48), feedparser `ImportError` (rss.py:23-24), `parse_feed` exception (rss.py:29-30), `decide_changed(None, ...)` short-circuit (base.py:79), bytes/bytearray fingerprint branch (base.py:26), `aiohttp` `ImportError` (api.py:53-54), request-level network exception (api.py:72-73), and `render=True` ObscuraFetcher path (site.py:54-58); all five targeted files reach 100 % line coverage and total suite coverage rises from 95.43 % to 95.50 %.

## 0.11.0 — 2026-06-22

### Added
- **Opt-in strategy-feedback loop in the scrape service** — `ScrapeConfig(learn_strategy=True, strategy_db=...)` constructs a durable `ujin.adapt.StrategyFeedback` (built/closed by `build_scrape_components`; empty `strategy_db` → ephemeral `:memory:`). When on, the `auto` backend path biases the first `(backend, render_mode)` it tries toward the host's proven-best `recommend()`, skips a recommendation flagged by `is_penalized()` (via an optional injected `SiteStore`), and records every fetch outcome with `record(host, backend, render_mode, ok, latency)` so the loop closes. Strictly additive and off by default — a no-config scrape is byte-identical to before.
- **`PollEngine(respect_robots=True)`** — when `adaptive=True` is also set, automatically builds a `RobotsCache` (injectable `robots_fetcher`, configurable `robots_ttl`, 1 h default) and wires it into the engine's `robots=` hook on `LearnedRateLimiter`: `Crawl-delay` becomes a hard floor on the learned per-host interval, and any URL whose path is disallowed is silently skipped — counted as a poll but not a failure so backoff and penalty logic are unaffected. Off by default; the pre-existing engine/poll path is byte-identical when the flag is unset.
- **test(cli)**: Added 9 tests covering previously-uncovered `ujin/cli.py` paths — `_version()` exception/metadata-fallback/`"unknown"` branches (lines 48-59), the YAML-error-without-`problem_mark` branch (line 99), `_cmd_obscura_build` success and missing-Cargo.toml paths (lines 200-216), and `_cmd_watch` callback/webhook/selector+render paths (lines 299-319); `cli.py` coverage rises from 82 % to 99 % and total suite coverage from ~95 % to 95.43 %.

## 0.10.0 — 2026-06-22

### Added
- **Opt-in adaptive poll engine** — `PollEngine(adaptive=True)` wires the
  already-shipped Track-1 governor into the live loop: the engine constructs a
  per-process `SiteStore` + `LearnedRateLimiter`, paces each poll through the
  async `acquire(host)` gate, floors every target's next interval by
  `interval_for(host)`, and persists each response via `observe(...)` so a 429
  durably backs the host off and a restarted process resumes calibrated. The
  store path (`site_store_path`, default in-process `:memory:`),
  `adaptive_base_interval`, and an optional `robots` adapter are configurable, and
  the limiter shares the engine's injectable `clock`/`sleep`. Strictly additive
  and off by default — with the flag unset no `SiteStore`/limiter is built, no
  extra I/O happens, and the poll path is byte-identical to before.
- **scrape multi-URL batch**: `POST /scrape` now accepts an optional `urls` list so one request scrapes several URLs and returns one result per URL under a new additive `batch` list (in request order); the top-level fields mirror the first URL's result. The URLs are fetched concurrently with a bounded concurrency cap — `ScrapeService.scrape_urls()` fans out the per-URL `scrape()` calls under an `asyncio.Semaphore` (`batch_max_concurrency`, env `BATCH_MAX_CONCURRENCY`, default 8) and isolates per-URL failures as `kind='error'` entries so one failing URL never sinks the batch. The batch form is single-`mode` (the `modes` multi-extract map and `page_size`/`cursor` pagination are not applied per URL) and is bounded by `batch_max_items` (default 64). Omitting `urls` keeps the classic single-`url` behaviour byte-for-byte unchanged.
- **docs/ADAPTIVE.md** — end-to-end user guide for the durable adaptive-learning
  subsystem (SiteStore/HostRecord, derive_signals/PolicySignals/SignalAdvisor,
  StrategyFeedback/StrategyOutcome, LearnedRateLimiter, ujin.robots); surfaces all
  adaptive symbols in the README.md feature list.

## 0.9.0 — 2026-06-22

### Added
- **Learned rate governor** (`ujin/adapt/rate.py`, pure stdlib) —
  `LearnedRateLimiter(store, robots=None, *, base_interval=0.0, clock=..., sleep=...,
  max_concurrency=8)` composes `derive_signals(record)` output
  (`recommended_interval`, `concurrency_factor`, `rate_limited`, `cooldown_secs`) via
  the `SignalAdvisor` bridge with an optional robots `Crawl-delay`, backed by the
  existing `AdaptiveInterval` / `AIMDLimiter` / `TokenBucket` primitives.
  `interval_for(host)` / `concurrency_for(host)` report the effective cadence and
  concurrency (the interval never below `max(observed Crawl-delay,
  robots.crawl_delay(host))`); the async `acquire(host)` gate (also `async with`)
  paces a per-host token bucket and caps in-flight requests; `observe(host,
  status=..., latency=..., error=...)` feeds each response back into the store and
  the in-process controllers so a 429 raises the interval and throttles concurrency
  while clean responses relax both toward baseline. Persisted state warm-starts the
  controllers on restart. Exported additively from `ujin.adapt`; opt-in and wired
  into nothing by default — the scrape/poll path is unchanged unless the limiter is
  explicitly used. `crawl_delay` and the host-policy signals now drive this governor.
- **scrape multi-extract**: `POST /scrape` now accepts an optional `modes` list (`links`/`article`/`auto`/`structured`/`html`) so one request fetches the page once and returns a result per mode under a new additive `extracts` map (keyed by mode); the top-level fields mirror the first listed mode. Backed by `ScrapeService.scrape_multi()`, which runs each mode over the already-fetched body and isolates per-mode failures as `kind='error'` entries so one failing mode never sinks the others. The `html` mode returns the raw fetched HTML in the new `html` response field. Omitting `modes` keeps the classic single-`mode` behaviour byte-for-byte unchanged.

## 0.8.0 — 2026-06-22

### Added
- **`StrategyFeedback` / `StrategyOutcome`** (`ujin/adapt/strategy.py`, pure stdlib) — a durable per-host, per-strategy outcome store on SQLite. A *strategy* is a `(backend, render_mode)` pair. `StrategyFeedback(store=':memory:', clock=time.time)`: `record(host, strategy, *, ok, latency)` is an atomic serialized upsert (counters accumulate, latency gauges overwrite, `last_seen` stamped from the injectable clock); `recommend(host)` returns the highest-success-rate known strategy deterministically (ties broken by attempts then lexicographic order), or `None` for an unseen host; `is_penalized(host, strategy, record)` is a pure no-I/O helper that returns `True` when `derive_signals(record).rate_limited` or `health` is low; `close()` runs a truncating `wal_checkpoint`. Reuses the WAL-mode / `synchronous=NORMAL` durability pattern from `SiteStore`. Both names exported additively from `ujin.adapt`; opt-in only — not wired into any default scrape/poll path. Input layer for future learned strategy-selection.
- **tests**: Added `tests/test_jobs_coverage.py` (38 offline tests) raising per-file coverage of `ujin/jobs/app.py` to 99%, `ujin/jobs/pipeline.py` to 100%, `ujin/jobs/cron.py` to 100%, and `ujin/jobs/transforms.py` to 98%, lifting TOTAL coverage from ~91% to 95%.
- **Host policy signals** (`ujin/adapt/signals.py`, pure stdlib) — a deterministic
  interpretation layer over `SiteStore`/`HostRecord`. `derive_signals(record, *,
  base_interval=0.0, robots_crawl_delay=None)` returns a frozen `PolicySignals`
  (`recommended_interval`, `cooldown_secs`, `should_cooldown`, `rate_limited`,
  `concurrency_factor`, `health` in 0..1) doing no I/O: a 429 (counter or last
  status) sets `rate_limited`, raises the interval and throttles concurrency;
  `recommended_interval` is never below `max(crawl_delay, robots_crawl_delay)`;
  rising `error_count` lowers `health` and raises `cooldown_secs`; a clean record
  is pristine (`health==1.0`, no cooldown, full concurrency, interval ==
  `base_interval`). `SignalAdvisor(store)` is a read-only bridge whose
  `for_host(host)` reads `store.get(host)` and derives signals without mutating it.
  Exported additively from `ujin.adapt`; opt-in and wired into nothing by default —
  it is the input layer the planned strategy-feedback and learned-rate-limit units
  consume.
- **Test coverage for social sources and jobs client** — fixture-driven offline unit tests for `mastodon.py` (47%→100%), `twitter.py` (44%→100%), `jobs/client.py` (67%→100%), and `sitemap.py` (79%→100%); total suite coverage rises to 88.9% (floor 85%).
- **Coverage gap-fill** — offline tests for `poll/__init__` lazy imports, `_nitter.nitter_posts` (success/failure/cooldown paths), and `_syndication.syndication_posts` (JSON/HTML/error paths); closes the 87%→88% gap flagged in prior review.
- **Scrape subsystem coverage** (`tests/test_cov_scrape.py`) — 49 offline fixture-driven tests raising `app.py` 67%→97%, `build.py` 75%→100%, `config.py` 76%→100%, `host_overrides.py` 78%→97%, `service.py` 83%→97%; total suite coverage rises to 91.4% (floor 89%).

## 0.7.0 — 2026-06-22

- **tests**: Added `tests/test_cov_trends_mcp.py` (35 offline tests) raising per-file coverage of `ujin/trends/corroboration.py`, `ujin/trends/scorer.py`, `ujin/mcp/server.py`, `ujin/service.py`, and `ujin/sources/social/x.py` to ≥99% each, lifting TOTAL coverage from ~89% to 90.7%.

## 0.6.0 — 2026-06-22

Additive only — no public symbol, CLI subcommand, flag, env var, response field,
or Docker target was renamed or removed, so the three consumer-contract surfaces
(awork / hct-site / wordle-max) stay frozen and green.

### Added
- **`SiteStore` / `HostRecord`** (`ujin/adapt/site_store.py`, pure stdlib) — a
  durable per-host observed-state store on SQLite. Persists last status,
  p50/last latency, error count, 429 count, observed `Crawl-delay`, the adaptive
  interval, and `last_seen` so a fresh process resumes calibrated, polite
  polling. `SiteStore(path=':memory:', clock=time.time)`; `get(host)` returns a
  zero-valued `HostRecord` for unknown hosts; `record(host, **signals)` is an
  atomic serialized upsert (counters accumulate, gauges overwrite, `last_seen`
  stamped from the injectable clock); `close()` runs a truncating
  `wal_checkpoint`. Reuses the disk cache's WAL-mode / `synchronous=NORMAL`
  durability pattern. Both names are exported from `ujin.adapt`. This is the
  foundation other Track-1 adaptive units consume.
- **`ujin.robots`**: `RobotsPolicy` parses robots.txt into per-User-agent groups with Allow/Disallow longest-match precedence, `*` and `$` wildcard handling, `Crawl-delay` extraction, and `Sitemap:` directive collection; malformed/empty/missing file → allow-all. `RobotsPolicy.is_allowed(path, agent='*') -> bool` and `RobotsPolicy.crawl_delay(agent='*') -> float | None` are pure methods over already-parsed text. `RobotsCache(ttl, fetcher, clock)` adds a TTL fetch+cache layer with injectable fetcher and clock for deterministic tests; opt-in only — default scrape/poll behavior is unchanged unless `RobotsCache` is explicitly used. `crawl_delay()` values are a future input to the learned-rate-limit system (`ujin.adapt.concurrency`).

## 0.5.0 — 2026-06-17

Feature + performance + developer-experience cycle. Every change is **additive** —
no public symbol, CLI subcommand, flag, env var, response field, or Docker target
was renamed or removed, so the three consumer-contract surfaces
(awork / hct-site / wordle-max) stay frozen and green.

### Added
- **List-reshaping transforms** (`ujin/jobs/transforms.py`, pure stdlib):
  - `flatten` — fan a list payload into one event per item (inverse of `chunk`),
    with an optional `index` field; non-list payloads pass through.
  - `sort` — sort a list payload by a dotted `key` (or natural order), `reverse`
    optional; missing/uncomparable values sort last without raising.
  - `limit` — cap a list payload to the first/last N items (`count`, `from`).
  - `rename` — remap dict keys (`mapping`) across a dict or list-of-dicts;
    `drop_missing` materializes absent keys as null.
- **`csv` sink** (`ujin/jobs/sinks.py`, pure stdlib) — append event rows to a
  CSV/TSV file with auto header on create, explicit-or-inferred (and then
  locked) `columns`, configurable `delimiter` / `path_in_event`; non-dict items
  are skipped and a no-row event is a silent no-op.
- All five kinds are additive, registered as built-ins, discoverable at
  `GET /kinds`, and documented in docs/LIST_TRANSFORMS.md.
- **`ujin doctor`** — reports which fetch backends (http/obscura/playwright/
  selenium) and optional Python extras are installed, what each unlocks, and the
  exact `pip install` to enable a missing one. Reuses
  `ujin/fetch/capabilities.py`.
- **`ujin init [targets.yaml]`** — scaffolds a commented, ready-to-run starter
  `targets.yaml` (HTTP page, RSS feed, JSON API, shell command). `-f/--force`
  overwrites; refuses to clobber otherwise.
- **`ujin --version`** — prints the installed version.
- Usage examples in `--help` for every subcommand (epilogs), clearer top-level
  help, default-value hints on flags, and `metavar`s.
- README: a 60-second quickstart and a Troubleshooting section mapping each
  common error to its fix; `ujin doctor` referenced from docs/BACKENDS.md.
- CLI tests for `doctor`/`init`/`--version` and every actionable error path.

### Changed
- **Disk cache (SQLite) runs in WAL mode with `synchronous=NORMAL`.** Per-put
  commits no longer fsync the whole database file, lifting the per-put commit
  cost from ~1.3 ms to ~20 µs (~49x) and the put+get-via-`to_thread` roundtrip
  from ~1.45 ms to ~0.12 ms — raising the durable-write ceiling from ~600 to
  ~40k writes/s. The public `DiskCache` API and its durability contract are
  unchanged: committed rows survive process death and reopen (new tests
  `test_disk_durable_across_reopen_without_clean_close`,
  `test_disk_close_checkpoints_wal`). `close()` now runs a truncating
  `wal_checkpoint` so the on-disk file stays self-contained after shutdown.
  New benchmark `test_disk_cache_put` isolates the commit path; the
  `disk_cache_roundtrip` async baseline was re-recorded.
- **Actionable CLI errors** (no tracebacks; clean `ujin: …` messages):
  - missing targets file → names the path + suggests `ujin init`;
  - invalid YAML → names the file **and line/column**;
  - non-mapping document or target entry → explains the expected shape;
  - unknown source kind → lists the valid kinds;
  - missing required config key (e.g. `url`) → names the key.

## 0.4.0 — 2026-06-10

Hardening release: the test/coverage/benchmark infrastructure, API
normalization, the MCP server, and the backend capability matrix.

### Fixed
- **Builtin transforms built through the registry crashed** with
  `'BuildContext' object is not callable` — every workflow/job using
  `select`/`dedupe`/etc. through `JobManager` was broken. (The old tests
  called `jobs.transforms.build_transform` directly and missed it.)
- `render="http"` on a thin page now returns the thin link-set instead of
  discarding the body and failing.
- `.coverage` accidentally tracked in git; now ignored.

### Added
- **MCP server** (`ujin[mcp]` extra): `ujin mcp-serve` exposes
  scrape/jobs as agent tools over stdio or streamable HTTP — `scrape_url`,
  `scrape_feed`, `discover_site`, `get_capabilities`, `get_metrics`, and the
  job lifecycle (`list/get/create/run/pause/resume/get_job_results`).
  See docs/MCP.md.
- **Backend capability matrix** (`ujin/fetch/capabilities.py`) +
  `GET :8901/capabilities` with live availability for
  http/obscura/playwright/selenium. Human version: docs/BACKENDS.md.
- **Benchmark harness** (`benchmarks/`): pytest-benchmark sync paths + a
  custom async runner gated at 4x the committed `baseline.json`
  (`make bench`, `make bench-record`). Findings: docs/PERFORMANCE.md.
- **CI** (GitHub Actions): 3.11/3.12 matrix, offline suite with coverage
  gate (`fail_under=85`, branch coverage), separate benchmark job. No
  obscura build, no browser downloads.
- ~250 new tests (now ~440, fully offline, <10 s) including consumer-contract
  tripwires for awork / hct-site / wordle-max (docs/CONSUMERS.md) and shared
  fixtures (fake aiohttp origin, full-protocol FakePage, obscura stub binary,
  HTML corpus) — docs/TESTING.md.
- New docs: ARCHITECTURE, TESTING, CONSUMERS, BACKENDS, PERFORMANCE, MCP.

### Changed (breaking where noted)
- Health responses normalized to `{ok, status, service, ...}` on all three
  services (additive everywhere; `:8901` keeps `status`).
- **Breaking:** `:8900 GET /stats` renamed to `GET /metrics` (no known
  consumer; the poller control surface had none).
- `UJIN_API_KEY` now guards **all** services (was :8902 only). Opt-in via
  env; `/health` stays open. `ApiKeyMiddleware` moved to `ujin.auth`
  (`ujin.jobs.auth` remains as a shim).
- Stable import surfaces declared: `ujin.scrape` exports `ScrapeService`,
  `ScrapeResult`, `build_scrape_service`; `ujin.jobs` exports `JobManager`;
  everything else is internal.

## 0.3.0

File-driven workflows (setup → collect → serve), plugin system, obscura
submodule, scrape/jobs services, containerization. (Pre-changelog.)
