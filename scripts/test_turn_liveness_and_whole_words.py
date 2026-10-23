#!/usr/bin/env python3
"""
The turn is judged by LIVENESS, its reattach buffer by BYTES, and the
person's words go into a running turn WHOLE.

A host handles continuous 24/7 work on large data, efficiently.

  liveness    server/turn_registry.py stopped every turn at a 30-minute TOTAL
              (CSERIES_CHAT_TURN_TIMEOUT_S, default 1800). At the 27-32 s per
              model call measured on one Host's long turns that ended live work
              at ~60 calls. Now a turn is stopped only after TURN_IDLE_S with
              NO SIGN OF LIFE (no frame from the turn, no note_alive); a total
              exists only when a Host sets one. The record says "timeout" (and
              why) BEFORE the turn is cancelled, so the runtime reads a platform
              stop, never the person's Stop; the wire says what happened.
  buffer      the reattach buffer counted FRAMES (6,000), one per text delta, so
              one long write (a desktop design: ~30,000 deltas) overflowed it
              alone and a reattach replayed a hole. Now text deltas are coalesced
              on the buffer and the bound is bytes; a replay and a live tail
              both give back exactly the text that streamed.
  nudge       server/routes/circles.py cut a nudge into a running turn to 2,000
              characters, silently. Now it goes in whole.

Container-free: APP_ROOT=<repo> python3 scripts/test_turn_liveness_and_whole_words.py
No pod, no DB, no network. Exit 0 = all pass.
"""

import asyncio
import importlib.util
import json
import logging
import os
import sys
import time
import types

APP_ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_ROOT)
sys.path.insert(0, os.path.join(APP_ROOT, "brain"))
logging.basicConfig(level=logging.ERROR)

FAILS: list[str] = []


