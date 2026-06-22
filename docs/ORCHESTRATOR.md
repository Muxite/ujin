# Autonomous orchestrator

`orchestrator/` is a self-running development loop for ujin. It drives fleets of
headless `claude -p` agents through continuous **WORK → TEST → REVIEW → PLAN** cycles,
using git worktrees, branches, and automatic merges. It is *tooling* — not part of the
shipped `ujin` package, excluded from the coverage source, and it never alters the
frozen consumer-contract surface on its own.

## How it runs

A systemd user timer fires one **tick** every 10 minutes. Each tick advances the state
machine one bounded step (at most `max_concurrent` expensive agent operations), writes
everything to `orchestrator/state/` (gitignored), and exits — so the loop is crash-safe
and resumable.

```
cycle:  planning ─▶ working ─▶ integrating ─▶ releasing ─▶ done ─▶ (next cycle)
focus:  ready ─▶ building ─▶ testing ─▶ reviewing ─▶ {approved | needs_work | dead}
                                                       approved ─▶ integrated
```

| Stage | Actor | Model |
|-------|-------|-------|
| PLAN | Planner emits `backlog.json` | Opus |
| WORK | Builder implements one focus in its worktree | Opus (hard) / Sonnet (routine) |
| TEST | Deterministic harness runs `make gate` → `test.json` | — |
| REVIEW | Verifier emits `verdict.json` | Haiku |
| INTEGRATE | Serialized `merge --no-ff` + re-gate | deterministic + Haiku triage |
| RELEASE | Version + CHANGELOG finalize, merge → master | deterministic |

## Roles & worktrees

Each `agent/<focus>` gets an isolated worktree under `.claude/worktrees/<focus>`.
Because ujin is installed editable, every command in a worktree runs with
`PYTHONPATH=<worktree>` and a startup self-check asserts `import ujin` resolves *inside*
the worktree — guarding against wrong-tree false-greens.

## Safety rails (full-auto)

- **Kill switch:** `touch orchestrator/state/KILL` → the next tick is a no-op (and the
  systemd unit short-circuits in `ExecStartPre`). Remove the file to resume.
- **Consumer-contract hard block:** a branch that edits `tests/test_consumer_contracts.py`
  is rejected outright, quarantined to `dead/*`, and flagged in the digest — never merged.
- **Coverage ratchet:** coverage must stay ≥ `max(85, recorded floor)`; the floor only
  rises (`state/cov_floor.txt`).
- **Benchmark gate:** median ≤ 4× baseline, *blocking* (unlike CI).
- **Integration stays green:** a merge that turns integration red is hard-reset away and
  the focus re-queued.
- **Budget caps:** per-agent (`--max-budget-usd`), per-cycle, per-day; over cap → stop
  dispatching new work, finish in-flight only.
- **Runaway prevention:** `max_concurrent` cap, `max_build_retries` then quarantine, and
  a no-progress detector (identical diff across a retry → quarantine).
- **Releases don't push** unless `push_on_release = true`.

## Configure

Edit `orchestrator/config.toml` (re-read every tick — no restart needed). Key knobs:
`autonomy` (`full_auto` | `supervised`), `push_on_release`, `max_concurrent`,
`max_build_retries`, `coverage_floor`, model tiers, and budgets.

## Operate

```bash
# Dry-run the whole pipeline offline (no LLM cost) with the fake backend:
python3 -m orchestrator.orchestrator --run-cycle --fake

# One real tick / status / digest:
python3 -m orchestrator.orchestrator --tick
python3 -m orchestrator.orchestrator --status
python3 -m orchestrator.orchestrator --digest

# Install + enable the 24/7 loop (user services):
cp orchestrator/systemd/*.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ujin-orchestrator.timer ujin-orchestrator-gc.timer

# Watch it live / stop it:
journalctl --user -u ujin-orchestrator -f
touch orchestrator/state/KILL          # emergency brake
systemctl --user disable --now ujin-orchestrator.timer
```

## Tests

`orchestrator/tests/` exercises the deterministic plumbing (state, worktree, gates
parsing, and a full fake-backend cycle in a temp git repo). They are isolated from
ujin's own `make test` (different `testpaths`), so run them explicitly:

```bash
python3 -m pytest orchestrator/tests -q
```
