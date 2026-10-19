#!/usr/bin/env python3
"""A Stop of a relay worker stops it, and the job it holds is answered.

The bastion opens worker sockets to the Host and each one carries one job: an
egress connection (server/network/egress_relay.py), an embed (embed_relay.py), a
harness run or a terminal session (nac_harness.py), a management op
(nac_mgmt.py). A worker that is handed its job cancels its idle receive and
awaits it before it runs the job. The receive's own CancelledError is expected
there. A cancel of the worker's task (a Stop) that lands in the same await
arrives as the same exception, because Task.cancel hands it to the task being
awaited, and every worker dropped both, so it ran the job anyway.

Stopping is half of it. The job's waiter is another task, and the egress gate
waits on it with no timeout, so a worker that stops must answer its job. Two
neighbouring moments had the same hole: a Stop while the worker waits idle left
its job slot open, so the next dispatch handed its job to a worker that had
gone; and a Stop just after a job was handed over left that job unanswered.

Container-free: APP_ROOT=<repo> python3 scripts/test_relay_workers_keep_stop.py.
No pod, no DB, no network, no signals. The real workers and relays with a fake
worker socket that answers a job at once. A task factory marks the receive the
worker cancels and then awaits, and its first cancel starts the Stop, so the
Stop lands while the worker awaits it: before the receive's cancel has run, or
after it (the technique of scripts/test_stop_never_swallowed.py). The waiter is
the real one: dispatch for egress, embed, harness runs and management ops;
acquire and the real BastionSession for a terminal.

A job that never reached the box is answered the way its module answers a job
that never started: egress dispatch returns False (the gate refuses a pinned
request with a 503 and sends an unpinned one the platform way), an embed None,
a harness run None (502, "not started"), a management op no-worker (502, and
the ship runner sends it again), and a terminal ends with "could not reach the
machine" and exit 1.

For each of the five kinds of job:
  1. a Stop in that window: the worker stops, nothing of the job reaches the
     box, and the job's waiter is answered at once with that kind's failure
  2. control: no Stop. The receive's own cancel does not stop the worker; the
     job runs and its waiter gets the box's answer
  3. a Stop while the worker waits idle: the next job goes to the next worker,
     not to the stopped one
  4. a Stop just after the job was handed over, before the worker saw it: the
     worker stops and the job's waiter is answered at once with a failure
  5. a Stop just after the worker sent the job's first frame: the box has the
     job, so the waiter is never told it never started. A management op is "no
     reply" once its budget is out (the ship runner waits for its receipt and
     never sends it again), a harness run is stopped 'over', an embed is None
     at its budget, egress dispatch returns True at once (the gate sends
     nothing the platform way) and a terminal ends at once with exit 1 and no
     "could not reach the machine"
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

APP_ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_ROOT)
sys.path.insert(0, os.path.join(APP_ROOT, "brain"))

from server.network import egress_relay as EG  # noqa: E402
from server.network import embed_relay as EM  # noqa: E402
from server.network import nac_harness as NH  # noqa: E402
from server.network import nac_mgmt as NM  # noqa: E402
from server.network import session_lane as SL  # noqa: E402

for _name in ("cseries.egress", "cseries.embed_relay", "cseries.harness", "cseries.mgmt"):
    logging.getLogger(_name).setLevel(logging.WARNING)

FAILS: list[str] = []
TOKEN = 7
# Each waiter's own timeout, far past SOON: only a job that is answered ends a
# wait inside SOON. Embed, harness and management waits have a timeout; egress
# and a terminal's reader have none.
WAIT = 30.0
SOON = 2.0
PENDING = "<still waiting>"
# Scenario 5's waiter timeout. A job the box already has is left to its waiter,
# and embed, harness and management waits end on their timeout, so this one is
# short: inside SOON, and far past the moment the worker stops.
BUDGET = 0.6


def check(name: str, ok, detail: str = "") -> None:
    ok = bool(ok)
    print(f"[relay-keep-stop] {'ok  ' if ok else 'FAIL'} {name}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


# ── the window: the receive the worker cancels, and the Stop it sets off ─────
# {"owner": the worker's task, "stop": callable, "stop_first": bool}; "child"
# is filled in when the window opens.
ARM: dict = {}


class _Armed(asyncio.Task):
    """The worker's idle receive. Its first cancel (the worker's own, once it
    has a job) sets off the Stop, so the Stop lands while the worker awaits it."""

    def cancel(self, msg=None):
        stop = ARM.pop("stop", None)
        if stop is None:
            return super().cancel(msg)
        ARM["child"] = self
        loop = asyncio.get_running_loop()
        if ARM.get("stop_first"):
            # Ahead of the receive's own wake-up: the Stop goes to the receive
            # (Task.cancel hands it to the future the worker awaits).
            loop.call_soon(stop)
            return super().cancel(msg)
        # After it: the receive has finished when the Stop lands, and the
        # worker has not resumed yet.
        out = super().cancel(msg)
        loop.call_soon(stop)
        return out


def _factory(loop, coro, **kw):
    if (ARM.get("owner") is not None and asyncio.current_task(loop) is ARM["owner"]
            and getattr(coro, "__qualname__", "") == "FakeWS.receive"):
        return _Armed(coro, loop=loop, **kw)
    return asyncio.Task(coro, loop=loop, **kw)


# ── fakes ────────────────────────────────────────────────────────────────────
class FakeWS:
    """The box's end of one worker socket. `replies` answer a job at once."""

    def __init__(self, replies=()):
        self.sent: list = []
        self._replies: asyncio.Queue = asyncio.Queue()
        for r in replies:
            self._replies.put_nowait(r)

    async def send_bytes(self, b):
        self.sent.append(bytes(b))

    async def receive_bytes(self):
        return await self._replies.get()

    async def receive(self):
        # serve()'s idle watch: an idle box says nothing, so only a cancel ends it.
        await asyncio.get_running_loop().create_future()


