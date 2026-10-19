#!/usr/bin/env python3
"""The harness lease: a harness lives while something is USING it, and dies one
grace window after the last holder stopped saying so.

The operator's idle-sweep reads only the `privatae.ai/last-wake` annotation
(in the platform controller) — never a live
WebSocket, never an in-flight /run. So every holder has to re-stamp it. fan_out
always did; the editor never did, so an editor left open past the 900s grace had
its pod scaled to 0 underneath the user. This exercises the real
_start_lease/_stop_lease/session_opened/session_closed/_release_and_bill with
_wake stubbed and the heartbeat turned down to 1s.

Run in-image: docker run --entrypoint python3 :latest scripts/test_harness_lease.py
"""
import asyncio, os, sys

# Must be set BEFORE importing harness_backend — the constant is read at import.
os.environ["HARNESS_LEASE_HEARTBEAT_S"] = "1"
sys.path.insert(0, "/app")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server.applications.harness_backend as hb

FAILS = []
def check(n, ok, d=""):
    print(f"[harness-lease] {'ok  ' if ok else 'FAIL'} {n}" + (f" — {d}" if d else ""))
    (FAILS.append(n) if not ok else None)


def stub_wake():
    """Swap _wake for a counter. Returns the list it appends to."""
    calls = []
    async def _fake(harness_id, creds=None):
        calls.append(harness_id)
        return {"ok": True, "url": "http://harness-vscode.ns.svc:8832"}
    hb._wake = _fake
    return calls


async def main():
    check("heartbeat constant is env-tunable", hb.HARNESS_LEASE_HEARTBEAT_S == 1,
          str(hb.HARNESS_LEASE_HEARTBEAT_S))

    # A live editor session holds the lease open indefinitely.
    hb._state.clear(); calls = stub_wake()
    host = await hb.session_opened("vscode", "vscode")
    check("session_opened returns an upstream host", host is not None, str(host))
    await asyncio.sleep(2.5)
    check("lease re-stamps while the WS is open", len(calls) >= 3,
          f"{len(calls)} wakes (1 initial + >=2 beats)")

    # Closing the last WS stops it — no lease on a pod nobody is holding.
    hb.session_closed("vscode", "vscode")
    n = len(calls)
    await asyncio.sleep(2.5)
    check("lease stops once the last WS closes", len(calls) == n,
          f"{len(calls) - n} beats after close")

    # A bare page load is NOT a holder: the sweep must still be able to reclaim it.
    hb._state.clear(); calls = stub_wake()
    await hb.ensure_awake("vscode", "vscode")
    check("bare ensure_awake starts no lease",
          (hb._state.get("vscode") or {}).get("lease_task") is None)
    await asyncio.sleep(2.5)
    check("bare page load never re-stamps", len(calls) == 1,
          f"{len(calls)} wakes, want exactly 1")

    # Release must cancel the lease, or a beat would re-wake what we just shut down.
    hb._state.clear(); calls = stub_wake()
    await hb.session_opened("vscode", "vscode")
    await asyncio.sleep(1.5)
    hb._state["vscode"]["ws"] = 1   # keep it "open" so only release can stop the beat
    await hb._release_and_bill("vscode", "vscode")
    n = len(calls)
    await asyncio.sleep(2.5)
    check("release cancels the lease (no re-wake after release)", len(calls) == n,
          f"{len(calls) - n} beats after release")

    # Two tabs share one heartbeat.
    hb._state.clear(); stub_wake()
    await hb.session_opened("vscode", "vscode")
    t1 = hb._state["vscode"]["lease_task"]
    await hb.session_opened("vscode", "vscode")
    check("a second WS reuses the one lease task",
          t1 is hb._state["vscode"]["lease_task"])
    check("ws count tracks both sessions", hb._state["vscode"]["ws"] == 2,
          str(hb._state["vscode"]["ws"]))
    hb._stop_lease("vscode")

    # The login release rule (server/routes/review.py::harness_login finally):
    # hold the pod ONLY while the vendor is still waiting on the user's browser.
    #
    # Read the REAL condition out of review.py rather than restating it here. A
    # local re-implementation of `done or not ok` passes whatever review.py
    # says, so inverting the real condition would leave this green — the check
    # would assert against its own copy. Reviewer caught exactly that.
    import ast, pathlib
    src = pathlib.Path(__file__).resolve().parent.parent / "server" / "routes" / "review.py"
    tree = ast.parse(src.read_text())
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.AsyncFunctionDef) and n.name == "harness_login"), None)
    check("found harness_login in review.py", fn is not None)
    tries = [n for n in ast.walk(fn) if isinstance(n, ast.Try)] if fn else []
    guard = None
    for t in tries:
        for stmt in t.finalbody:
            if isinstance(stmt, ast.If):
                guard = stmt.test
                break
    check("harness_login's finally guards the release on a condition",
          guard is not None,
          "release is unconditional — a pending login would be killed")
    if guard is not None:
        expr = ast.unparse(guard)
        # Must consult all three: done (finished), ok (refused), and `answered`
        # (did the sidecar reply at all). Any one alone gets a case wrong.
        check("release condition consults `done`", "done" in expr, expr)
        check("release condition consults `ok`", "ok" in expr, expr)
        check("release condition consults `answered`", "answered" in expr, expr)
        # Evaluate the REAL expression against every case.
        def real(ok, done, answered=True):
            return bool(eval(expr,  # noqa: S307
                             {"out": {"ok": ok, "done": done}, "answered": answered}))
        check("login: vendor still waiting -> hold", real(True, False) is False, expr)
        check("login: vendor finished -> release", real(True, True) is True, expr)
        check("login: vendor refused -> release", real(False, False) is True, expr)
        # A transport fault is NOT a verdict. A timeout or a reset also lands as
        # ok=False, and releasing on it scales the pod to 0 under a login that is
        # very likely still waiting — the pod being slow to answer is not
        # evidence the user gave up. This case used to release; it must hold.
        check("login: sidecar unreachable -> hold, not release",
              real(False, False, answered=False) is False, expr)
        check("login: unreachable but already done -> still release",
              real(False, True, answered=False) is True, expr)

    print(f"[harness-lease] {'PASS' if not FAILS else 'FAIL'} ({len(FAILS)} failing)")
    return 1 if FAILS else 0


sys.exit(asyncio.run(main()))
