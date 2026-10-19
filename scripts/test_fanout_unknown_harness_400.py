#!/usr/bin/env python3
"""A dispatch that names something that is not a harness is refused BY NAME.

Measured on one Host 2026-09-21 (project 9239, 21:54:29). The host sent
harnesses=[codex, codex-s2, codex-s3, codex-s4] — variant ids where harness
ids go. The connected-filter silently dropped the three unknown names, the
call returned `ok`, one run started, and the host believed four had. Ten
seconds earlier the SAME mistake in harness_model had been refused with a
400 that named the keys; harnesses had no such check.

What this pins, by driving the real route with the connected-harness read
stubbed:
  * a name that is not a harness at all -> 400, and the message names it and
    lists what harnesses are;
  * a real harness that is merely not connected keeps its 409 (that names a
    real engine the user can connect);
  * the refusal happens before any wake, worktree or credential work (the
    stub is the only seam touched).

Run: APP_ROOT=<repo> python3 scripts/test_fanout_unknown_harness_400.py
"""
import asyncio
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402

from server.routes import review as rv  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


async def _connected():
    return ["codex"]


rv._installed_harnesses = _connected


def _status(harnesses):
    try:
        run(rv.fan_out(rv.FanOutReq(prompt="build it", harnesses=harnesses)))
    except HTTPException as e:
        return e.status_code, str(e.detail)
    except Exception as e:  # noqa: BLE001 — anything past the guard is a wrong turn
        return None, f"{type(e).__name__}: {e}"
    return 200, ""


print("== the incident: variant ids where harness ids go ==")
code, detail = _status(["codex", "codex-s2", "codex-s3", "codex-s4"])
check("refused with 400", code == 400, f"{code} {detail}")
check("...naming every unknown name",
      all(v in detail for v in ("codex-s2", "codex-s3", "codex-s4")), detail)
check("...and listing what harnesses are", "'codex'" in detail and "'claude-code'" in detail, detail)
check("...and pointing at `variants` for several runs of one harness", "`variants`" in detail, detail)

print("== a real harness that is not connected keeps its 409 ==")
code, detail = _status(["claude-code"])
check("409, not 400", code == 409, f"{code} {detail}")
check("...and it says to connect it", "connected" in detail, detail)

print("== a single unknown name among connected ones is still refused ==")
code, detail = _status(["codex", "gpt-6-astra"])
check("a model id in the harness list -> 400 naming it",
      code == 400 and "gpt-6-astra" in detail, f"{code} {detail}")

print()
if FAILURES:
    print(f"FAIL {len(FAILURES)}: {FAILURES}")
    sys.exit(1)
print("all green")