class Client:
    """The egress client's two streams, as the gate hands them to dispatch."""

    def __init__(self):
        self.closed = False
        self.wrote = b""

    async def read(self, _n):
        return b""

    def write(self, b):
        self.wrote += b

    async def drain(self):
        pass

    def close(self):
        self.closed = True


# ── the five kinds of job, each with its real waiter ────────────────────────
class Egress:
    name, first, replies = "egress", b"D", (b"E",)

    def relay(self):
        return EG.EgressRelay()

    def worker(self, ws):
        return EG.RelayWorker(ws, bastion_token_id=TOKEN)

    async def wait(self, relay, ctx, timeout=WAIT):
        ctx["client"] = c = Client()
        return await relay.dispatch(b"CONNECT example.org:443 HTTP/1.1\r\n\r\n", c, c,
                                    timeout=timeout, bastion_token_id=TOKEN)

    def failed(self, got, ctx):
        # dispatch says no worker relayed it, and the client's stream is still
        # the gate's to answer (a 503 for a pinned rule, the platform path else).
        c = ctx.get("client")
        return got is False and c is not None and not c.closed and not c.wrote

    def ran(self, got, ctx):
        return got is True

    # A job the box already has: dispatch says it was relayed, at once, so the
    # gate sends nothing of it the platform way.
    on_budget = False

    def kept(self, got, ctx):
        return got is True


class Embed:
    name, first = "embed", b"C"
    replies = (b"V" + json.dumps({"vector": [0.25, 0.5]}).encode(),)

    def relay(self):
        return EM.EmbedRelay()

    def worker(self, ws):
        return EM.EmbedWorker(ws, TOKEN)

    async def wait(self, relay, ctx, timeout=WAIT):
        return await relay.dispatch(TOKEN, "a-model", "some text", timeout=timeout)

    def failed(self, got, ctx):
        return got is None

    def ran(self, got, ctx):
        return got == [0.25, 0.5]

    # A job the box already has: no vector comes back, and dispatch answers None
    # when its own timeout is out.
    on_budget = True

    def kept(self, got, ctx):
        return got is None


class HarnessRun:
    name, first, replies = "harness run", b"H", (b'J{"ok": true}', b"X0")

    def relay(self):
        return NH.HarnessRelay()

    def worker(self, ws):
        return NH.HarnessWorker(ws, TOKEN, lane="harness")

    async def wait(self, relay, ctx, timeout=WAIT):
        return await relay.dispatch(TOKEN, {"prompt": "go"}, timeout=timeout)

    def failed(self, got, ctx):
        # "it never started": run_harness_op answers 502 "...so it was not started"
        # and does not audit a run.
        return got is None

    def ran(self, got, ctx):
        return got == (b'{"ok": true}', b"", 0)

    # A run the box already has: "stopped waiting, it may still finish there"
    # (HarnessRunStopped), at the backstop, never "it was not started".
    on_budget = True

    def kept(self, got, ctx):
        return got == repr(NH.HarnessRunStopped("over", 0.0))


