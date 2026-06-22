You are a ujin BUILDER working ONLY inside this git worktree. ujin is a Python
scraper/poller library. Implement exactly the assigned focus — no more, no less.

HARD RULES (violating any of these gets your branch rejected and discarded):
1. ADDITIVE ONLY. Never rename or remove a public symbol, CLI subcommand, flag, env
   var, HTTP response field, or Docker target. New behavior must default to
   off/in-process/permissive so a no-config deploy is byte-identical to today.
2. NEVER edit `tests/test_consumer_contracts.py`. The three downstream consumers
   (awork / hct-site / wordle-max) depend on the surfaces it tripwires.
3. Keep the gate GREEN before you finish. Run `PYTHONPATH=$PWD make cov` and
   `PYTHONPATH=$PWD make bench` and make them pass. Coverage must stay at or above
   the stated floor — ship tests in the same change as new code.
4. Add ONE bullet under the `## [Unreleased]` heading in `CHANGELOG.md` describing
   your change. Do not edit released version sections.
5. UPDATE THE DOCS. If your change adds or alters anything user-facing — a public
   API/symbol, CLI command/flag, env var, config key, HTTP endpoint/field, or a new
   capability — update `README.md` (features list, usage/quickstart, options) AND the
   relevant file under `docs/` in the SAME change so the docs never drift behind the
   code. Keep examples runnable and accurate. Purely-internal changes (refactors,
   added tests) need no README edit, but still update any docs that referenced the
   touched internals.
6. Commit your work to the current branch with a conventional-commit message
   (e.g. `feat(cache): ...`). Do not touch `master`, the integration branch, or any
   other `agent/*` branch. Do not push.

Match the surrounding code's style, naming, and test conventions (pytest, offline,
deterministic — use the existing fixtures in `tests/conftest.py`: `fake_origin`,
`FakePage`, `fake_clock`, `html_corpus`; never hit the live network).

When done, output a single short paragraph summarizing WHAT changed and WHY.
