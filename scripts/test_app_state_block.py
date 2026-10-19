#!/usr/bin/env python3
"""Gate for the app_state prompt block.

A user asked their host whether the app was working now. The host answered
that the platform's ready check on the app was still failing. Minutes later
the delivery proof for that same app reported it installed and running, with
the platform's own ready check answering ok.

Both messages are in the same thread, five minutes apart. The proof was
polling /api/v1/ping green (envelope: status 200, body {"ok": true,
"status": "pong"}) across the whole window the host spent saying it was
dead.

The host was not inventing it. The per-turn context for that turn carried
exactly two blocks, `memory` and `memory_prefire`, and
what they held about this app was a v0.3.0 app.yaml, a v0.4.2 app.yaml (the
app was on 0.5.1) and a recalled memory saying the bot "only trades while
the control panel app is open". PROMPT_SCHEMA had ONE per-turn entry,
`memory`, so recall was the only fresh thing reaching the host. Stale
documents and old memories, no live state — hence a confident, specific,
wrong paragraph in which every detail was sourced from something that used
to be true.

The platform measured the answer and kept it to itself. This block hands it
over. The fix is context, not a rule telling the host not to guess.

Run: APP_ROOT=<repo> python3 scripts/test_app_state_block.py
"""
import asyncio
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(_ROOT / "brain"))

_FAILURES: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(f"[app-state] {'ok  ' if cond else 'FAIL'} {label}"
          f"{('  ' + extra) if extra and not cond else ''}")
    if not cond:
        _FAILURES.append(label)


from mastercode.runtime import prompt_schema as PS  # noqa: E402


class FakePool:
    def __init__(self, rows):
        self.rows = rows

    async def fetch(self, _sql, *_a):
        return self.rows


class BoomPool:
    async def fetch(self, *_a):
        raise RuntimeError("pool down")


def render(rows):
    rt = types.SimpleNamespace(pool=FakePool(rows))
    sess = types.SimpleNamespace(entity_id=1)
    return asyncio.run(PS._b_app_state(rt, sess, None))


NOW = datetime.now(timezone.utc)

# ── 1. the real row shape ────────────────────────────────────────────────────
REAL = [{
    "app_id": "trading-dryrun", "status": "completed", "live": False,
    "summary": "trading-dryrun: delivered — running and ready with a clean log",
    "verify": {"verdict": "delivered", "version": "0.5.1", "lifecycle": "window",
               "kind": "process", "running": True,
               "ready": {"ok": True, "status": 200}},
    "at": NOW - timedelta(minutes=7),
}]
out = render(REAL)
check("the measured verdict, version and ready result all reach the host",
      bool(out) and "0.5.1" in out and "ready check ok" in out
      and "running and ready" in out, repr(out))
check("the app id is not printed twice (the summary already leads with it)",
      bool(out) and "trading-dryrun: trading-dryrun" not in out)
check("a window-lifecycle app's 'not running' is explained as normal, not broken",
      bool(out) and "normal state, not a failure" in out)
check("...and the block does not invent WHY the lifecycle is window",
      bool(out) and "switched off" not in out and "resident_enabled" not in out)
check("the measurement carries its age", bool(out) and "measured 7m ago" in out)
check("the block outranks recall and stale app.yaml copies explicitly",
      bool(out) and "outranks anything recalled" in out and "older versions" in out)
check("...and names the tool for state RIGHT NOW",
      bool(out) and "system.app_status" in out)

# ── 2. a proof still running has NO verdict to report ───────────────────────
out2 = render([{
    "app_id": "podapp", "status": "in_progress", "live": True, "summary": None,
    "verify": None, "at": NOW - timedelta(minutes=2),
}])
check("an in-flight proof is marked as such and must not be pre-empted",
      bool(out2) and "RIGHT NOW" in out2 and "do not pre-empt" in out2, repr(out2))
check("...and no verdict word is asserted for it",
      bool(out2) and "delivered" not in out2.split("This is the platform")[0])

