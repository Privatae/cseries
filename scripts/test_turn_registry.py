#!/usr/bin/env python3
"""
Turn-registry gate (server/turn_registry.py, smoke).

Structural verification of the detached-turn machinery that fixed the
phone-disconnect failure (turn dies in the UI, keeps running
invisibly, retries stack duplicate turns):

  1. detach     — a turn runs to completion with NO tail attached; frames buffer
  2. tail       — replay-from-0 + live-follow yields the same frames, and a
                  dropped tail never kills the turn
  3. lock       — identical resend while running = reattached (same record);
                  different text = nudged (never a second turn)
  4. nudge      — pre-session nudges queue and drain on the session frame;
                  every nudge lands on the buffer as a 2:[{type:"nudge"}] frame
  5. cancel     — tears the inner generator down (its finally runs → the
                  runtime task dies with it), appends a terminal 3: frame
  6. memory     — no count of turns: a turn the platform
                   starts for the Host is refused with TurnCapError, loudly,
                   only when the Host's memory does not hold one more
                   (host_memory); a person's turn always starts
  7. timeout    — a turn past the OPT-IN total TURN_TIMEOUT_S is torn down
                  like a cancel (the default is no total: liveness, see
                  scripts/test_turn_liveness_and_whole_words.py)
  8. key        — compose_thread_key is the single composition both the send
                  path and the lookup routes use

Stdlib-only; loads server/turn_registry.py straight from file (the
mastercode nudge import inside _push_nudge is monkeypatched — same technique
as scripts/test_host_sdk_wall.py). Exit 0 = all pass.
"""

import asyncio
import json
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REG_PATH = os.path.join(HERE, "..", "server", "turn_registry.py")
# The registry asks runtime/host_memory whether a turn of the Host's own fits.
sys.path.insert(0, os.path.join(HERE, "..", "brain"))

spec = importlib.util.spec_from_file_location("turn_registry", REG_PATH)
tr = importlib.util.module_from_spec(spec)
sys.modules["turn_registry"] = tr  # dataclass field resolution needs the entry
spec.loader.exec_module(tr)

FAILS = []


