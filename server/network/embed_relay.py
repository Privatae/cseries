"""
Embed reverse-relay lane — the host reaches a bastion's embedder over a worker
WebSocket the BASTION opens TO the host, instead of dialing the bastion's tunnel IP.

The embed data plane was the last host->bastion lane still using FORWARD-DIAL
(serveOllamaForward on the bastion's tunnel address, port 11434). The entity pod has
no WireGuard interface into the concentrator mesh, so that dial can never succeed. Every other
host->bastion lane (egress, mgmt, harness) already solved this with a reverse relay:
the bastion opens worker WebSockets TO the host (wss://<host>:8443/bastion-embed) and
the host dispatches jobs onto them. This lane moves embed onto that same model.

The embed job is request/response (one text in -> one vector out), so the mgmt lane
is the exact analog — this copies nac_mgmt's worker/relay TRANSPORT ONLY, swapping the
payload. UNLIKE mgmt there is NO signed-grant gate and NO audit: an embed is a
data-plane read the host already owns via its own embedding config, not an op ON the
user's box. Embeddings are never billed and never audited.

Frames (binary):
  pod -> bastion : C + json({"model": str, "input": str})
  bastion -> pod : V + json({"vector": [float, ...]})   # one vector, terminal
                   E + <reason>                          # error, terminal -> None

The relay is KEYED BY bastion token_id — embed must reach a SPECIFIC bastion (the
host's own), exactly like mgmt (unlike egress, where any worker will do). A separate
WS lane from the heartbeat and the other control lanes: a dead/slow embedder must
never stall control (the circuit-breaker lesson, embed_backends.py:18-22). (The
bastion's /mcp relay on port 7878 this line used to name is gone — 2026-09-10.)

The relay carries NO model of its own — the model is passed through from the host's
config (memory_engine._entity_embedding_config); the bastion serves whatever the host
names (the config-driven-model rule).
"""
import asyncio
import json
import logging
from typing import Optional

# Own visible logger (uvicorn leaves the app logger at WARNING root; mirror mgmt's handler).
log = logging.getLogger("cseries.embed_relay")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s embed-relay %(message)s"))
    log.addHandler(_h)


class EmbedWorker:
    """One bastion->host worker WebSocket = one embed job. The endpoint coroutine owns all
    WS I/O; dispatch hands it (model, text, result) and awaits the collected vector."""

    def __init__(self, ws, bastion_token_id: int):
        self.ws = ws
        self.bastion_token_id = bastion_token_id
        self._job: "asyncio.Future" = asyncio.get_event_loop().create_future()

    def assign(self, job) -> bool:
        if self._job.done():
            return False
        self._job.set_result(job)
        return True

    async def serve(self, relay: "EmbedRelay"):
        """Endpoint-owned: register idle, await one job, run it, return (the WS then closes).
        An idle worker also keeps a receive() pending so a bastion disconnect is noticed."""
        relay._register_idle(self)
        recv = asyncio.ensure_future(self.ws.receive())
        started = False
        try:
            await asyncio.wait({recv, self._job}, return_when=asyncio.FIRST_COMPLETED)
            if not self._job.done():
                # Idle bastion dropped: cancel our slot so a dispatch that already pulled us
                # off the idle queue gets a failed assign() and moves to the next worker.
                self._job.cancel()
                log.info("embed worker %s dropped while idle", self.bastion_token_id)
                return
            recv.cancel()
            try:
                await recv
            except asyncio.CancelledError:
                # recv's own cancel, asked for just above. A Stop of this worker that lands
                # while it waits for recv arrives as the same exception (Task.cancel hands
                # it to the task being awaited); only this task's count of pending cancels
                # tells the two apart. Dropped, the worker ran the job anyway.
                if asyncio.current_task().cancelling():
                    raise
            except Exception:
                pass
            job = self._job.result()
            if job is None:
                return
            model, text, result = job
            started = True
            try:
                await self._run(model, text, result)
            except Exception as e:
                log.info("embed worker %s run error: %r", self.bastion_token_id, e)
                if not result.done():
                    result.set_result(None)
        except asyncio.CancelledError:
            # A Stop. No job yet: close the slot the way a drop while idle does. A
            # job not yet started is answered None, as a failed run is, rather than left to
            # run out dispatch's timeout.
            if not self._job.done():
                self._job.cancel()
            elif not started and not self._job.cancelled():
                job = self._job.result()
                if job is not None and not job[2].done():
                    log.info("embed worker %s stopped before its job", self.bastion_token_id)
                    job[2].set_result(None)
            return
        finally:
            if not recv.done():
                recv.cancel()

    async def _run(self, model: str, text: str, result: "asyncio.Future"):
        await self.ws.send_bytes(b"C" + json.dumps({"model": model, "input": text}).encode())
        while True:
            frame = await self.ws.receive_bytes()
            if not frame:
                continue
            tag, payload = frame[:1], frame[1:]
            if tag == b"V":
                try:
                    vec = (json.loads(payload.decode() or "{}") or {}).get("vector")
                except Exception:
                    vec = None
                if not result.done():
                    result.set_result(list(vec) if vec else None)
                return
            if tag == b"E":
                log.info("embed worker %s error: %s",
                         self.bastion_token_id, payload.decode("utf-8", "replace"))
                if not result.done():
                    result.set_result(None)
                return


