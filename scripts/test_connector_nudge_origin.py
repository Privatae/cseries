#!/usr/bin/env python3
"""
A connector's forwarded message keeps its connector_origin when it lands in a
running turn (review of feat/connectors-sdm, finding 4).

A message that STARTS a turn carries its origin in the frame, and the turn's
user row keeps it (session.py, _user_tags). A message that arrives while a
turn runs in its thread is a nudge, and nudges carried words only, so the
row written for it got {"nudge": True} and nothing else. On Slack that is the
usual path: a share with a comment is two messages, and the share lands in
the turn the comment started.

  1. brain_service.send_message hands the frame's connector_origin to the
     registry as the nudge's tags (and nothing when the frame has none)
  2. the registry keeps them with a nudge queued before the session frame,
     and pushes them with it when the frame arrives; a nudge with no tags is
     pushed exactly as before (two arguments)
  3. a nudge into a turn that already has its session is pushed with them
  4. nudges nobody read go on as the next turn carrying their origin in its
     frame: one as itself, several as a list in the order of the texts
  5. session.push_nudge keeps only connector_origin, and the drain reads it
     by text before consume_nudges_timed empties the queue

Exit 0 = all pass.
"""

import asyncio
import logging
import os
import sys
from turn_source import turn_source  # session.py and the modules split out of it

APP_ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_ROOT)
sys.path.insert(0, os.path.join(APP_ROOT, "brain"))
logging.basicConfig(level=logging.ERROR)

FAILS: list[str] = []
ORIGIN = {"kind": "forward", "source": "bot", "name": "Hermes"}


def check(name, cond, extra=""):
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"[nudge-origin] FAIL {name} {extra}")


def gen(frames, gate=None):
    async def _g():
        for f in frames:
            if gate is not None:
                await gate.wait()
            yield f
    return _g