class Terminal:
    name, first, replies = "terminal session", b"S", (b"X0",)

    def relay(self):
        return NH.HarnessRelay()

    def worker(self, ws):
        return NH.HarnessWorker(ws, TOKEN, lane="session")

    async def wait(self, relay, ctx, timeout=WAIT):
        s = SL.BastionSession(server="box-a", op="run", session_id="t2954",
                              spec={"cols": 80, "rows": 24}, user="u")
        if not await relay.acquire(TOKEN, s.drive, timeout=timeout, lane="session"):
            return "no worker"
        code = await s.wait()          # the session's reader: no timeout of its own
        events = []
        while (ev := await s.recv()) is not None:
            events.append(ev)
        return code, events

    def failed(self, got, ctx):
        if not (isinstance(got, tuple) and got[1]):
            return False
        code, events = got
        return (code == SL.EXIT_BAD_SPEC and events[0][0] == "E"
                and "could not reach the machine" in events[0][1]
                and events[-1] == ("X", SL.EXIT_BAD_SPEC))

    def ran(self, got, ctx):
        return got == (0, [("X", 0)])

    # A session the box already has: the driver's own finally ends it at once,
    # with exit 1 and no "could not reach the machine", since it did.
    on_budget = False

    def kept(self, got, ctx):
        return got == (None, [("X", SL.EXIT_BAD_SPEC)])


class Mgmt:
    name, first, replies = "management op", b"C", (b"Oup\n", b"X0")

    def relay(self):
        return NM.MgmtRelay()

    def worker(self, ws):
        return NM.MgmtWorker(ws, TOKEN)

    async def wait(self, relay, ctx, timeout=WAIT):
        return await relay.dispatch_detailed(TOKEN, "status", [], timeout=timeout)

    def failed(self, got, ctx):
        # The op never reached the machine: "no worker" (502, and the ship runner
        # sends it again), never "no reply" (504, "it may still be running there").
        return got == (None, NM.DISPATCH_NO_WORKER)

    def ran(self, got, ctx):
        return got == ((b"up\n", b"", 0), "")

    # An op the box already has: "no reply" once the budget is out, so the ship
    # runner waits for its receipt. "No worker" here would send it again while
    # it still runs on the machine.
    on_budget = True

    def kept(self, got, ctx):
        return got == (None, NM.DISPATCH_NO_REPLY)


# ── scenarios ────────────────────────────────────────────────────────────────
async def _answer(waiter):
    await asyncio.wait({waiter}, timeout=SOON)
    if not waiter.done():
        return PENDING
    if waiter.cancelled():
        return "<cancelled>"
    if waiter.exception() is not None:
        return repr(waiter.exception())
    return waiter.result()


async def _settle(*tasks):
    for t in tasks:
        if not t.done():
            t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def in_window(kind, stop_first: bool):
    relay, ws = kind.relay(), FakeWS(kind.replies)
    task = asyncio.create_task(kind.worker(ws).serve(relay))
    seen: dict = {}

    def stop() -> None:
        seen["awaits_child"] = getattr(task, "_fut_waiter", None) is ARM.get("child")
        seen["child_done"] = ARM["child"].done()
        task.cancel()                   # the Stop

    ARM.clear()
    ARM.update(owner=task, stop=stop, stop_first=stop_first)
    ctx: dict = {}
    waiter = asyncio.create_task(kind.wait(relay, ctx))
    got = await _answer(waiter)
    await asyncio.wait({task}, timeout=SOON)
    stopped = task.done()
    ARM.clear()
    await _settle(task, waiter)
    return got, ctx, ws, stopped, seen


async def no_stop(kind):
    relay, ws = kind.relay(), FakeWS(kind.replies)
    task = asyncio.create_task(kind.worker(ws).serve(relay))
    ctx: dict = {}
    waiter = asyncio.create_task(kind.wait(relay, ctx))
    got = await _answer(waiter)
    await asyncio.wait({task}, timeout=SOON)
    ended = task.done() and not task.cancelled()
    await _settle(task, waiter)
    return got, ctx, ws, ended


async def idle_stop(kind):
    relay = kind.relay()
    ws_a, ws_b = FakeWS(kind.replies), FakeWS(kind.replies)
    a = kind.worker(ws_a)
    ta = asyncio.create_task(a.serve(relay))
    await asyncio.sleep(0.01)           # A is registered and waits idle
    ta.cancel()                         # the Stop
    await asyncio.wait({ta}, timeout=SOON)
    relay.discard(a)                    # what the endpoint does once serve() returns (server/main.py)
    tb = asyncio.create_task(kind.worker(ws_b).serve(relay))
    ctx: dict = {}
    waiter = asyncio.create_task(kind.wait(relay, ctx))
    got = await _answer(waiter)
    stopped = ta.done()
    await _settle(ta, tb, waiter)
    return got, ctx, ws_a, ws_b, stopped