class EmbedRelay:
    """Pool of idle bastion worker WebSockets, KEYED BY bastion token_id — embed must reach a
    SPECIFIC host's bastion (unlike egress, where any worker will do)."""

    def __init__(self):
        self._idle: dict = {}     # token_id -> asyncio.Queue[EmbedWorker]
        self._workers: dict = {}  # token_id -> set[EmbedWorker]

    def _q(self, token_id: int) -> "asyncio.Queue":
        if token_id not in self._idle:
            self._idle[token_id] = asyncio.Queue()
            self._workers[token_id] = set()
        return self._idle[token_id]

    def _register_idle(self, w: EmbedWorker):
        self._q(w.bastion_token_id)
        self._workers[w.bastion_token_id].add(w)
        self._idle[w.bastion_token_id].put_nowait(w)

    def discard(self, w: EmbedWorker):
        s = self._workers.get(w.bastion_token_id)
        if s:
            s.discard(w)

    def worker_count(self, token_id: int) -> int:
        return len(self._workers.get(token_id, ()))

    async def dispatch(self, token_id: int, model: str, text: str,
                       timeout: float) -> Optional[list]:
        """Hand an embed job to an idle worker for THIS host's bastion; collect its vector.
        Returns the vector (list[float]) or None if no live worker / no result within `timeout`."""
        deadline = asyncio.get_event_loop().time() + timeout
        q = self._q(token_id)
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return None
            try:
                worker = await asyncio.wait_for(q.get(), remaining)
            except asyncio.TimeoutError:
                return None
            self._workers[token_id].discard(worker)  # leaving idle; serve() re-adds if reused
            result = asyncio.get_event_loop().create_future()
            if not worker.assign((model, text, result)):
                continue  # stale worker (dropped while idle); try the next
            try:
                return await asyncio.wait_for(result, max(0.1, deadline - asyncio.get_event_loop().time()))
            except asyncio.TimeoutError:
                return None


_relay: Optional[EmbedRelay] = None


def get_embed_relay() -> EmbedRelay:
    global _relay
    if _relay is None:
        _relay = EmbedRelay()
    return _relay


async def dispatch_embed(pool, hostname: str, model: str, text: str,
                         timeout: float = 60.0) -> Optional[list]:
    """Embed `text` on the bastion `hostname`'s embedder over the reverse relay — the SINGLE
    entry point the sentinel dial path (memory_engine._embed_via_byo) calls. Resolves
    hostname -> bastion token_id from network_bastion_heartbeat, then dispatches onto that
    bastion's worker. Returns the vector (list[float]) or None (no bastion registered, no
    live worker, or no vector within `timeout`). NO audit, NO signed-grant gate — embed is a
    data-plane read the host already owns."""
    hostname = (hostname or "").strip()
    if not hostname:
        return None
    token_id = await pool.fetchval(
        "SELECT token_id FROM network_bastion_heartbeat WHERE hostname=$1 "
        "ORDER BY last_seen_at DESC LIMIT 1", hostname)
    if token_id is None:
        log.info("embed dispatch: no bastion registered for '%s'", hostname)
        return None
    relay = get_embed_relay()
    if relay.worker_count(token_id) == 0:
        log.info("embed dispatch: no embed worker connected for '%s'", hostname)
        return None
    return await relay.dispatch(token_id, model, text, timeout)