async def main():
    import server.turn_registry as tr
    from server import brain_service as bs
    from mastercode.runtime import session as S

    # ── 1. send_message passes the frame's origin as the nudge's tags ──────
    seen: list = []

    class _Reg:
        def start_or_attach(self, **kw):
            seen.append(kw.get("nudge_tags", "MISSING"))
            raise tr.TurnBusyError("stop here")

    tr_get = tr.get_turn_registry
    tr.get_turn_registry = lambda: _Reg()   # send_message imports it at call time
    try:
        async for _f in bs.send_message(entity_id=None, message="fwd text", app_id="circles",
                                        frame={"connector_origin": ORIGIN}):
            pass
        async for _f in bs.send_message(entity_id=None, message="plain", app_id="circles"):
            pass
    finally:
        tr.get_turn_registry = tr_get
    check("send_message hands the frame's connector_origin to the registry",
          seen[:1] == [{"connector_origin": ORIGIN}], repr(seen))
    check("…and no tags when the frame carries none", seen[1:2] == [None], repr(seen))

    # ── 2. pre-session nudge keeps its tags until the session frame ────────
    pushed: list = []
    reg = tr.TurnRegistry()

    def _push(sid, text, *rest):
        pushed.append((sid, text) + tuple(rest))
    reg._push_nudge = _push
    gate = asyncio.Event()
    rec, _ = reg.start_or_attach(
        thread_key="k1", entity_id=1, app_id="circles", message="the comment",
        make_inner=gen(['2:[{"type": "session", "session_id": "s-1"}]\n', "e:{}\n"], gate))
    try:
        r2, mode = reg.start_or_attach(
            thread_key="k1", entity_id=1, app_id="circles", message="the shared text",
            make_inner=gen(["e:{}\n"]), nudge_tags={"connector_origin": ORIGIN})
    except TypeError as e:
        r2, mode = None, f"TypeError: {e}"
    reg.start_or_attach(
        thread_key="k1", entity_id=1, app_id="circles", message="the person again",
        make_inner=gen(["e:{}\n"]))
    check("a forward in a running turn is a nudge", mode == "nudged" and r2 is rec, mode)
    check("its tags wait with it before the session frame",
          getattr(rec, "pending_nudge_tags", None) == [{"connector_origin": ORIGIN}, None],
          repr(getattr(rec, "pending_nudge_tags", None)))
    gate.set()
    for _ in range(100):
        if len(pushed) >= 2:
            break
        await asyncio.sleep(0.02)
    check("on the session frame it is pushed WITH its tags",
          ("s-1", "the shared text", {"connector_origin": ORIGIN}) in pushed, repr(pushed))
    check("a nudge with no tags is pushed exactly as before (two arguments)",
          ("s-1", "the person again") in pushed, repr(pushed))
    await asyncio.wait_for(rec.done.wait(), 5)

    # ── 3. a nudge into a turn that already has its session ────────────────
    pushed.clear()
    reg_b = tr.TurnRegistry()
    reg_b._push_nudge = _push
    first, rest = asyncio.Event(), asyncio.Event()

    async def _two_step():
        await first.wait()
        yield '2:[{"type": "session", "session_id": "s-2"}]\n'
        await rest.wait()
        yield "e:{}\n"
    rec_b, _ = reg_b.start_or_attach(thread_key="k2", entity_id=1, app_id="circles",
                                     message="first", make_inner=_two_step)
    first.set()
    for _ in range(100):
        if rec_b.session_id:
            break
        await asyncio.sleep(0.02)
    try:
        reg_b.start_or_attach(thread_key="k2", entity_id=1, app_id="circles", message="shared",
                              make_inner=gen(["e:{}\n"]),
                              nudge_tags={"connector_origin": ORIGIN})
    except TypeError:
        pass
    check("a nudge into a live session is pushed with its tags",
          pushed == [("s-2", "shared", {"connector_origin": ORIGIN})], repr(pushed))
    rest.set()
    await asyncio.wait_for(rec_b.done.wait(), 5)

    # ── 4. nudges nobody read go on carrying their origin ──────────────────
    for label, extra, want in (
            ("one", [("shared", {"connector_origin": ORIGIN})], ORIGIN),
            ("several", [("mine", None), ("shared", {"connector_origin": ORIGIN})],
             [None, ORIGIN])):
        calls: list = []

        def _follow(text, frame=None, _calls=calls):
            _calls.append((text, frame))
            return gen(["e:{}\n"])()
        reg_c = tr.TurnRegistry()
        reg_c._push_nudge = _push
        hold = asyncio.Event()
        rec_c, _ = reg_c.start_or_attach(
            thread_key="k3" + label, entity_id=1, app_id="circles", message="opening",
            make_inner=gen(["e:{}\n"], hold), make_followup=_follow)
        for text, tags in extra:
            try:
                reg_c.start_or_attach(thread_key="k3" + label, entity_id=1, app_id="circles",
                                      message=text, make_inner=gen(["e:{}\n"]),
                                      nudge_tags=tags)
            except TypeError:
                pass
        hold.set()      # the opening turn ends without ever having a session
        await asyncio.wait_for(rec_c.done.wait(), 5)
        for _ in range(100):
            if calls:
                break
            await asyncio.sleep(0.02)
        check(f"requeued ({label}): the next turn's frame carries the origin",
              bool(calls) and calls[0][1] == {"connector_origin": want}, repr(calls))

    # ── 5. session: push keeps only connector_origin; read before the drain ─
    sid = "sess-nudge-origin"
    S.push_nudge(sid, "plain words")
    try:
        S.push_nudge(sid, "forwarded words",
                     tags={"connector_origin": ORIGIN, "nudge": False, "app_id": "evil"})
    except TypeError as e:
        check("push_nudge takes tags", False, repr(e))
    tags = S.nudge_row_tags(sid) if hasattr(S, "nudge_row_tags") else None
    check("only connector_origin is kept, by text",
          tags == {"forwarded words": {"connector_origin": ORIGIN}}, repr(tags))
    got = S.consume_nudges_timed(sid)
    check("the drain still returns (text, sent-at) pairs, in order",
          [t for t, _ts in got][:1] == ["plain words"], repr(got))
    check("and the tags go with the queue",
          (S.nudge_row_tags(sid) if hasattr(S, "nudge_row_tags") else None) == {})
    src = turn_source(APP_ROOT)
    drain = src.split("_nudge_row_tags = nudge_row_tags(sess.session_id)")
    check("the drain reads the tags right before consume_nudges_timed",
          len(drain) == 2 and drain[1].lstrip().startswith(
              "_nudges_timed = consume_nudges_timed(sess.session_id)"))
    check("the nudge row merges them under nudge=True",
          "**(_nudge_row_tags.get(_nt) or {})," in src)


asyncio.run(main())
if FAILS:
    print(f"FAILED: connector nudge origin ({len(FAILS)} failure(s))")
    sys.exit(1)
print("PASSED: connector nudge origin (0 failure(s))")
