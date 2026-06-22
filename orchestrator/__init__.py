"""Autonomous 24/7 development orchestrator for ujin.

A tick-based, resumable state machine that drives fleets of headless ``claude -p``
agents through continuous WORK -> TEST -> REVIEW -> PLAN cycles, using git worktrees,
branches, and automatic merges. See ``docs/ORCHESTRATOR.md`` for the design.

The package is *tooling* — it is not part of the shipped ``ujin`` distribution and is
excluded from the package's coverage source, so it never affects the consumer-contract
surface or the 85% coverage gate.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
