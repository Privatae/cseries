#!/usr/bin/env python3
"""A Stop is never swallowed.

The Stop button and the turn's own time limit stop a turn the same way: they
cancel the turn's task (server/turn_registry.py, TurnRegistry.cancel and
_expire). That task runs brain_service._send_message_inner, whose frame loop
waits up to 2 s for a progress event, and on a quiet stretch cancels its
getter and awaits it. The getter's own CancelledError is expected there. A
cancel of the turn's task that lands in the same await arrives as the same
exception (Task.cancel hands it to the task being awaited), and the loop used
to drop both, so the turn ran on after the person pressed Stop.
backup_ship.ship_now had the same await on its heartbeat task, so a ship
cancelled in that moment went on to send the backup's key and record itself
done.

Container-free: APP_ROOT=<repo> python3 scripts/test_stop_never_swallowed.py.
No pod, no DB, no model, no network, no signals. A task factory marks the
child the code under test cancels and then awaits (the frame loop's getter,
the ship's heartbeat), and its first cancel starts the Stop at once, so the
Stop lands while the owner awaits that child: before the child's cancel has
run, or after it.

  1. the turn: the real _send_message_inner inside the real turn registry,
     with a stand-in runtime whose one tool call sends no progress, so the
     loop reaches its 2 s timeout. A Stop in the window stops the turn: the
     runtime task is cancelled, nothing runs on, the turn ends "cancelled"
     and says so, and the Stop route answers "cancelled"
  2. controls for 1: a Stop while the loop waits stops the turn (the path
     that always worked), and a quiet stretch past the 2 s wait with no Stop
     does NOT end the turn. Re-raising every CancelledError at the getter
     would fail this one: the getter's own cancel arrives on every quiet
     stretch.
  3. the ship: the real ship_now to an S3 destination, its storage and
     database calls stubbed. A cancel in the window stops the ship: the key
     is not sent, the row is recorded failed rather than done, the cancel
     reaches the caller, and the source is still closed. Control: a ship
     with no cancel sends both and records done.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

APP_ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_ROOT)
sys.path.insert(0, os.path.join(APP_ROOT, "brain"))

from server import backup_ship as SHIP  # noqa: E402
from server import brain_service as BS  # noqa: E402
from server import turn_registry as TR  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[stop-never-swallowed] {'ok  ' if ok else 'FAIL'} {name}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


# ── the window: which child, whose Stop ─────────────────────────────────────
# {"owner": the task under test, "match": coro -> bool, "stop": callable,
#  "stop_first": bool}; "child" is filled in when the window opens.
ARM: dict = {}


class _Armed(asyncio.Task):
    """The child the owner cancels and then awaits. Its first cancel sets off
    the Stop, so the Stop lands while the owner awaits it."""

    def cancel(self, msg=None):
        stop = ARM.pop("stop", None)
        if stop is None:
            return super().cancel(msg)
        ARM["child"] = self
        loop = asyncio.get_running_loop()
        if ARM.get("stop_first"):
            # Ahead of the child's own wake-up: the owner's cancel goes to the
            # child (Task.cancel hands it to the future it awaits).
            loop.call_soon(stop)
            return super().cancel(msg)
        # After it: the child has finished when the Stop lands, and the owner
        # has not resumed yet.
        out = super().cancel(msg)
        loop.call_soon(stop)
        return out


def _factory(loop, coro, **kw):
    if (ARM.get("owner") is not None and asyncio.current_task(loop) is ARM["owner"]
            and ARM["match"](coro)):
        return _Armed(coro, loop=loop, **kw)
    return asyncio.Task(coro, loop=loop, **kw)


def _where(owner, seen: dict) -> None:
    """Where the owner stands when the Stop lands."""
    seen["awaits_child"] = getattr(owner, "_fut_waiter", None) is ARM.get("child")
    seen["child_done"] = ARM["child"].done()


async def _until(pred, timeout: float) -> bool:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not pred() and loop.time() < end:
        await asyncio.sleep(0.01)
    return bool(pred())


# ── 1/2. the turn ───────────────────────────────────────────────────────────
AFTER = "work the Stop should have prevented"
# The frame the registry appends for a stopped turn (TurnRegistry._run).
STOPPED = "3:" + json.dumps("(stopped — nothing is still running in the background)") + "\n"


class Runtime:
    """Stands in for EntityRuntime.send_message: one long tool call that sends
    no progress until the test releases it."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False
        self.finished = False

    async def send_message(self, *, progress_cb=None, **_kw):
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if progress_cb is not None:
            await progress_cb({"event": "text_delta", "text": AFTER})
        self.finished = True
        return {"content": AFTER}