# ── 3. a FAILED proof reads as failed ───────────────────────────────────────
out3 = render([{
    "app_id": "podapp", "status": "failed", "live": False,
    "summary": "podapp: not delivered — no ready answer",
    "verify": {"verdict": "not delivered", "version": "0.2.0",
               "lifecycle": "resident", "kind": "process", "running": False,
               "ready": {"ok": False, "status": None}},
    "at": NOW - timedelta(minutes=3),
}])
check("a failed proof surfaces the failure and the failed ready check",
      bool(out3) and "not delivered" in out3 and "ready check FAILED" in out3,
      repr(out3))
check("...and a RESIDENT app that is down gets no 'this is normal' excuse",
      bool(out3) and "normal state" not in out3)

# ── 3b. Second review (2026-09-04). The lifecycle line was gated ONLY on
# lifecycle == window, and window is the default for every non-resident
# process app — so it appended "…not a failure" to FAILED verdicts, on the
# very line this block exists to make unambiguous.
out3b = render([{
    "app_id": "podapp", "status": "failed", "live": False,
    "summary": "podapp: failed — could not start: port busy",
    "verify": {"verdict": "failed", "version": "0.2.0", "lifecycle": "window",
               "kind": "process", "running": False,
               "ready": {"ok": False, "status": None}},
    "at": NOW - timedelta(minutes=3),
}])
check("a FAILED window app gets no 'not a failure' excuse",
      bool(out3b) and "not a failure" not in out3b and "failed" in out3b,
      repr(out3b))
# A static app has no process at all, so no lifecycle sentence is true of it.
out3c = render([{
    "app_id": "docsite", "status": "completed", "live": False,
    "summary": "docsite: delivered — installed",
    "verify": {"verdict": "delivered", "version": "1.0.0", "lifecycle": "window",
               "kind": "static", "running": False, "ready": None},
    "at": NOW - timedelta(minutes=4),
}])
check("a STATIC app is never told it 'runs only while a window is open'",
      bool(out3c) and "runs only while a window is open" not in out3c, repr(out3c))

# ── 3d. A proof that never finished was never MEASURED. The boot sweep reaps
# it to status='failed' with only the reaper's error; an abandoned one stays
# in_progress past its window. Neither is a platform verdict of failure.
for label, row in (
    ("reaped by the boot sweep", {
        "app_id": "podapp", "status": "failed", "live": False,
        "summary": None, "verify": None, "at": NOW - timedelta(hours=3)}),
    ("abandoned past its own window", {
        "app_id": "podapp", "status": "in_progress", "live": False,
        "summary": None, "verify": None, "at": NOW - timedelta(hours=3)}),
):
    o = render([row])
    check(f"a proof {label} reports NO verdict, never 'failed'",
          bool(o) and "NO measured verdict" in o and "never that it failed" in o,
          repr(o))
    # Scoped to the app lines: the block FOOTER legitimately says "the state
    # RIGHT NOW", so asserting over the whole string tests the wrong thing.
    check(f"...and a proof {label} is not still 'being proved RIGHT NOW'",
          bool(o) and "RIGHT NOW" not in o.split("This is the platform")[0])

# ── 4. cost and safety ──────────────────────────────────────────────────────
check("an entity that never shipped an app renders nothing at all",
      render([]) is None)
check("a broken pool never takes the turn down (block fails soft)",
      asyncio.run(PS._b_app_state(
          types.SimpleNamespace(pool=BoomPool()),
          types.SimpleNamespace(entity_id=1), None)) is None)

# ── 5. it must actually be WIRED, and per-turn ──────────────────────────────
_blocks = {b.name: b for b in PS.PROMPT_SCHEMA}
check("app_state is in PROMPT_SCHEMA", "app_state" in _blocks)
check("...as PER_TURN — a verdict flips mid-conversation, which is the whole "
      "shape of the incident",
      "app_state" in _blocks
      and _blocks["app_state"].freshness is PS.Freshness.PER_TURN)
check("memory is no longer the only per-turn block",
      len([b for b in PS.PROMPT_SCHEMA
           if b.freshness is PS.Freshness.PER_TURN]) >= 2)

print()
if _FAILURES:
    print(f"{len(_FAILURES)} FAILED: {_FAILURES}")
    sys.exit(1)
print("[app-state] ALL OK")