def check(name: str, ok: bool, detail: str = ""):
    tag = "ok  " if ok else "FAIL"
    print(f"[turn-registry] {tag} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def make_gen(frames, *, gate=None, hang=False, finally_flag=None):
    """An inner-generator factory shaped like _send_message_inner: yields
    frames, optionally waits on a gate between them, flags its finally."""
    async def _gen():
        try:
            for f in frames:
                if gate is not None:
                    await gate.wait()
                    gate.clear()
                yield f
            if hang:
                await asyncio.sleep(3600)
        finally:
            if finally_flag is not None:
                finally_flag["ran"] = True
    return _gen


async def collect_tail(reg, rec, n=None, mode="attached", from_seq=0, mark_replayed=False):
    out = []
    async for frame in reg.tail(rec, mode=mode, from_seq=from_seq, mark_replayed=mark_replayed):
        out.append(frame)
        if n is not None and len(out) >= n:
            break
    return out


async def main():
    # ── 1. detach: turn completes with no tail attached ──────────────────
    reg = tr.TurnRegistry()
    rec, mode = reg.start_or_attach(
        thread_key="k1", entity_id=1, app_id="t", message="hello",
        make_inner=make_gen(['0:"a"\n', '0:"b"\n', "e:{}\n"]))
    check("start mode", mode == "started", mode)
    await asyncio.wait_for(rec.done.wait(), 5)
    check("detached completion", rec.status == "done", rec.status)
    # Consecutive text deltas are coalesced on the BUFFER: the same text, one
    # frame instead of one per delta.
    check("frames buffered (text deltas coalesced)", rec.frames == ['0:"ab"\n', "e:{}\n"],
          repr(rec.frames))

    # ── 2. tail: full replay after completion; dropped tail ≠ dead turn ──
    frames = await collect_tail(reg, rec)
    check("tail meta first",
          frames and frames[0].startswith("2:") and '"type": "turn"' in frames[0],
          repr(frames[:1]))
    check("tail replay", frames[1:] == rec.frames, repr(frames))
    # The reattach route's mark: after the replay, before anything live, so a
    # client can tell words written before it attached from words arriving.
    marked = await collect_tail(reg, rec, mode="reattached", mark_replayed=True)
    check("mark_replayed: the replay, then the mark",
          marked[1:-1] == rec.frames and json.loads(marked[-1][2:]) == [{"type": "replayed"}], repr(marked))
    gate_m = asyncio.Event()
    reg_m = tr.TurnRegistry()
    rec_m, _ = reg_m.start_or_attach(
        thread_key="km", entity_id=1, app_id="t", message="m",
        make_inner=make_gen(['0:"before"\n', "f:{}\n", '0:"after"\n', "e:{}\n"], gate=gate_m))
    gate_m.set()
    for _ in range(50):
        if rec_m.frames:
            break
        await asyncio.sleep(0.02)
    t_m = asyncio.create_task(collect_tail(reg_m, rec_m, n=5, mode="reattached", mark_replayed=True))
    await asyncio.sleep(0.05)
    for _ in range(3):
        gate_m.set()
        await asyncio.sleep(0.05)
    got_m = await asyncio.wait_for(t_m, 5)
    mark_at = next((i for i, f in enumerate(got_m) if '"replayed"' in f), -1)
    check("mark_replayed: live frames follow the mark, past ones precede it",
          mark_at > 0 and '"before"' in "".join(got_m[1:mark_at]) and '"after"' in "".join(got_m[mark_at + 1:]),
          repr(got_m))
    plain = await collect_tail(reg, rec)
    check("without mark_replayed no mark (POST /messages, the brain route)",
          not any('"replayed"' in f for f in plain), repr(plain))

    gate = asyncio.Event()
    reg2 = tr.TurnRegistry()
    rec2, _ = reg2.start_or_attach(
        thread_key="k2", entity_id=1, app_id="t", message="m",
        make_inner=make_gen(['0:"x"\n', '0:"y"\n', "e:{}\n"], gate=gate))
    t_tail = asyncio.create_task(collect_tail(reg2, rec2, n=2))
    gate.set()
    got = await asyncio.wait_for(t_tail, 5)   # meta + first frame, then DROPPED
    check("live tail follows", got[-1] == '0:"x"\n', repr(got))
    gate.set()
    gate.set()  # let remaining frames flow
    # The dropped tail must not have killed the turn:
    for _ in range(50):
        if rec2.done.is_set():
            break
        gate.set()
        await asyncio.sleep(0.02)
    check("dropped tail keeps turn alive",
          rec2.done.is_set() and rec2.status == "done", rec2.status)

    # ── 3. per-thread lock: identical resend reattaches, different nudges ─
    nudges = []
    reg3 = tr.TurnRegistry()
    reg3._push_nudge = lambda sid, text: nudges.append((sid, text))
    gate3 = asyncio.Event()
    rec3, _ = reg3.start_or_attach(
        thread_key="k3", entity_id=1, app_id="t", message="same text",
        make_inner=make_gen(
            ['2:[{"type": "session", "session_id": "s-1"}]\n', "e:{}\n"],
            gate=gate3))
    r_again, mode_again = reg3.start_or_attach(
        thread_key="k3", entity_id=1, app_id="t", message="same text",
        make_inner=make_gen(["e:{}\n"]))
    check("identical resend reattaches",
          mode_again == "reattached" and r_again is rec3, mode_again)
    r_nudge, mode_nudge = reg3.start_or_attach(
        thread_key="k3", entity_id=1, app_id="t", message="different text",
        make_inner=make_gen(["e:{}\n"]))
    check("different text nudges", mode_nudge == "nudged" and r_nudge is rec3,
          mode_nudge)
    check("nudge frame on buffer",
          any(f.startswith("2:") and '"nudge"' in f for f in rec3.frames),
          repr(rec3.frames))
    check("nudge queued pre-session", rec3.pending_nudges == ["different text"],
          repr(rec3.pending_nudges))
    gate3.set()  # release the session frame → drains the pending nudge
    for _ in range(50):
        if nudges:
            break
        await asyncio.sleep(0.02)
    check("nudge drains on session frame", nudges == [("s-1", "different text")],
          repr(nudges))
    gate3.set()
    await asyncio.wait_for(rec3.done.wait(), 5)

    # ── 5. cancel: generator finally runs, terminal 3: frame, status ─────
    flag = {"ran": False}
    reg5 = tr.TurnRegistry()
    rec5, _ = reg5.start_or_attach(
        thread_key="k5", entity_id=1, app_id="t", message="m",
        make_inner=make_gen(['0:"partial"\n'], hang=True, finally_flag=flag))
    for _ in range(50):
        if rec5.frames:
            break
        await asyncio.sleep(0.02)
    out = await reg5.cancel(rec5.turn_id)
    check("cancel ok", out.get("ok") and out.get("status") == "cancelled",
          repr(out))
    check("cancel tears down inner generator", flag["ran"] is True)
    check("cancel is loud on the wire",
          any(f.startswith("3:") for f in rec5.frames), repr(rec5.frames))
    check("cancelled turn leaves active set",
          reg5.active_for("k5") is None)

    # ── 6. memory, not a count ───────────────────────────────────────────
    import tempfile
    from mastercode.runtime import host_memory as hm
    reg6 = tr.TurnRegistry()
    old_root = hm.CGROUP_ROOT
    cg = tempfile.mkdtemp(prefix="cg-")

    def _cg(limit_mb, used_mb, inactive_mb=0):
        for name, v in (("memory.max", str(limit_mb * 1048576)),
                        ("memory.current", str(used_mb * 1048576)),
                        ("memory.stat", f"anon 1\ninactive_file {inactive_mb * 1048576}\n")):
            with open(os.path.join(cg, name), "w") as f:
                f.write(v)
    hm.CGROUP_ROOT = cg
    try:
        _cg(2048, 200)
        for i in range(12):        # far past the old ceiling of 8: memory holds them
            reg6.start_or_attach(thread_key=f"m{i}", entity_id=1, app_id="t", message="m",
                                 make_inner=make_gen(['0:"x"\n'], hang=True), system=True)
        check("no count of turns: twelve of the Host's own turns start when memory holds them",
              len(reg6._active) == 12, len(reg6._active))
        _cg(2048, 2048 - 12 * 12)   # what is free now holds no more beside the twelve running
        try:
            reg6.start_or_attach(thread_key="m-full", entity_id=1, app_id="t", message="m",
                                 make_inner=make_gen(["e:{}\n"]), system=True)
            check("a turn of the Host's own that does not fit is refused", False, "no TurnCapError")
        except tr.TurnCapError as e:
            check("a turn of the Host's own that does not fit is refused, saying why in memory terms",
                  hm.NO_ROOM in str(e) and "MB" in str(e), str(e))
        rec_p, mode_p = reg6.start_or_attach(thread_key="person", entity_id=1, app_id="t",
                                             message="hi", make_inner=make_gen(["e:{}\n"]))
        check("a person's turn always starts, memory full or not", mode_p == "started", mode_p)
        _cg(2048, 1000, inactive_mb=900)
        check("reclaimable file cache is not counted as the Host's working set",
              hm.working_set_bytes() == 100 * 1048576, hm.working_set_bytes())
        os.remove(os.path.join(cg, "memory.max"))
        check("no readable limit: nothing is held back", hm.turns_that_fit() is None and hm.one_more_fits())
    finally:
        hm.CGROUP_ROOT = old_root
        for k in list(reg6._active):
            rec_c = reg6.active_for(k)
            if rec_c:
                await reg6.cancel(rec_c.turn_id)

    # ── 7. timeout ───────────────────────────────────────────────────────
    reg7 = tr.TurnRegistry()
    old_to = tr.TURN_TIMEOUT_S
    tr.TURN_TIMEOUT_S = 0.2
    flag7 = {"ran": False}
    try:
        rec7, _ = reg7.start_or_attach(
            thread_key="k7", entity_id=1, app_id="t", message="m",
            make_inner=make_gen(['0:"x"\n'], hang=True, finally_flag=flag7))
        await asyncio.wait_for(rec7.done.wait(), 5)
        check("timeout status", rec7.status == "timeout", rec7.status)
        check("timeout tears down inner generator", flag7["ran"] is True)
        check("timeout is loud on the wire",
              any(f.startswith("3:") and "total set for this Host" in f for f in rec7.frames),
              repr(rec7.frames))
    finally:
        tr.TURN_TIMEOUT_S = old_to

    # ── 9. the requeued turn keeps its room; a turn knows its
    #       own record even after the thread's slot moved on ──────────────
    reg9 = tr.TurnRegistry()
    gate9 = asyncio.Event()
    seen9 = {}

    async def held_inner():
        seen9["own"] = tr.CURRENT_TURN_RECORD.get()

        async def child():
            seen9["child"] = tr.CURRENT_TURN_RECORD.get()
        await asyncio.create_task(child())
        await gate9.wait()
        yield "e:{}\n"

    async def follow_inner(_text):
        yield "e:{}\n"

    room9 = {"app_id": "circles", "thread": "t9"}
    rec9a, _ = reg9.start_or_attach(
        thread_key="k9", entity_id=1, app_id="circles", message="first",
        make_inner=lambda: held_inner(), make_followup=follow_inner, room=room9)
    await asyncio.sleep(0.05)
    reg9.start_or_attach(thread_key="k9", entity_id=1, app_id="circles", message="second",
                         make_inner=make_gen(["e:{}\n"]))
    gate9.set()
    await asyncio.wait_for(rec9a.done.wait(), 5)
    await asyncio.sleep(0.05)
    rec9b = reg9.active_for("k9") or next(
        (r for r in reg9._by_id.values() if r is not rec9a), None)
    check("a turn requeued after an identity hold keeps the room it was sent in",
          rec9b is not None and rec9b.room == room9, repr(getattr(rec9b, "room", None)))
    check("inside a turn (and a task it starts) CURRENT_TURN_RECORD is that turn's own record",
          seen9.get("own") is rec9a and seen9.get("child") is rec9a,
          repr((seen9.get("own"), seen9.get("child"))))
    if rec9b is not None and not rec9b.done.is_set():
        await asyncio.wait_for(rec9b.done.wait(), 5)

    # ── 8. one key composition ───────────────────────────────────────────
    # Keyed by the conversation, not the app
    # (scripts/test_turn_lock_by_scope.py is the full gate).
    check("thread key shape",
          tr.compose_thread_key(3, "circles", None) == "circles|3|none"
          and tr.compose_thread_key(3, "workbench", 12) == "project|3|12"
          and tr.compose_thread_key(3, "circles", None, thread_id="t") == "thread|3|t"
          and tr.compose_thread_key(3, None, None) == "chat|3|none")


if __name__ == "__main__":
    asyncio.run(main())
    if FAILS:
        print(f"[turn-registry] {len(FAILS)} FAILED: {', '.join(FAILS)}")
        sys.exit(1)
    print("[turn-registry] all checks passed")
    sys.exit(0)
