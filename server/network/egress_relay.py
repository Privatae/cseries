"""
NAC egress relay (P6, option-2 transport) — the REVERSE path.

The data path is asymmetric: the bastion can reach the host (it opens the MCP/heartbeat
connection to host:8443 over the tunnel), but the host CANNOT reach the bastion's tunnel IP
(the entity pod has no WireGuard interface; an egress SYN to a bastion tunnel IP black-holes).
So we flip the direction: the bastion opens a pool of WORKER WebSockets TO the host on the
existing :8443 lane and pulls egress jobs over them. The host's egress gate, instead of
dialing the bastion, hands each allowed request to an idle worker; the bastion dials out the
USER's own internet and relays the bytes back over that worker connection.

One job per worker WS, transparent byte relay (so the bastion can reuse the same
handleEgressConn it already has). Frames: b"D"+payload = data, b"E" = end. The host stays a
dumb pipe (client <-> worker WS); the bastion does the CONNECT/dial/200 exactly as the
direct proxy did — only the connection direction is reversed.

Only a bastion running the egress-capable client opens these workers, so the worker pool IS
the set of egress-able bastions — which also removes the bastion-selection race for the
common single-bastion case (old / non-egress bastions are simply never in the pool).
"""
import asyncio
import logging
from typing import Optional

# Egress gets its own stdout logger at INFO. uvicorn configures only its uvicorn.* loggers,
# so a plain logger.info() here propagates to a WARNING-level root and never prints — which
# made the worker handshake look like it was failing when it was only invisible. A dedicated
# handler (propagate=False, no dup) keeps the bastion-worker lifecycle observable.
log = logging.getLogger("cseries.egress")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s egress %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
    log.propagate = False


class RelayWorker:
    """One bastion->host worker WebSocket = one egress job. The WS endpoint coroutine owns
    ALL WS I/O; the gate just hands it (head + the client streams) and awaits completion."""

    def __init__(self, ws, bastion_token_id: Optional[int] = None):
        self.ws = ws
        self.bastion_token_id = bastion_token_id
        self._job: "asyncio.Future" = asyncio.get_event_loop().create_future()

    def assign(self, job) -> bool:
        """Gate -> worker: give this idle worker its one job. job = (head, c_reader,
        c_writer, done). Returns False if the worker was already taken/closed."""
        if self._job.done():
            return False
        self._job.set_result(job)
        return True

    async def serve(self, relay: "EgressRelay"):
        """Endpoint-owned: register idle, await one job, relay it, return (WS then closes).

        While idle we keep a receive() pending alongside the job future — an idle WS whose
        app never calls receive() gets torn down by the server, and the receive() also lets
        us notice the bastion disconnecting. Whichever fires first wins."""
        relay._register_idle(self)
        recv = asyncio.ensure_future(self.ws.receive())
        started = False
        try:
            await asyncio.wait({recv, self._job}, return_when=asyncio.FIRST_COMPLETED)
            if not self._job.done():
                # Idle bastion dropped (or sent a stray frame). CANCEL our job slot so a
                # dispatch that already pulled us off the idle queue gets a failed assign()
                # and moves to the next worker — otherwise it would hand a job to a worker
                # that already left serve(), and the gate's `await done` would hang forever.
                self._job.cancel()
                log.info("worker %s dropped while idle", self.bastion_token_id)
                return
            # Got a job: stop the idle receive BEFORE relaying, so _relay's receive_bytes
            # isn't racing a second concurrent receive on the same socket (that empties the
            # stream and the client gets nothing).
            recv.cancel()
            try:
                await recv
            except asyncio.CancelledError:
                # recv's own cancel, asked for just above. A Stop of this worker (a cancel
                # of its task) that lands while it waits for recv arrives as the same
                # exception, because Task.cancel hands it to the task being awaited; only
                # this task's count of pending cancels tells the two apart. It goes on to
                # the handler below. Dropped, the worker relayed the job anyway.
                if asyncio.current_task().cancelling():
                    raise
            except Exception:
                pass
            job = self._job.result()
            if job is None:
                return
            head, c_reader, c_writer, done = job
            started = True
            try:
                await self._relay(head, c_reader, c_writer)
            except Exception as e:
                log.info("worker %s relay error: %r", self.bastion_token_id, e)
            finally:
                if not done.done():
                    done.set_result(True)
        except asyncio.CancelledError:
            # A Stop. Leave no gate waiting on this worker, since dispatch awaits `done`
            # with no timeout. No job yet: close the slot the way a drop while
            # idle does, above. A job not yet started: nothing of it reached the bastion,
            # so answer False and dispatch reports that no worker relayed it.
            if not self._job.done():
                self._job.cancel()
            elif not started and not self._job.cancelled():
                job = self._job.result()
                if job is not None and not job[3].done():
                    log.info("worker %s stopped before relaying its job", self.bastion_token_id)
                    job[3].set_result(False)
            return
        finally:
            if not recv.done():
                recv.cancel()

    async def _relay(self, head: bytes, c_reader, c_writer):
        # The host already read `head` (to policy-check the destination); hand it to the
        # bastion first, then pipe the rest of the client through. The bastion's
        # handleEgressConn parses CONNECT/HTTP, dials out, and writes the 200 / response back.
        await self.ws.send_bytes(b"D" + head)

        async def c_to_ws():
            try:
                while True:
                    data = await c_reader.read(65536)
                    if not data:
                        break
                    await self.ws.send_bytes(b"D" + data)
            except Exception:
                pass
            try:
                await self.ws.send_bytes(b"E")
            except Exception:
                pass

        async def ws_to_c():
            try:
                while True:
                    frame = await self.ws.receive_bytes()
                    if not frame:
                        continue
                    if frame[:1] == b"D":
                        c_writer.write(frame[1:])
                        await c_writer.drain()
                    elif frame[:1] == b"E":
                        break
            except Exception:
                pass
            try:
                c_writer.close()
            except Exception:
                pass

        await asyncio.gather(c_to_ws(), ws_to_c())