async def handed_then_stop(kind):
    relay, ws = kind.relay(), FakeWS(kind.replies)
    w = kind.worker(ws)
    task = asyncio.create_task(w.serve(relay))
    handed = w.assign

    def assign_then_stop(job):
        ok = handed(job)
        task.cancel()                   # the Stop, before serve() has seen its job
        return ok

    w.assign = assign_then_stop
    ctx: dict = {}
    waiter = asyncio.create_task(kind.wait(relay, ctx))
    got = await _answer(waiter)
    await asyncio.wait({task}, timeout=SOON)
    stopped = task.done()
    await _settle(task, waiter)
    return got, ctx, ws, stopped


class SentThenStop(FakeWS):
    """The box takes the job and never answers it. The Stop lands just after the
    worker sent the job's first frame, at the worker's next await."""

    def __init__(self, on_first_send):
        super().__init__(())
        self._on_first_send = on_first_send

    async def send_bytes(self, b):
        await super().send_bytes(b)
        if len(self.sent) == 1:
            asyncio.get_running_loop().call_soon(self._on_first_send)


async def sent_then_stop(kind):
    relay, held = kind.relay(), {}
    ws = SentThenStop(lambda: held["task"].cancel())     # the Stop
    task = held["task"] = asyncio.create_task(kind.worker(ws).serve(relay))
    ctx: dict = {}
    waiter = asyncio.create_task(kind.wait(relay, ctx, timeout=BUDGET))
    await asyncio.wait({task}, timeout=SOON)
    stopped = task.done()
    await asyncio.sleep(0.05)           # a waiter the Stop answered has woken by now
    early = waiter.done()
    got = await _answer(waiter)
    await _settle(task, waiter)
    return got, early, ctx, ws, stopped


def _first(ws) -> bytes:
    return ws.sent[0][:1] if ws.sent else b""


async def main() -> None:
    asyncio.get_running_loop().set_task_factory(_factory)
    for kind in (Egress(), Embed(), HarnessRun(), Terminal(), Mgmt()):
        k = kind.name

        # ── 1. a Stop in the window ─────────────────────────────────────
        for stop_first, when in ((True, "before the receive's cancel has run"),
                                 (False, "after the receive's cancel has run")):
            got, ctx, ws, stopped, seen = await in_window(kind, stop_first)
            tag = f"{k} 1 ({when})"
            check(f"{tag}: the Stop landed while the worker awaited the receive it had just cancelled",
                  seen.get("awaits_child") is True and seen.get("child_done") is (not stop_first),
                  repr(seen))
            check(f"{tag}: the worker stops, and nothing of the job reaches the box",
                  stopped and not ws.sent, repr((stopped, ws.sent[:2])))
            check(f"{tag}: the job's waiter is answered at once, with a failure",
                  kind.failed(got, ctx), repr(got))

        # ── 2. control: the receive's own cancel, no Stop ────────────────
        got, ctx, ws, ended = await no_stop(kind)
        check(f"{k} 2: with no Stop the receive's own cancel does not stop the worker; the job runs",
              ended and _first(ws) == kind.first and kind.ran(got, ctx),
              repr((ended, ws.sent[:1], got)))

        # ── 3. a Stop while idle ────────────────────────────────────────
        got, ctx, ws_a, ws_b, stopped = await idle_stop(kind)
        check(f"{k} 3: after a Stop while idle, the next job goes to the next worker, not the stopped one",
              stopped and not ws_a.sent and _first(ws_b) == kind.first and kind.ran(got, ctx),
              repr((stopped, ws_a.sent[:1], ws_b.sent[:1], got)))

        # ── 4. a Stop just after the job was handed over ─────────────────
        got, ctx, ws, stopped = await handed_then_stop(kind)
        check(f"{k} 4: a Stop just after the job was handed over stops the worker before the box sees it",
              stopped and not ws.sent, repr((stopped, ws.sent[:1])))
        check(f"{k} 4: that job's waiter is answered at once, with a failure",
              kind.failed(got, ctx), repr(got))

        # ── 5. a Stop just after the job's first frame went to the box ───
        got, early, ctx, ws, stopped = await sent_then_stop(kind)
        check(f"{k} 5: a Stop just after the worker sent the job's first frame stops the worker",
              stopped and _first(ws) == kind.first, repr((stopped, ws.sent[:1])))
        check(f"{k} 5: the Stop does not answer a job the box has: its waiter "
              + ("waits out its own timeout" if kind.on_budget else "is answered at once, by the job's own path"),
              early is not kind.on_budget, repr((early, got)))
        check(f"{k} 5: that waiter's answer says the job reached the box, never that it was not started",
              kind.kept(got, ctx), repr(got))


if __name__ == "__main__":
    asyncio.run(main())
    if FAILS:
        print(f"[relay-keep-stop] {len(FAILS)} FAILED")
        sys.exit(1)
    print("[relay-keep-stop] all passed")