def _is_getter(coro) -> bool:
    return getattr(coro, "__qualname__", "") == "Queue.get"


async def run_turn(stop: str | None):
    """One turn through the real registry. `stop`: None (no Stop),
    "in-wait" (while the loop waits), "window-first" / "window-after" (while
    it awaits the getter it cancelled, before / after that cancel has run)."""
    rt = Runtime()
    BS._runtime = rt
    reg = TR.TurnRegistry()
    rec, _mode = reg.start_or_attach(
        thread_key=f"t2952|{stop}", entity_id=1, app_id="t2952-gate", message="go",
        make_inner=lambda: BS._send_message_inner(message="go", entity_id=1, app_id="t2952-gate"))
    seen: dict = {}
    loop = asyncio.get_running_loop()

    def stop_now() -> None:
        _where(rec.task, seen)
        # The registry's own Stop, started eagerly: its first step (record the
        # Stop, cancel the turn's task) runs here, in the window.
        seen["cancel"] = asyncio.Task(reg.cancel(rec.turn_id), loop=loop, eager_start=True)

    ARM.clear()
    if stop in ("window-first", "window-after"):
        ARM.update(owner=rec.task, match=_is_getter, stop=stop_now,
                   stop_first=(stop == "window-first"))
    await asyncio.wait_for(rt.started.wait(), 5)
    if stop == "in-wait":
        await asyncio.sleep(0.3)
        seen["cancel"] = asyncio.create_task(reg.cancel(rec.turn_id))
    elif stop is None:
        await asyncio.sleep(2.6)          # one quiet stretch past the 2 s wait
    else:
        await _until(lambda: "cancel" in seen, 5.0)
    if stop is not None:
        await _until(lambda: rt.cancelled or rec.done.is_set(), 1.0)
    snap = {"runtime_cancelled": rt.cancelled, "status": rec.status}
    rt.release.set()                      # a turn the Stop missed runs on to here
    await asyncio.wait_for(rec.done.wait(), 5)
    out = (await seen["cancel"]) if "cancel" in seen else None
    ARM.clear()
    return rt, rec, snap, out, seen


# ── 3. the ship ─────────────────────────────────────────────────────────────
SRC = {"export_id": 1, "s3_key": "exports/1.tar.age", "url": "https://source/artifact",
       "size_bytes": 5, "sha256": "aa", "sidecar_url": "https://source/key",
       "sidecar_bytes": 3, "sidecar_sha256": "bb"}


class Handle:
    def __init__(self, url: str) -> None:
        self.url = url
        self.closed = False

    def close(self) -> None:
        self.closed = True


async def run_ship(stop: str | None):
    """ship_now to an S3 destination with its storage and database stubbed.
    `stop`: None, or "window-first" / "window-after" (a cancel of the ship
    while it awaits the heartbeat it cancelled)."""
    calls: dict = {"sent": [], "finish": [], "handles": []}

    def open_source(url):
        calls["handles"].append(Handle(url))
        return calls["handles"][-1]

    def ship_blocking(creds, target, src, name, size, want_sha, progress=None):
        # An upload takes a while, so the heartbeat is running (asleep
        # between beats) when the ship cancels it, as it is in production.
        time.sleep(0.05)
        calls["sent"].append(name)
        return {"bytes": size, "key": name}

    async def finish(pool, log_id, *, state, sent, reason=None, detail=""):
        calls["finish"].append(state)

    async def heartbeat(pool, log_id, progress):
        await asyncio.sleep(3600)

    async def none(*_a, **_k):
        return None

    async def begin(*_a, **_k):
        return 7

    async def signed(src):
        return src

    async def credential(*_a, **_k):
        return {}

    stubs = {
        "validate_target": lambda t: t,
        "object_name": lambda export_id, s3_key, snapshot_time=None: "artifact",
        "destination_key": lambda t: "bucket/prefix",
        "sidecar_name": lambda name: name + ".key",
        "_begin": begin, "_signed": signed, "_s3_credential": credential,
        "_open_source": open_source, "_beat": none, "_s3_heartbeat": heartbeat,
        "_ship_to_s3_blocking": ship_blocking, "_finish": finish, "_drop_staged": none,
    }
    saved = {k: getattr(SHIP, k) for k in stubs}
    for k, v in stubs.items():
        setattr(SHIP, k, v)
    seen: dict = {}
    try:
        ship = asyncio.create_task(SHIP.ship_now(
            object(), 9003, {"kind": "s3", "cred_name": "mine"}, src=dict(SRC)))

        def stop_now() -> None:
            _where(ship, seen)
            ship.cancel()                 # shutdown, or the caller going away

        ARM.clear()
        if stop is not None:
            ARM.update(owner=ship, match=lambda c: getattr(c, "cr_code", None) is heartbeat.__code__,
                       stop=stop_now, stop_first=(stop == "window-first"))
        await asyncio.wait({ship}, timeout=5)
        outcome = ("cancelled" if ship.cancelled() else "pending" if not ship.done()
                   else "raised" if ship.exception() is not None else "returned")
        if not ship.done():
            ship.cancel()
        return calls, outcome, seen
    finally:
        ARM.clear()
        for k, v in saved.items():
            setattr(SHIP, k, v)


