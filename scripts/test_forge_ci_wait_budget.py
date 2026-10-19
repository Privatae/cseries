#!/usr/bin/env python3
"""Gate: a project's forge-CI wait is never capped at one hour, and a long
wait stays a cheap poll.

The defect: pipeline.py's gitea-actions row refused wait_budget_s above 3600,
so a CI suite longer than an hour could never gate auto-deliver — the row
read "CI did not answer" and held every merge and install. The wait itself
polled every 15 s for its whole budget.

What this pins (no forge — a fake clock and a fake status read):
  1. wait_budget_s of 2 h and 6 h validate; the ceiling is the stated
     one-day backstop (pipeline.CI_WAIT_BACKSTOP_S), and above it the error
     names it;
  2. wait() over a 6 h budget that never answers stops at the budget and
     reads the forge a few hundred times at most, not every 15 s;
  3. early on it still polls at its 15 s pace, so a quick CI is seen quickly;
  4. the not-answered reason states a long budget in hours.

Run: APP_ROOT=<repo> python3 scripts/test_forge_ci_wait_budget.py
"""
import asyncio
import os
import sys
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path[:0] = [str(_ROOT), str(_ROOT / "brain")]

FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"[forge-ci-wait] ok   {label}")
    else:
        FAILED.append(label)
        print(f"[forge-ci-wait] FAIL {label}" + (f" — {detail}" if detail else ""))


def main() -> int:
    from mastercode.runtime import pipeline as pl
    from server.routes import forge_status as fs

    # ── 1. the schema ────────────────────────────────────────────────────
    for budget in (7200, 6 * 3600):
        norm, errs = pl.validate({"format": pl.FORMAT, "check": [
            {"provider": "gitea-actions", "contexts": "check/suite", "wait_budget_s": budget}]})
        check(f"wait_budget_s={budget} (a CI suite longer than an hour) validates",
              not errs and norm["check"][0]["wait_budget_s"] == budget, f"{errs}")
    backstop = getattr(pl, "CI_WAIT_BACKSTOP_S", None)
    check("the ceiling is a stated backstop of at least a day",
          isinstance(backstop, int) and backstop >= 86_400, repr(backstop))
    if isinstance(backstop, int):
        _, errs = pl.validate({"format": pl.FORMAT, "check": [
            {"provider": "gitea-actions", "contexts": "a", "wait_budget_s": backstop + 1}]})
        check("above the backstop the error names it",
              any(f"at most {backstop}" in e for e in errs), f"{errs}")

    # ── 2-3. the wait is a cheap poll ────────────────────────────────────
    class Clock:
        def __init__(self):
            self.t = 0.0
            self.reads_at: list[float] = []

        def now(self):
            return self.t

        async def sleep(self, s):
            self.t += s
            if self.t > 10 ** 7:
                raise AssertionError("wait() never stopped at its budget")
    c = Clock()

    async def pending(method, path, params=None, **kw):
        c.reads_at.append(c.t)
        return {"statuses": [{"context": "check/suite", "status": "pending",
                              "updated_at": "2026-10-02T10:00:00Z"}]}
    row = {"provider": "gitea-actions", "contexts": "check/suite", "wait_budget_s": 6 * 3600}
    v = asyncio.run(fs.wait("u/app", "b" * 40, row, request=pending, sleep=c.sleep, clock=c.now))
    check("a 6 h wait that never answers stops at its budget, as did not run",
          v["ran"] is False and c.t == 6 * 3600.0, f"t={c.t} {v.get('reason')}")
    check("...and reads the forge at most a few hundred times (not every 15 s)",
          len(c.reads_at) <= 400, f"reads={len(c.reads_at)} (every 15 s would be {6 * 3600 // 15 + 1})")
    gaps = [b - a for a, b in zip(c.reads_at, c.reads_at[1:])]
    check("...while its first minutes still poll every 15 s",
          gaps[:5] == [15.0] * 5, f"{gaps[:6]}")
    check("...and no gap between reads is ever longer than two minutes",
          max(gaps) <= 120.0 + 1e-6, f"max gap {max(gaps)}")
    check("the not-answered reason states a long budget in hours",
          "within 6 h" in (v.get("reason") or ""), v.get("reason") or "")

    if FAILED:
        print(f"FAIL: forge CI wait — {len(FAILED)} check(s): " + "; ".join(FAILED))
        return 1
    print("OK: forge CI wait — no one-hour cap, and a long wait stays a cheap poll")
    return 0


if __name__ == "__main__":
    sys.exit(main())
