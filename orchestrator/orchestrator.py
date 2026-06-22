"""Orchestrator entrypoint — ``python3 -m orchestrator.orchestrator <command>``.

Commands:
  --serve       Run continuously (the 24/7 daemon): tick back-to-back so agents work
                without idle gaps — the only pauses are gate (test) runs. Reloads
                config.toml each iteration. This is what the systemd service runs.
  --tick        Advance the state machine one step (for cron/manual stepping).
  --status      Print the current cycle + foci + budget.
  --digest      Print the cycle digest (Telegram-ready).
  --run-cycle   Loop ticks until the current cycle reaches 'done' (bounded). For the
                offline dry-run; combine with --fake to use the no-LLM backend.
  --fake        Use the deterministic FakeAgentBackend (no API cost).

Safety: a KILL file (orchestrator/state/KILL) pauses work — --serve idles (keeps the
daemon alive, rechecking) and --tick is a no-op. Remove the file to resume.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from . import config as config_mod
from .agents import ClaudeAgentBackend, FakeAgentBackend
from .cycle import tick
from .digest import digest_text, status_text
from .state import CYCLE_DONE, StateStore

# Tick actions that mean "nothing to do right now" — back off before re-ticking.
IDLE_ACTIONS = frozenset({"killed", "plan_empty", "plan_skipped_budget"})


def serve(cfg_store_loader, backend, *, idle_sleep=30.0, busy_sleep=0.5,
          max_iterations=None, sleep_fn=time.sleep) -> int:
    """Continuous loop: tick back-to-back, only backing off when idle.

    ``cfg_store_loader`` returns a fresh ``(cfg, store)`` each iteration so edits to
    config.toml take effect live. Returns the number of iterations run.
    """
    i = 0
    while max_iterations is None or i < max_iterations:
        i += 1
        cfg, store = cfg_store_loader()
        if store.killed:
            sleep_fn(idle_sleep)
            continue
        result = tick(cfg, backend, store)
        sleep_fn(idle_sleep if result.get("action") in IDLE_ACTIONS else busy_sleep)
    return i


def _backend(fake: bool):
    return FakeAgentBackend() if fake else ClaudeAgentBackend()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="orchestrator")
    ap.add_argument("--config", default=None, help="path to config.toml")
    ap.add_argument("--fake", action="store_true", help="use the no-LLM fake backend")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--serve", action="store_true")
    g.add_argument("--tick", action="store_true")
    g.add_argument("--status", action="store_true")
    g.add_argument("--digest", action="store_true")
    g.add_argument("--run-cycle", action="store_true")
    ap.add_argument("--max-ticks", type=int, default=200, help="safety cap for --run-cycle")
    ap.add_argument("--idle-sleep", type=float, default=30.0, help="--serve idle backoff (s)")
    args = ap.parse_args(argv)

    cfg = config_mod.load(args.config)
    store = StateStore(cfg.state_dir)

    if args.status:
        print(status_text(cfg, store))
        return 0
    if args.digest:
        print(digest_text(cfg, store))
        return 0

    backend = _backend(args.fake)

    if args.serve:
        print(f"orchestrator serving: cycle {cfg.cycle}, autonomy={cfg.autonomy}, "
              f"max_concurrent={cfg.max_concurrent}", flush=True)
        serve(lambda: (config_mod.load(args.config), StateStore(config_mod.load(args.config).state_dir)),
              backend, idle_sleep=args.idle_sleep)
        return 0

    if args.tick:
        result = tick(cfg, backend, store)
        print(json.dumps(result, indent=2, default=str))
        return 0

    if args.run_cycle:
        start = store.read_cycle().get("cycle")
        for i in range(args.max_ticks):
            result = tick(cfg, backend, store)
            print(f"[tick {i}] {json.dumps(result, default=str)}")
            if result.get("action") == "killed":
                break
            cyc = store.read_cycle()
            # Stop once this cycle finishes (phase done) — don't roll into the next.
            if cyc.get("phase") == CYCLE_DONE and cyc.get("cycle") == start:
                tick(cfg, backend, store)  # one more to log cycle_complete
                break
        print("\n" + status_text(cfg, store))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
