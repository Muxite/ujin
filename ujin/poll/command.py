"""CommandPollable — poll a shell command by watching its stdout.

Run a command on each poll and fingerprint its output; ``changed`` flips when the
output differs. Useful for "poll ANYTHING" that has a CLI: `kubectl get pods`,
`git ls-remote`, `df -h`, a health-check script, etc. A non-zero exit is a failure.
"""
from __future__ import annotations

import asyncio
import time

from ujin.poll.base import PollResult, decide_changed, fingerprint


class CommandPollable:
    def __init__(
        self,
        argv: list[str],
        *,
        key: str | None = None,
        timeout: float = 30.0,
        cwd: str | None = None,
    ) -> None:
        if not argv:
            raise ValueError("argv must be non-empty")
        self.argv = argv
        self.key = key or " ".join(argv)
        self.timeout = timeout
        self.cwd = cwd

    async def poll(self, prev: PollResult | None) -> PollResult:
        start = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(), self.timeout)
            except asyncio.TimeoutError:
                proc.kill()
                return PollResult.failure(f"command timed out after {self.timeout}s")
        except FileNotFoundError as exc:
            return PollResult.failure(f"command not found: {exc}")
        except Exception as exc:  # noqa: BLE001
            return PollResult.failure(f"{type(exc).__name__}: {exc}")

        latency = int((time.monotonic() - start) * 1000)
        if proc.returncode != 0:
            return PollResult(
                ok=False,
                status=proc.returncode,
                error=err.decode("utf-8", "replace")[:500] or f"exit {proc.returncode}",
                latency_ms=latency,
            )
        text = out.decode("utf-8", "replace")
        fp = fingerprint(text)
        return PollResult(
            ok=True,
            changed=decide_changed(fp, prev),
            fingerprint=fp,
            payload=text,
            status=0,
            latency_ms=latency,
        )