def check(name, ok, detail=""):
    print(f"[turn-liveness] {'ok  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def load_registry(env: dict):
    """A fresh copy of server/turn_registry.py loaded under `env` (its knobs
    are read at import)."""
    saved = {k: os.environ.get(k) for k in env}
    try:
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        spec = importlib.util.spec_from_file_location(
            f"turn_registry_copy_{len(sys.modules)}",
            os.path.join(APP_ROOT, "server", "turn_registry.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def text_of(frames) -> str:
    out = []
    for f in frames:
        if isinstance(f, str) and f.startswith("0:"):
            out.append(json.loads(f[2:]))
    return "".join(out)


async def wait_done(rec, s):
    try:
        await asyncio.wait_for(asyncio.shield(rec.done.wait()), s)
        return True
    except asyncio.TimeoutError:
        return False


# ── liveness, not a wall clock ────────────────────────────────────────────

async def liveness():
    tr = load_registry({"CSERIES_CHAT_TURN_TIMEOUT_S": None, "CSERIES_CHAT_TURN_IDLE_S": None})
    check("liveness: by default a turn has NO total wall clock (CSERIES_CHAT_TURN_TIMEOUT_S unset)",
          getattr(tr, "TURN_TIMEOUT_S", None) == float("inf"), repr(getattr(tr, "TURN_TIMEOUT_S", None)))
    check("liveness: the idle window defaults to 30 minutes with no sign of life (the agent reaper's figure)",
          getattr(tr, "TURN_IDLE_S", None) == 1800.0, repr(getattr(tr, "TURN_IDLE_S", None)))
    tr_opt = load_registry({"CSERIES_CHAT_TURN_TIMEOUT_S": "7200", "CSERIES_CHAT_TURN_IDLE_S": "600"})
    check("liveness: a Host may still opt in to a total, and set the idle window",
          tr_opt.TURN_TIMEOUT_S == 7200.0 and getattr(tr_opt, "TURN_IDLE_S", None) == 600.0,
          repr((tr_opt.TURN_TIMEOUT_S, getattr(tr_opt, "TURN_IDLE_S", None))))

    tr.TURN_IDLE_S = 0.4

    # 1. a turn that keeps producing frames runs well past the idle window
    async def busy():
        for i in range(30):              # ~1.5 s of steady output
            yield f'0:{json.dumps("x")}\n'
            await asyncio.sleep(0.05)
        yield "e:{}\n"
    reg = tr.TurnRegistry()
    rec, _ = reg.start_or_attach(thread_key="live", entity_id=1, app_id="t",
                                 message="m", make_inner=busy)
    ok = await wait_done(rec, 5)
    check("liveness: a turn that keeps emitting runs past the idle window to the end (no wall clock)",
          ok and rec.status == "done", rec.status)

    # 2. a silent turn is stopped for showing no sign of life; the record says
    #    so BEFORE the cancel reaches the turn (what session._turn_timed_out reads)
    seen = {}

    async def silent():
        try:
            yield '0:"started"\n'
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            r = tr.CURRENT_TURN_RECORD.get()
            seen["status_at_cancel"] = getattr(r, "status", None)
            raise
    reg2 = tr.TurnRegistry()
    t0 = time.time()
    rec2, _ = reg2.start_or_attach(thread_key="quiet", entity_id=1, app_id="t",
                                   message="m", make_inner=silent)
    ok = await wait_done(rec2, 4)
    took = time.time() - t0
    last = rec2.frames[-1] if rec2.frames else ""
    check("liveness: a turn with no sign of life for the idle window is stopped (status timeout, cause idle)",
          ok and rec2.status == "timeout" and getattr(rec2, "stop_cause", None) == "idle" and took < 3,
          repr((ok, rec2.status, getattr(rec2, "stop_cause", None), round(took, 2))))
    check("liveness: the record reads 'timeout' when the cancel reaches the turn (never mistaken for a Stop)",
          seen.get("status_at_cancel") == "timeout", repr(seen))
    check("liveness: the wire says what happened — no sign of life — not a ceiling",
          last.startswith("3:") and "no sign of life" in last and "ceiling" not in last, repr(last))

    # 3. a sign of life that is not a frame (note_alive, the tool loop's round
    #    mark) keeps a quiet turn alive; once it stops, the idle window applies
    async def quiet_with_session():
        yield "2:" + json.dumps([{"type": "session", "session_id": "s-alive", "turn_token": "tk"}]) + "\n"
        await asyncio.sleep(3600)
    # A wider idle window here than in 1-2: the signal is a 0.1 s heartbeat,
    # and under a loaded gate run (300+ gates in parallel) a sleep can
    # oversleep past a 0.4 s window — a flake, never what is under test.
    _idle_saved, tr.TURN_IDLE_S = tr.TURN_IDLE_S, 1.0
    reg3 = tr.TurnRegistry()
    tr._registry = reg3
    rec3, _ = reg3.start_or_attach(thread_key="noted", entity_id=1, app_id="t",
                                   message="m", make_inner=quiet_with_session)
    end = time.time() + 2.5
    while time.time() < end:
        getattr(tr, "note_alive", lambda _s: None)("s-alive")
        await asyncio.sleep(0.1)
    alive_while_noted = not rec3.done.is_set()
    ok = await wait_done(rec3, 3)
    check("liveness: note_alive() from the turn keeps it alive past the idle window",
          alive_while_noted, rec3.status)
    check("liveness: ...and once the signs of life stop, it is stopped after the idle window",
          ok and rec3.status == "timeout", rec3.status)
    tr.TURN_IDLE_S = _idle_saved
    tr._registry = None

    # 4. the person's Stop inside the idle window is a Stop, never a timeout
    reg4 = tr.TurnRegistry()

    async def hang():
        yield '0:"x"\n'
        await asyncio.sleep(3600)
    rec4, _ = reg4.start_or_attach(thread_key="stop", entity_id=1, app_id="t",
                                   message="m", make_inner=hang)
    await asyncio.sleep(0.1)
    await reg4.cancel(rec4.turn_id)
    check("liveness: the person's Stop reads as a Stop (cancelled, no platform cause)",
          rec4.status == "cancelled" and getattr(rec4, "stop_cause", None) is None,
          repr((rec4.status, getattr(rec4, "stop_cause", None))))

    # 5. the opt-in total still works and says it is the Host's total
    tr.TURN_TIMEOUT_S = 0.4
    tr.TURN_IDLE_S = 30
    reg5 = tr.TurnRegistry()
    rec5, _ = reg5.start_or_attach(thread_key="total", entity_id=1, app_id="t",
                                   message="m", make_inner=busy)
    ok = await wait_done(rec5, 4)
    last5 = rec5.frames[-1] if rec5.frames else ""
    check("liveness: an opt-in total stops even a busy turn, and the wire names the Host's total",
          ok and rec5.status == "timeout" and getattr(rec5, "stop_cause", None) == "total"
          and "total set for this Host" in last5, repr((rec5.status, last5)))
    tr.TURN_TIMEOUT_S = float("inf")

    # 6. the stopped-turn row names the cause
    try:
        line = tr.TurnRegistry.describe_stopped_turn("timeout", [], cause="idle")
    except TypeError as e:
        line = repr(e)
    check("liveness: the stopped-turn row says 'no sign of life', not a time limit",
          "no sign of life" in line, line)

    # 7. the offline runner's orphan threshold follows the idle window, not
    #    an infinite total (a dead process's rows are still reclaimed)
    try:
        import server.turn_registry as real_tr
        from mastercode.brain import offline_task_runner as otr
        s_idle, s_total = real_tr.TURN_IDLE_S, real_tr.TURN_TIMEOUT_S
        real_tr.TURN_IDLE_S, real_tr.TURN_TIMEOUT_S = 1800.0, float("inf")
        a = otr._orphan_after_s()
        real_tr.TURN_TIMEOUT_S = 999.0
        b = otr._orphan_after_s()
        real_tr.TURN_IDLE_S, real_tr.TURN_TIMEOUT_S = s_idle, s_total
        check("liveness: orphan rows are reclaimed after the idle window + 5 min (or the opt-in total, if sooner)",
              a == 2100.0 and b == 1299.0, repr((a, b)))
    except Exception as e:  # noqa: BLE001
        check("liveness: orphan threshold follows the idle window", False, repr(e))

    # 8. with no total, a live turn can hold a takeover past any fixed age: the
    #    takeover sweep's age backstop never takes a row a live turn of this
    #    process is carrying (it still takes a forgotten one, and the boot id
    #    still takes another process's at once)
    try:
        from mastercode.runtime import identity_hold as H

        class _P:
            def __init__(self):
                self.calls = []

            async def execute(self, sql, *args):
                self.calls.append((sql, args))
                return "UPDATE 0"
        h = H.Takeover(None, 1)
        h.add(4242, {})
        live_ids = H._in_flight_takeover_ids()
        p = _P()
        await H.sweep_taken_over(p, 1, 1860.0)
        sql, args = p.calls[-1]
        h._settled = True
        after_settle = H._in_flight_takeover_ids()
        check("liveness: the takeover sweep's age backstop skips rows a live turn of this process carries",
              4242 in live_ids and "NOT (id = ANY($6::bigint[]))" in sql and 4242 in list(args[5])
              and 4242 not in after_settle, repr((live_ids, sql[-120:], args[5:], after_settle)))
    except Exception as e:  # noqa: BLE001
        check("liveness: the takeover sweep's age backstop skips rows a live turn of this process carries",
              False, repr(e))


# ── the reattach buffer, by bytes, coalesced ──────────────────────────────

async def buffer():
    tr = load_registry({"CSERIES_TURN_BUFFER_BYTES": None})
    deltas = [f"<div class=\"w{i % 97}\">{i}</div>\n" for i in range(40000)]   # ~1.1 MB, 40k deltas
    want = "".join(deltas)
    gate = asyncio.Event()

    async def long_write():
        yield '9:{"toolCallId":"c1","toolName":"artifact","args":{}}\n'
        for i, d in enumerate(deltas):
            if i == 20000:
                gate.set()
                await asyncio.sleep(0.05)       # let the mid-stream tail attach
            yield "0:" + json.dumps(d) + "\n"
            if i % 2000 == 0:
                await asyncio.sleep(0)
        yield 'a:{"toolCallId":"c1","result":"ok"}\n'
        yield "0:" + json.dumps(" done") + "\n"
        yield "e:{}\n"

    reg = tr.TurnRegistry()
    rec, _ = reg.start_or_attach(thread_key="big", entity_id=1, app_id="t",
                                 message="m", make_inner=long_write)

    async def collect(r):
        return [f async for f in reg.tail(r)]
    early = asyncio.create_task(collect(rec))
    await gate.wait()
    mid = asyncio.create_task(collect(rec))
    await asyncio.wait_for(rec.done.wait(), 30)
    early_frames = await asyncio.wait_for(early, 5)
    mid_frames = await asyncio.wait_for(mid, 5)
    replay = await collect(rec)

    check("buffer: one ~1 MB write of 40,000 deltas does not overflow the reattach buffer",
          rec.truncated is False, f"truncated={rec.truncated}, frames={len(rec.frames)}")
    check("buffer: the buffer coalesces the deltas — a few frames, not one per delta",
          len(rec.frames) <= 64, f"{len(rec.frames)} frames")
    check("buffer: a reattach after the turn replays EXACTLY the text that streamed",
          text_of(replay) == want + " done", f"{len(text_of(replay))} vs {len(want) + 5}")
    check("buffer: a tail attached from the start gets exactly the same text",
          text_of(early_frames) == want + " done", f"{len(text_of(early_frames))}")
    check("buffer: a tail attached mid-write gets exactly the same text (snapshot + live, nothing twice)",
          text_of(mid_frames) == want + " done", f"{len(text_of(mid_frames))}")
    order = [f[:2] for f in replay[1:]]
    check("buffer: frame order survives coalescing (tool call, text, result, text, finish)",
          order[0] == "9:" and order[-1] == "e:" and "a:" in order
          and order.index("a:") > order.index("0:") and order[order.index("a:") + 1] == "0:",
          repr(order[:4] + ["..."] + order[-4:]))

    # The byte bound is honest when it is ever hit.
    tr.MAX_BUFFER_BYTES = 20000
    reg2 = tr.TurnRegistry()

    go = asyncio.Event()

    async def huge():
        await go.wait()
        for d in deltas[:5000]:
            yield "0:" + json.dumps(d) + "\n"
        yield "e:{}\n"
    rec2, _ = reg2.start_or_attach(thread_key="huge", entity_id=1, app_id="t",
                                   message="m", make_inner=huge)
    live_task = asyncio.create_task(collect(rec2))   # attached before any output
    await asyncio.sleep(0.05)
    go.set()
    await asyncio.wait_for(rec2.done.wait(), 10)
    live = await asyncio.wait_for(live_task, 5)
    late = await collect(rec2)
    check("buffer: past the byte bound the buffer stops and says so (truncated, a note where the hole is)",
          rec2.truncated is True and any("replay buffer is full" in f for f in late),
          repr((rec2.truncated, late[-1:])))
    check("buffer: ...while the live stream still carries every frame",
          text_of(live) == "".join(deltas[:5000]), f"{len(text_of(live))}")


# ── the person's words into a running turn, whole ─────────────────────────

async def whole_words():
    sys.modules.setdefault("server.main", types.ModuleType("server.main"))
    import server.routes.circles as CR
    from mastercode.runtime import session as S

    class Req:
        def __init__(self, body):
            self.body = body

        async def json(self):
            return self.body

    async def _audit(**_kw):
        return None
    saved = (CR._entity_id, CR._audit)
    CR._entity_id, CR._audit = (lambda: 9003), _audit
    words = ("Please use the blue palette and keep the header. " * 120).strip()   # ~6 KB
    try:
        out = await CR.nudge(Req({"session_id": "s-whole-words", "text": words}))
    finally:
        CR._entity_id, CR._audit = saved
    got = S.consume_nudges("s-whole-words")
    check("nudge: a nudge into a running turn is queued WHOLE (never cut at 2,000 characters)",
          got == [words] and out.get("queued_chars") == len(words),
          repr(([len(g) for g in got], out.get("queued_chars"), len(words))))


async def main():
    # Each section runs whatever the one before it did: a crash is a FAIL of
    # that section, never a reason to skip the next.
    for section in (liveness, buffer, whole_words):
        try:
            await section()
        except Exception as e:  # noqa: BLE001
            check(f"{section.__name__}: ran to the end", False, repr(e))


if __name__ == "__main__":
    asyncio.run(main())
    if FAILS:
        print(f"[turn-liveness] {len(FAILS)} FAILED: {', '.join(FAILS)}")
        sys.exit(1)
    print("[turn-liveness] all checks passed")
    sys.exit(0)
