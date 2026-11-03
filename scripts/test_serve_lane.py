#!/usr/bin/env python3
"""A Host reaches the model server on a bastion's machine over the bastion's
serve lane (server/network/serve_relay.py, the /bastion-serve route in
server/main.py, brain/mastercode/model_backends.py).

A standalone Host's pairings report no address of their own (reported_ip
empty, llm_port 0), so a bastion's models had no endpoint and chat could not
use them. The bastion parks worker WebSockets on /bastion-serve
(client-go/servelane.go); the Host names a service on one, the bastion answers
'A' and carries the bytes to the server on its own machine, or 'E' + why.

Pinned:
  1. list_local_models: a beat with no address gets a loopback origin for its
     models while that bastion's serve lane is connected, `llm_models` on the
     "llm" service and `models` on "ollama"; with the lane not connected the
     models are not listed; a beat with an address keeps http://<ip>:<port>.
  2. A chat completion through that origin, by the real chat client
     (provider_gateway._call_local), arrives at the stand-in server on the
     bastion's side with the client's own request, streamed back whole.
  3. The first frame is 'S' + {"service": "llm"} and nothing else: never an
     address, never the client's bytes before the bastion's 'A'.
  4. A connection goes only to the bastion whose origin was dialled, never to
     another machine's worker.
  5. A refusal ('E' + why) reaches the client as a 502 carrying the reason;
     no lane connected is a 503 at once, nothing dispatched.
  6. The route: /bastion-serve takes the bastion's own bearer and its operator
     certificate's binding, parks the worker (the lane counts as connected),
     and a wrong bearer or binding closes 4401 with nothing parked. The
     loopback gate lets the route through as a bastion lane.

Container-free: stub pool; a fake bastion WebSocket that splices to a real
stand-in HTTP server on loopback.
APP_ROOT=<repo> python3 scripts/test_serve_lane.py
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import types

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain")]

from server.network import serve_relay  # noqa: E402
from mastercode import model_backends  # noqa: E402

FAILS: list = []
CHECKS = 0


def check(cond, msg, extra=None):
    global CHECKS
    CHECKS += 1
    print(("  PASS  " if cond else "  FAIL  ") + msg + (("\n        " + str(extra)[:600]) if extra is not None and not cond else ""))
    if not cond:
        FAILS.append(msg)


# ── a stand-in OpenAI-compatible server on the "bastion's machine" ───────────

SEEN: list = []   # (method, path, body) the stand-in received


async def standin(reader, writer):
    try:
        while True:
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = await reader.read(4096)
                if not chunk:
                    return
                head += chunk
            hdr, _, rest = head.partition(b"\r\n\r\n")
            lines = hdr.decode().split("\r\n")
            method, path, _ = lines[0].split(" ", 2)
            n = 0
            for ln in lines[1:]:
                k, _, v = ln.partition(":")
                if k.strip().lower() == "content-length":
                    n = int(v.strip())
            while len(rest) < n:
                rest += await reader.read(n - len(rest))
            body = rest[:n]
            SEEN.append((method, path, body))
            if path == "/v1/chat/completions":
                req = json.loads(body)
                words = ["served ", "on the ", "bastion's ", "machine by ", req["model"]]
                chunks = b"".join(
                    b"data: " + json.dumps({"choices": [{"delta": {"content": w}, "finish_reason": None}]}).encode()
                    + b"\n\n" for w in words) + b"data: [DONE]\n\n"
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                             b"Transfer-Encoding: chunked\r\n\r\n")
                for c in (chunks[: len(chunks) // 2], chunks[len(chunks) // 2:]):
                    writer.write(b"%x\r\n" % len(c) + c + b"\r\n")
                    await writer.drain()
                writer.write(b"0\r\n\r\n")
            else:
                out = json.dumps({"object": "list", "data": [{"id": "stand-in"}]}).encode()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n"
                             % len(out) + out)
            await writer.drain()
    finally:
        writer.close()


# ── a bastion's parked serve worker, as the route hands it to the relay ──────

class FakeBastionWS:
    """The bastion end of one /bastion-serve worker (servelane.go serveJob):
    idle until given a job; on 'S' it answers 'A' and splices D/E frames to
    the stand-in at `upstream`, or 'E' + `refuse`."""

    def __init__(self, upstream, *, refuse: str | None = None, name="box"):
        self.upstream = upstream
        self.refuse = refuse
        self.name = name
        self.frames: list = []        # every frame the Host sent
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.gone = asyncio.Event()
        self.up_w = None
        self.headers = {"x-client-cert-cn": "operator"}
        self.client = types.SimpleNamespace(host="::1")
        self.closed: list = []
        self.hello = None

    async def accept(self):
        pass

    async def receive_text(self):
        return self.hello

    async def receive(self):          # parked: until closed
        await self.gone.wait()
        return {"type": "websocket.disconnect", "code": 1000}

    async def receive_bytes(self):
        f = await self.inbox.get()
        if f is None:
            raise RuntimeError("closed")
        return f

    async def send_bytes(self, b):
        self.frames.append(bytes(b))
        if len(self.frames) == 1:
            if self.refuse is not None:
                await self.inbox.put(b"E" + self.refuse.encode())
                return
            r, w = await asyncio.open_connection(*self.upstream)
            self.up_w = w

            async def pump():
                while True:
                    d = await r.read(65536)
                    if not d:
                        break
                    await self.inbox.put(b"D" + d)
                await self.inbox.put(b"E")
            await self.inbox.put(b"A")
            asyncio.ensure_future(pump())
            return
        if b[:1] == b"D" and self.up_w:
            self.up_w.write(b[1:])
            await self.up_w.drain()
        elif b[:1] == b"E" and self.up_w:
            self.up_w.write_eof()

    async def close(self, code=1000):
        self.closed.append(code)
        self.gone.set()
        await self.inbox.put(None)


async def park(relay, ws, token_id):
    w = serve_relay.ServeWorker(ws, bastion_token_id=token_id)
    t = asyncio.ensure_future(w.serve(relay))
    for _ in range(100):
        if relay.worker_count(token_id):
            break
        await asyncio.sleep(0.01)
    return t


class Pool:
    def __init__(self, rows):
        self.rows = rows

    async def fetch(self, q, *a):
        return self.rows


def beat(token_id, *, ip=None, models=(), llm=(), llm_port=None, host=None):
    return {"token_id": token_id, "hostname": host or f"box{token_id}", "reported_ip": ip,
            "models": json.dumps(list(models)), "llm_models": json.dumps(list(llm)), "llm_port": llm_port}


async def main():
    srv = await asyncio.start_server(standin, "127.0.0.1", 0)
    upstream = ("127.0.0.1", srv.sockets[0].getsockname()[1])
    # No owner-named servers in this test.
    from mastercode import owner_endpoints

    async def _none(pool, eid):
        return []
    owner_endpoints.models = _none

    serve_relay._relay = None
    serve_relay._gateway = None
    relay = serve_relay.get_serve_relay()

    # ── 1. list_local_models ─────────────────────────────────────────────────
    print("1 a beat with no address is reached over its serve lane")
    pool = Pool([beat(7, llm=["qwen-local"], models=["llama3"]),
                 beat(8, ip="10.0.0.8", llm=["far"], llm_port=18080)])
    got = await model_backends.list_local_models(pool, 1)
    check([m["id"] for m in got] == ["far"],
          "lane not connected: the address-less machine's models are not listed", got)
    ws7 = FakeBastionWS(upstream)
    t7 = await park(relay, ws7, 7)
    check(serve_relay.connected(7) and not serve_relay.connected(8), "the parked worker connects bastion 7 only")
    got = await model_backends.list_local_models(pool, 1)
    by = {m["id"]: m for m in got}
    check(set(by) == {"llama3", "qwen-local", "far"}, "lane connected: its models are listed", got)
    q = by.get("qwen-local", {})
    o = by.get("llama3", {})
    check(q.get("endpoint", "").startswith("http://127.0.0.1:") and q.get("kind") == "llm-openai",
          "llm_models get a loopback origin, kind llm-openai", q)
    check(o.get("endpoint", "").startswith("http://127.0.0.1:") and o.get("kind") == "ollama"
          and o.get("endpoint") != q.get("endpoint"),
          "models (ollama) get an origin of their own, not the llm one", (o, q))
    check(by.get("far", {}).get("endpoint") == "http://10.0.0.8:18080",
          "a beat with an address keeps http://<ip>:<llm_port>", by.get("far"))
    again = {m["id"]: m["endpoint"] for m in await model_backends.list_local_models(pool, 1)}
    check(again.get("qwen-local") == q.get("endpoint"), "the origin is stable across reads", again)

    # ── 2 + 3. a chat completion by the real chat client ────────────────────
    print("2 a chat completion through the lane, by the real chat client")
    from mastercode import provider_gateway as pg
    res = await pg._call_local([{"role": "user", "content": "hello"}], "qwen-local", base_url=q["endpoint"])
    check(res.get("content", "").strip() == "served on the bastion's machine by qwen-local",
          "the answer is the stand-in's, whole", res)
    chat = [s for s in SEEN if s[1] == "/v1/chat/completions"]
    check(len(chat) == 1 and json.loads(chat[0][2]).get("model") == "qwen-local"
          and json.loads(chat[0][2]).get("messages")[-1]["content"] == "hello",
          "the stand-in received the client's own request", SEEN)
    print("3 the first frame names the service and nothing else")
    first = ws7.frames[0] if ws7.frames else b""
    check(first[:1] == b"S" and json.loads(first[1:]) == {"service": "llm"},
          "first frame is 'S' + {\"service\": \"llm\"}", first)
    check(all(f[:1] in (b"D", b"E") for f in ws7.frames[1:]), "after 'A' only D and E frames", ws7.frames[:3])
    check(not any(b"10.0.0" in f or b"127.0.0.1" in f for f in ws7.frames[:1]),
          "no address in the service frame", first)
    await asyncio.wait_for(t7, 5)

    # ollama service for `models`
    ws7b = FakeBastionWS(upstream)
    t7b = await park(relay, ws7b, 7)
    res = await pg._call_local([{"role": "user", "content": "hi"}], "llama3", base_url=o["endpoint"])
    check(ws7b.frames and json.loads(ws7b.frames[0][1:]) == {"service": "ollama"}
          and "served on" in res.get("content", ""),
          "a `models` model asks the lane for ollama", (ws7b.frames[:1], res))
    await asyncio.wait_for(t7b, 5)

    # ── 4. only that bastion's worker ───────────────────────────────────────
    print("4 a connection goes only to the bastion whose origin was dialled")
    ws9 = FakeBastionWS(upstream, name="other")
    t9 = await park(relay, ws9, 9)
    pool2 = Pool([beat(7, llm=["qwen-local"]), beat(9, llm=["other-model"])])
    # bastion 7 has no worker parked now; 9 has one. 7's origin must not take 9's.
    serve_relay._relay._inflight[7] = 1   # 7 counts as connected (busy) so dispatch waits for ITS worker
    serve_relay.DISPATCH_TIMEOUT_S = 0.5
    ep7 = await serve_relay.get_serve_gateway().endpoint(7, "llm")
    res = await pg._call_local([{"role": "user", "content": "x"}], "qwen-local", base_url=ep7)
    check(not ws9.frames, "bastion 9's worker was never handed bastion 7's connection", ws9.frames)
    check("503" in str(res.get("error", "")), "bastion 7 busy past the wait: 503", res)
    serve_relay._relay._inflight.pop(7, None)
    serve_relay.DISPATCH_TIMEOUT_S = 20.0
    got = {m["id"]: m for m in await model_backends.list_local_models(pool2, 1)}
    check(set(got) == {"other-model"}, "only the connected bastion's models are listed", got)
    await ws9.close()
    await asyncio.wait_for(t9, 5)

    # ── 5. refusal and not connected ────────────────────────────────────────
    print("5 a refusal carries its reason; no lane is a 503 at once")
    why = "refused: no llm server is set on this machine (PRIVATAE_LLM_ADDR)"
    wsr = FakeBastionWS(upstream, refuse=why)
    tr = await park(relay, wsr, 7)
    import aiohttp
    async with aiohttp.ClientSession() as s:
        async with s.post(ep7 + "/v1/chat/completions", json={"model": "m", "messages": []}) as r:
            body = await r.json()
            check(r.status == 502 and why in body["error"]["message"],
                  "the bastion's refusal: 502 with its words", (r.status, body))
            check(len(wsr.frames) == 1, "nothing of the client's was sent after the refusal", wsr.frames)
        await asyncio.wait_for(tr, 5)
        n_before = len(SEEN)
        loop = asyncio.get_event_loop()
        t0 = loop.time()
        async with s.post(ep7 + "/v1/chat/completions", json={"model": "m", "messages": []}) as r:
            body = await r.json()
            check(r.status == 503 and "not connected" in body["error"]["message"]
                  and loop.time() - t0 < 2 and len(SEEN) == n_before,
                  "no worker parked: 503 at once, nothing reached any server", (r.status, body))

    # ── 5b. a gone machine's listeners close ────────────────────────────────
    print("5b a machine whose lane has gone has its listeners closed after the grace")
    import time as _t
    gw = serve_relay.get_serve_gateway()
    port7 = int(ep7.rsplit(":", 1)[1])
    keys7 = [k for k in gw._servers if k[0] == 7]
    check(keys7 and not serve_relay.connected(7), "bastion 7: listeners held, lane not connected", keys7)
    kept = gw.sweep(_t.monotonic() + 1)
    check(kept == [] and all(k in gw._servers for k in keys7),
          "within the grace (a moment between two jobs): kept", kept)
    closed = gw.sweep(_t.monotonic() + serve_relay.LISTENER_GRACE_S + 1)
    check(sorted(closed) == sorted(keys7) and not any(k[0] == 7 for k in gw._servers),
          "past the grace: every listener of bastion 7 closed", closed)
    try:
        r_, w_ = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port7), 3)
        w_.close()
        refused = False
    except OSError:
        refused = True
    check(refused, "...and its port no longer takes connections")

    # ── 6. the route ────────────────────────────────────────────────────────
    print("6 the /bastion-serve route")
    import starlette.staticfiles as _sf
    orig = _sf.StaticFiles.__init__

    def _init(self, *a, **k):
        k["check_dir"] = False
        return orig(self, *a, **k)
    _sf.StaticFiles.__init__ = _init
    try:
        import server.main as m
    finally:
        _sf.StaticFiles.__init__ = orig
    from server import brain_service
    from server.network import quarantine_gate as QG
    BEARER = "mcp_" + "s" * 40

    class RoutePool:
        async def fetchrow(self, q, *a):
            if "FROM mcp_client_tokens" in q and a and a[0] == hashlib.sha256(BEARER.encode()).digest():
                return {"id": 31, "op_uuid": None, "hardware_fingerprint": "f" * 64}
            return None

        async def fetchval(self, q, *a):
            return None

        async def fetch(self, q, *a):
            return []
    brain_service.get_pool = lambda: RoutePool()

    async def _not_held(pool, tid):
        return False
    QG.is_quarantined = _not_held
    good = FakeBastionWS(upstream)
    good.hello = "BEARER " + BEARER
    rt = asyncio.ensure_future(m.bastion_serve_relay(good))
    for _ in range(200):
        if serve_relay.connected(31):
            break
        await asyncio.sleep(0.01)
    check(serve_relay.connected(31), "the bastion's own bearer parks the worker: lane 31 connected")
    await good.close()
    await asyncio.wait_for(rt, 5)
    check(not serve_relay.connected(31), "closed: no longer connected")
    bad = FakeBastionWS(upstream)
    bad.hello = "BEARER mcp_" + "x" * 40
    await asyncio.wait_for(m.bastion_serve_relay(bad), 5)
    check(4401 in bad.closed and not serve_relay.connected(31), "a wrong bearer: 4401, nothing parked", bad.closed)
    unbound = FakeBastionWS(upstream)
    unbound.hello = "BEARER " + BEARER
    unbound.headers = {"x-client-cert-cn": "router"}
    await asyncio.wait_for(m.bastion_serve_relay(unbound), 5)
    check(4401 in unbound.closed and not serve_relay.connected(31),
          "the bearer on a certificate that is not the operator's: 4401", unbound.closed)
    from server.middleware import localhost_only
    check("/bastion-serve" in localhost_only._BASTION_WS_PATHS,
          "the loopback gate lets /bastion-serve through as a bastion lane")

    srv.close()
    await serve_relay.get_serve_gateway().close()


asyncio.run(main())
print(f"\n{CHECKS - len(FAILS)}/{CHECKS} passed")
sys.exit(1 if FAILS else 0)
