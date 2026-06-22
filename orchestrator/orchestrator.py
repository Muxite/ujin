"""Orchestrator entrypoint — ``python3 -m orchestrator.orchestrator <command>``.

Commands:
  --tick        Advance the state machine one step (what the systemd timer fires).
  --status      Print the current cycle + foci + budget.
  --digest      Print the cycle digest (Telegram-ready).
  --run-cycle   Loop ticks until the current cycle reaches 'done' (bounded). For the
                offline dry-run; combine with --fake to use the no-LLM backend.
  --fake        Use the deterministic FakeAgentBackend (no API cost).

Safety: a KILL file (orchestrator/state/KILL) makes every tick a no-op. systemd's
ExecStartPre also checks for it so the unit short-circuits before Python even starts.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import config as config_mod
from .agents import ClaudeAgentBackend, FakeAgentBackend
from .cycle import tick
from .digest import digest_text, status_text
from .state import CYCLE_DONE, StateStore


def _backend(fake: bool):
    return FakeAgentBackend() if fake else ClaudeAgentBackend()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="orchestrator")
    ap.add_argument("--config", default=None, help="path to config.toml")
    ap.add_argument("--fake", action="store_true", help="use the no-LLM fake backend")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--tick", action="store_true")
    g.add_argument("--status", action="store_true")
    g.add_argument("--digest", action="store_true")
    g.add_argument("--run-cycle", action="store_true")
    ap.add_argument("--max-ticks", type=int, default=200, help="safety cap for --run-cycle")
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