async def main() -> None:
    asyncio.get_running_loop().set_task_factory(_factory)

    # ── 1. the turn, a Stop in the window ───────────────────────────────
    for stop, when in (("window-first", "before the getter's cancel has run"),
                       ("window-after", "after the getter's cancel has run")):
        rt, rec, snap, out, seen = await run_turn(stop)
        tag = f"1 ({when})"
        check(f"{tag}: the Stop landed while the turn awaited the getter it had just cancelled",
              seen.get("awaits_child") is True
              and seen.get("child_done") is (stop == "window-after"), repr(seen))
        check(f"{tag}: the Stop cancels the runtime task, so the work stops",
              snap["runtime_cancelled"] is True and not rt.finished, repr((snap, rt.finished)))
        check(f"{tag}: nothing the turn did after the Stop reaches the wire",
              not any(AFTER in f for f in rec.frames), repr(rec.frames[-4:]))
        check(f"{tag}: the turn ends cancelled and says so",
              rec.status == "cancelled" and STOPPED in rec.frames,
              repr((rec.status, rec.frames[-3:])))
        check(f"{tag}: the Stop route answers cancelled",
              out == {"ok": True, "status": "cancelled"}, repr(out))

    # ── 2. controls for the turn ────────────────────────────────────────
    rt, rec, snap, out, _seen = await run_turn("in-wait")
    check("2a: a Stop while the loop waits stops the turn",
          snap["runtime_cancelled"] is True and rec.status == "cancelled"
          and out == {"ok": True, "status": "cancelled"} and not rt.finished,
          repr((snap, rec.status, out)))

    rt, rec, snap, out, _seen = await run_turn(None)
    check("2b: a quiet stretch past the 2 s wait, with no Stop, does not end the turn",
          snap == {"runtime_cancelled": False, "status": "running"}, repr(snap))
    check("2b: that turn finishes its work and its reply reaches the wire",
          rt.finished and rec.status == "done" and any(AFTER in f for f in rec.frames),
          repr((rt.finished, rec.status, rec.frames[-4:])))

    # ── 3. the ship ─────────────────────────────────────────────────────
    for stop, when in (("window-first", "before the heartbeat's cancel has run"),
                       ("window-after", "after the heartbeat's cancel has run")):
        calls, outcome, seen = await run_ship(stop)
        tag = f"3 ({when})"
        check(f"{tag}: the cancel landed while the ship awaited the heartbeat it had just cancelled",
              seen.get("awaits_child") is True
              and seen.get("child_done") is (stop == "window-after"), repr(seen))
        check(f"{tag}: the cancelled ship sends nothing more (the key is not sent)",
              calls["sent"] == ["artifact"], repr(calls["sent"]))
        check(f"{tag}: its row is recorded failed, never done",
              calls["finish"] == ["failed"], repr(calls["finish"]))
        check(f"{tag}: the cancel reaches the ship's caller",
              outcome == "cancelled", outcome)
        check(f"{tag}: the source is still closed",
              bool(calls["handles"]) and all(h.closed for h in calls["handles"]),
              repr([(h.url, h.closed) for h in calls["handles"]]))
    calls, outcome, _seen = await run_ship(None)
    check("3 (control): a ship nobody cancels sends the backup and its key and records done",
          calls["sent"] == ["artifact", "artifact.key"] and calls["finish"] == ["done"]
          and outcome == "returned" and all(h.closed for h in calls["handles"]),
          repr((calls, outcome)))


if __name__ == "__main__":
    asyncio.run(main())
    if FAILS:
        print(f"[stop-never-swallowed] {len(FAILS)} FAILED")
        sys.exit(1)
    print("[stop-never-swallowed] all passed")
