#!/usr/bin/env python3
"""Gate: there is no original conversation (2026-10-12: a scheduled turn goes
to whatever thread called for it; there is no original conversation thread).

Pinned here, container-free:
  A. brain_service's net under a send that names no thread
     (_lands_in_circles) catches a Circles send and nothing else: never a
     project or Workbench send, never a send that names a thread, never a
     hidden-lane send (a fired wake-up, a replay, an A/B variant), whose
     caller owns its thread (a scheduled task records its own topic);
  B. a decided decision's forward turn starts and is followed to its end
     (decisions_service.forward_decision_turn). It used to `await` the
     stream send_message returns, which raised every time, so no decision
     was ever forwarded; a refused turn (a 3: frame) is not counted;
  C. the context panel's route (GET /api/circles/thread-context/self/{id})
     answers 404 for the original conversation's ids and reads nothing: it
     used to map them to app:circles and show that scope's history.

Run: APP_ROOT=<repo> python3 scripts/test_no_original_conversation.py
"""
import asyncio
import os
import sys

failures = []


def check(label, ok, detail=""):
    print(f"[no-original] {'ok  ' if ok else 'FAIL'} {label}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def main() -> int:
    app_root = os.environ.get("APP_ROOT", "/app")
    for p in (app_root, os.path.join(app_root, "brain")):
        if p not in sys.path:
            sys.path.insert(0, p)
    from server import brain_service as bs
    from server import decisions_service as ds

    # A. the net's reach.
    L = bs._lands_in_circles
    check("A a Circles send with no thread is caught", L({"app_id": "circles"}) is True)
    check("A ...and one with no app at all (the brain API, a forwarded decision)", L({}) is True)
    check("A a send that names a thread is left alone", L({"app_id": "circles", "thread_id": "x"}) is False)
    check("A a project send is left alone", L({"app_id": "workbench", "project_id": 4}) is False)
    check("A a Workbench send with no project is left alone", L({"app_id": "workbench"}) is False)
    for hidden in ("scheduled_task", "circles-replay", "circles-ab"):
        check(f"A a hidden-lane send ({hidden}) thinking in Circles is left alone",
              L({"app_id": hidden, "history_context": "chat"}) is False)

    # B. the decision's forward turn.
    sent, followed = [], []

    def stream(frames):
        async def gen(**kw):
            sent.append(kw)
            for f in frames:
                yield f
                followed.append(f)
        return gen

    real = bs.send_message
    try:
        bs.send_message = stream(['2:[{"type":"session"}]\n', '0:"ok"\n', 'e:{}\n'])

        async def run_ok():
            ok = await ds.forward_decision_turn(9003, "sess-1", "Decision on X: I chose `a`.", 9)
            for _ in range(20):
                await asyncio.sleep(0.01)
            return ok
        ok = asyncio.run(run_ok())
        check("B a decided decision starts its turn, once, with the composed words",
              ok is True and len(sent) == 1 and sent[0].get("message", "").startswith("Decision on X")
              and sent[0].get("entity_id") == 9003, f"{ok} {sent}")
        check("B ...and the turn is followed to its end", followed[-1:] == ['e:{}\n'], f"{followed}")
        sent.clear()
        followed.clear()
        bs.send_message = stream(['3:"This thread was deleted."\n'])
        refused = asyncio.run(ds.forward_decision_turn(9003, "sess-1", "Decision on Y", 10))
        check("B a refused turn (a 3: frame) is not counted as forwarded", refused is False, f"{refused}")
    finally:
        bs.send_message = real

    # C. the context panel never reads the old scope.
    from fastapi import HTTPException
    # circles.py imports server.main at module scope, and server.main mounts
    # the dashboard's static directories, which a checkout does not have.
    import types
    sys.modules.setdefault("server.main", types.ModuleType("server.main"))
    from server.routes import circles as rc
    read = []

    class Pool:
        def __getattr__(self, name):
            read.append(name)
            raise RuntimeError("the pool was used")
    real_pool = bs.get_pool
    bs.get_pool = lambda: Pool()
    try:
        for legacy in ("original", "self", "ORIGINAL"):
            try:
                asyncio.run(rc.get_thread_context("self", legacy))
                code = 200
            except HTTPException as e:
                code = e.status_code
            except Exception as e:  # noqa: BLE001
                code = type(e).__name__
            check(f"C the context panel for '{legacy}' is a 404 and reads nothing", code == 404 and not read,
                  f"{code} {read}")
            read.clear()
    finally:
        bs.get_pool = real_pool

    if failures:
        print(f"[no-original] FAILED {len(failures)}")
        return 1
    print("[no-original] all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