class EgressRelay:
    """Pool of idle bastion worker WebSockets + the gate's dispatch onto them, KEYED BY
    bastion token_id.

    v127: an egress rule can PIN a subject to a named bastion ("my browser goes out my
    home machine"), so the gate must reach one specific bastion rather than whichever
    worker is next. Same structure as EmbedRelay (server/network/embed_relay.py), which
    has always needed a specific box.

    There is deliberately NO shared/undifferentiated queue beside these. A worker is
    enqueued exactly once (_register_idle) so it can live in one queue only: keep a
    shared queue as well and either the pinned path can never find its worker, or two
    dispatchers pop the same one. Unpinned dispatch races across ALL the per-bastion
    queues instead (see _take_any)."""

    def __init__(self):
        self._idle: dict = {}     # token_id -> asyncio.Queue[RelayWorker]
        self._workers: dict = {}  # token_id -> set[RelayWorker]
        # token_id -> jobs currently in flight on that bastion. Needed because a
        # worker is REMOVED from _workers the moment it is taken, so "idle count
        # is 0" cannot tell a busy bastion from an absent one — and the pinned
        # path has to, or an offline machine costs the user a 20s stall on every
        # request instead of an immediate, accurate 503.
        self._inflight: dict = {}

    def _q(self, token_id) -> "asyncio.Queue":
        if token_id not in self._idle:
            self._idle[token_id] = asyncio.Queue()
            self._workers[token_id] = set()
        return self._idle[token_id]

    def _register_idle(self, w: RelayWorker):
        self._q(w.bastion_token_id)
        self._workers[w.bastion_token_id].add(w)
        self._idle[w.bastion_token_id].put_nowait(w)

    def discard(self, w: RelayWorker):
        s = self._workers.get(w.bastion_token_id)
        if s:
            s.discard(w)

    def worker_count(self, token_id=None) -> int:
        """Idle workers for ONE bastion, or across all of them when token_id is None.

        IDLE ONLY — dispatch removes a worker the moment it takes it and serve()
        registers each worker exactly once, so a bastion whose only worker is mid-request
        counts 0. The gate depends on knowing that: it is why a pin distinguishes "no
        queue for that bastion at all" (never connected → fail fast) from "queue exists,
        count 0" (busy → wait)."""
        if token_id is not None:
            return len(self._workers.get(token_id, ()))
        return sum(len(s) for s in self._workers.values())

    def has_bastion(self, token_id) -> bool:
        """Is that bastion CONNECTED RIGHT NOW — idle or busy?

        Not "has a queue": _idle keys are created on first sight and never removed
        (and _q() creates one for any token asked about), so a key proves only that
        the bastion existed at some point in this process's life. A machine that
        connected this morning and is now off would pass that test and cost the
        user the full dispatch timeout on every pinned request before a 503 that
        says "did not answer" — when the true answer is "not connected", available
        immediately."""
        return bool(self._workers.get(token_id)) or bool(self._inflight.get(token_id))

    async def _take_any(self, remaining: float):
        """One idle worker from ANY bastion, or None on timeout.

        Races a get() against every per-bastion queue. Two things here are load-bearing:

        1. MORE THAN ONE getter can finish in the same tick. If several queues are
           non-empty their get()s all complete before this coroutine resumes, so `done`
           holds several workers. Cancelling an already-completed task does NOT put its
           item back — so every extra must be re-queued explicitly. Without that, one
           unpinned request drains a worker from every connected bastion; each orphan
           then sits in serve() awaiting a job that never arrives and is already out of
           _workers, so a later PINNED request sees worker_count(N) == 0 and 503s while
           that bastion's worker is idle and invisible.
        2. On timeout every pending getter must be cancelled, or a leaked getter steals a
           worker later for a request that has already fallen back. Cancelling a PENDING
           Queue.get() is safe — asyncio re-wakes the next getter and leaves the item.
        """
        queues = list(self._idle.values())
        if not queues:
            return None            # asyncio.wait(set()) raises ValueError
        getters = [asyncio.ensure_future(q.get()) for q in queues]
        try:
            done, pending = await asyncio.wait(
                getters, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
        except BaseException:
            for t in getters:
                t.cancel()
            raise
        for t in pending:
            t.cancel()
        taken = []
        for t in done:
            try:
                taken.append(t.result())
            except BaseException:
                pass
        if not taken:
            return None
        winner, extras = taken[0], taken[1:]
        for w in extras:
            self._q(w.bastion_token_id).put_nowait(w)   # back where it came from
        return winner

    async def dispatch(self, head: bytes, c_reader, c_writer, timeout: float,
                       bastion_token_id=None) -> bool:
        """Hand an allowed egress request to an idle bastion worker and relay until done.

        `bastion_token_id` None = any bastion (v059 behaviour, and what every unpinned
        request does); an int = that bastion ONLY — never another, and never the platform
        path. The caller (EgressProxy._handle) turns False into a 503 for a pinned rule
        instead of falling back, which is the whole point of pinning.

        Returns False if no worker came free within `timeout`, or if the worker that took
        the request was stopped before it relayed any of it. Skips workers that were
        already taken (their assign() fails)."""
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                log.info("dispatch: no live worker within %.0fs (bastion=%s)", timeout,
                         bastion_token_id if bastion_token_id is not None else "any")
                return False
            if bastion_token_id is not None:
                try:
                    worker = await asyncio.wait_for(
                        self._q(bastion_token_id).get(), remaining)
                except asyncio.TimeoutError:
                    return False
            else:
                worker = await self._take_any(remaining)
                if worker is None:
                    return False
            self.discard(worker)  # leaving the idle set; a worker serves exactly one job
            done = asyncio.get_event_loop().create_future()
            if not worker.assign((head, c_reader, c_writer, done)):
                continue  # stale worker (dropped while idle); try the next
            tid = worker.bastion_token_id
            self._inflight[tid] = self._inflight.get(tid, 0) + 1
            try:
                relayed = await done
            finally:
                # Must be a finally: a cancelled client (the user closed the tab
                # mid-request) would otherwise leave the bastion looking busy
                # forever, and has_bastion would keep answering "connected" for a
                # machine that has since gone.
                n = self._inflight.get(tid, 1) - 1
                if n > 0:
                    self._inflight[tid] = n
                else:
                    self._inflight.pop(tid, None)
            # False comes only from a worker stopped before it relayed anything
            # (RelayWorker.serve): the client's streams are untouched, so the caller
            # can still refuse or take another way out.
            return relayed is not False


_relay: Optional[EgressRelay] = None


def get_egress_relay() -> EgressRelay:
    global _relay
    if _relay is None:
        _relay = EgressRelay()
    return _relay
