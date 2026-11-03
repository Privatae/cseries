#!/usr/bin/env python3
"""A Host reaches every model server on a bastion's machine over the bastion's
serve lane: embeddings and the PII sterilizer, as chat already does
(test_serve_lane.py).

A standalone Host's pairings report no address of their own (reported_ip
empty, no pii_port), so every caller that built http://<reported_ip>:<port>
had nothing to dial, and a fallback to an address on the machine is shut by
the VM's machine wall, as it must be. The serve lane is the one door: the Host
names a service ("ollama", "llm", "pii"), the bastion dials the server set on
its own machine.

Pinned, for each caller:
  1. embed_backends.list_embed_bastions: an address-less bastion's embedder
     gets a serve-lane origin while its lane is connected ("ollama" for the
     `embed` capability's models, "llm" for an embedding model on its
     OpenAI-compatible server); not connected, it is not listed; a bastion
     with an address keeps http://<ip>:11434.
  2. embed_backends.resolve_endpoint_for_hostname (the embed reader's live
     re-resolve): the same, by the host's embedding model; not connected is
     None, never an address.
  3. memory_engine._byo_embedding_config: a lane origin is dialled directly
     (no peer_forward); a mesh bastion still rides the Hub's peer forward.
     A real embed by memory_engine._embed_via_byo reaches the stand-in server
     on the bastion's side with the first frame 'S' + {"service": ...} only.
  4. The switch-time probe (routes/embedding.apply_embedding_switch): a lane
     origin is probed directly, a mesh bastion through the peer forward.
  5. media_engine.media_config_state: the same rule for picture embeddings.
  6. embed_autowire.autowire_decision: an address-less pairing whose
     OpenAI-compatible server serves an embedding model is wired; a mesh
     bastion with no `embed` capability still is not.
  7. pii_client.resolve_endpoint: an address-less beat with pii_ok gets the
     "pii" lane origin; a sterilize call reaches the stand-in through it; a
     mesh beat keeps http://<ip>:<pii_port>; not connected is None.
  8. No route from the Host to the machine: with every lane down, no caller
     returns any address at all, and every origin any caller returned for an
     address-less bastion is this Host's own loopback listener.

Container-free: stub pools; a fake bastion WebSocket that splices to a real
stand-in HTTP server on loopback.
APP_ROOT=<repo> python3 scripts/test_lane_embed_pii.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import types

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain")]
logging.disable(logging.CRITICAL)

from server.network import serve_relay  # noqa: E402
from mastercode import embed_backends as EB  # noqa: E402
from mastercode import memory_engine as ME  # noqa: E402
from mastercode import pii_client  # noqa: E402

FAILS: list = []
CHECKS = 0


def check(cond, msg, extra=None):
    global CHECKS
    CHECKS += 1
    print(("  PASS  " if cond else "  FAIL  ") + msg + (("\n        " + str(extra)[:600]) if extra is not None and not cond else ""))
    if not cond:
        FAILS.append(msg)


SEEN: list = []   # (method, path, body)
DIM = 768


async def standin(reader, writer):
    """The servers on the bastion's machine: /v1/embeddings, /sterilize,
    /health, as one HTTP/1.1 stand-in."""
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
            if path == "/v1/embeddings":
                out = {"data": [{"embedding": [0.5] * DIM}], "model": json.loads(body)["model"]}
            elif path == "/sterilize":
                out = {"text": "[NAME_1] lives here", "findings": [{"type": "NAME"}], "stats": {}}
            else:
                out = {"ok": True}
            raw = json.dumps(out).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n"
                         % len(raw) + raw)
            await writer.drain()
    finally:
        writer.close()


class FakeBastionWS:
    """One parked /bastion-serve worker (servelane.go serveJob): on 'S' it
    answers 'A' and splices D/E frames to the stand-in."""

    def __init__(self, upstream):
        self.upstream = upstream
        self.frames: list = []
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.gone = asyncio.Event()
        self.up_w = None
        self.headers = {"x-client-cert-cn": "operator"}
        self.client = types.SimpleNamespace(host="::1")

    async def accept(self):
        pass

    async def receive(self):
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
        self.gone.set()
        await self.inbox.put(None)


async def park(relay, upstream, token_id):
    ws = FakeBastionWS(upstream)
    w = serve_relay.ServeWorker(ws, bastion_token_id=token_id)

    async def _serve():   # as the /bastion-serve route runs it (server/main.py)
        try:
            await w.serve(relay)
        finally:
            relay.discard(w)
    t = asyncio.ensure_future(_serve())
    for _ in range(100):
        if relay.worker_count(token_id):
            break
        await asyncio.sleep(0.01)
    return ws, t


def bastion(token_id, *, ip=None, caps=(), models=(), llm=(), host=None):
    """A topology.list_bastions row."""
    return {"id": token_id, "hostname": host or f"box{token_id}", "name": f"box{token_id}",
            "handle": None, "online": True, "reported_ip": ip, "capabilities": list(caps),
            "models": list(models), "llm_models": list(llm), "last_seen_at": None}


class HBPool:
    """resolve_endpoint_for_hostname's heartbeat read, by hostname."""

    def __init__(self, beats):
        self.beats = beats

    async def fetchrow(self, q, *a):
        if "network_bastion_heartbeat" in q:
            b = self.beats.get(a[0])
            if not b:
                return None
            return {"token_id": b["id"], "reported_ip": b["reported_ip"],
                    "capabilities": json.dumps(b["capabilities"]), "models": json.dumps(b["models"]),
                    "llm_models": json.dumps(b["llm_models"])}
        return None


class PiiPool:
    def __init__(self, rows):
        self.rows = rows

    async def fetch(self, q, *a):
        assert "pii_ok IS NOT NULL" in q, q
        return self.rows


async def main():
    srv = await asyncio.start_server(standin, "127.0.0.1", 0)
    upstream = ("127.0.0.1", srv.sockets[0].getsockname()[1])
    serve_relay._relay = None
    serve_relay._gateway = None
    relay = serve_relay.get_serve_relay()
    returned: list = []   # every endpoint a caller returned for an address-less bastion

    from server.network import topology
    LOCAL_OLLAMA = bastion(7, caps=["embed"], models=["nomic-embed-text", "llama3"])
    LOCAL_LLM = bastion(8, caps=["llm-openai"], llm=["nomic-embed-text"])
    MESH = bastion(9, ip="10.0.0.9", caps=["embed"], models=["bge-m3"])
    MESH_LLM = bastion(10, ip="10.0.0.10", caps=["llm-openai"], llm=["nomic-embed-text"])
    rows = [LOCAL_OLLAMA, LOCAL_LLM, MESH, MESH_LLM]

    async def _list(pool, eid):
        return [dict(r) for r in rows]
    topology.list_bastions = _list

    # ── 1. list_embed_bastions ──────────────────────────────────────────────
    print("1 list_embed_bastions")
    got = {b["hostname"]: b for b in await EB.list_embed_bastions(None, 1)}
    check(set(got) == {"box9"}, "no lane connected: only the bastion with an address is listed", got)
    check(got.get("box9", {}).get("embed_endpoint") == "http://10.0.0.9:11434",
          "a bastion with an address keeps http://<ip>:11434", got.get("box9"))
    ws7, t7 = await park(relay, upstream, 7)
    ws8, t8 = await park(relay, upstream, 8)
    got = {b["hostname"]: b for b in await EB.list_embed_bastions(None, 1)}
    check(set(got) == {"box7", "box8", "box9"},
          "lanes connected: the address-less embedders are listed; a mesh llm server is not", got)
    e7 = got.get("box7", {}).get("embed_endpoint", "")
    e8 = got.get("box8", {}).get("embed_endpoint", "")
    returned += [e7, e8]
    check(e7.startswith("http://127.0.0.1:") and serve_relay.is_lane_origin(e7),
          "ollama embedder: a serve-lane origin on this Host's loopback", e7)
    check(e8.startswith("http://127.0.0.1:") and serve_relay.is_lane_origin(e8) and e8 != e7,
          "OpenAI-compatible embedder: a lane origin of its own", (e7, e8))
    check("nomic-embed-text" in got.get("box8", {}).get("models", []),
          "the llm server's embedding model is offered for that machine", got.get("box8"))

    # ── 2. resolve_endpoint_for_hostname ────────────────────────────────────
    print("2 resolve_endpoint_for_hostname")
    hb = HBPool({b["hostname"]: b for b in rows})
    r7 = await EB.resolve_endpoint_for_hostname(hb, 1, "box7", "nomic-embed-text")
    r8 = await EB.resolve_endpoint_for_hostname(hb, 1, "box8", "nomic-embed-text")
    r9 = await EB.resolve_endpoint_for_hostname(hb, 1, "box9", "bge-m3")
    returned += [r7, r8]
    check(r7 == e7, "address-less, model on ollama: that lane's origin", (r7, e7))
    check(r8 == e8, "address-less, model on the llm server: the llm lane's origin", (r8, e8))
    check(r9 == "http://10.0.0.9:11434", "a bastion with an address: http://<ip>:11434", r9)
    check(await EB.resolve_endpoint_for_hostname(hb, 1, "box7", "not-served") is None,
          "a model the machine does not serve: None, no guessed service")

    # ── 3. the embed reader's config and a real embed ───────────────────────
    print("3 _byo_embedding_config and a real embed through the lane")

    class BPPool(HBPool):
        def __init__(self, beats, bp):
            super().__init__(beats)
            self.bp = bp

        async def fetchrow(self, q, *a):
            if "behavioral_params" in q:
                return {"behavioral_params": json.dumps(self.bp)}
            return await super().fetchrow(q, *a)
    beats = {b["hostname"]: b for b in rows}
    cfg8 = await ME._byo_embedding_config(BPPool(beats, {
        "embedding_model": "nomic-embed-text", "embedding_bastion": "box8",
        "embedding_base_url": "http://127.0.0.1:1"}), 1)
    returned.append(cfg8.get("base_url"))
    check(cfg8.get("base_url") == e8 and "peer_forward" not in cfg8,
          "address-less bastion: the live lane origin, dialled directly (no peer forward)", cfg8)
    cfg9 = await ME._byo_embedding_config(BPPool(beats, {
        "embedding_model": "bge-m3", "embedding_bastion": "box9",
        "embedding_base_url": "http://10.0.0.9:11434"}), 1)
    check(cfg9.get("base_url") == "http://10.0.0.9:11434" and cfg9.get("peer_forward"),
          "mesh bastion: its address, through the Hub's peer forward", cfg9)
    n0 = len(SEEN)
    vec = await ME._embed_via_byo("hello", "nomic-embed-text", cfg8)
    check(isinstance(vec, list) and len(vec) == DIM, "the embed came back from the stand-in", vec and len(vec))
    check(any(s[1] == "/v1/embeddings" and json.loads(s[2])["model"] == "nomic-embed-text" for s in SEEN[n0:]),
          "the stand-in received the client's own /v1/embeddings request", SEEN[n0:])
    first = ws8.frames[0] if ws8.frames else b""
    check(first[:1] == b"S" and json.loads(first[1:]) == {"service": "llm"},
          "first frame names the service \"llm\" and nothing else", first)
    await asyncio.wait_for(t8, 5)
    ws8, t8 = await park(relay, upstream, 8)
    ws7b = ws7
    vec = await ME._embed_via_byo("hi", "nomic-embed-text", {"base_url": e7})
    check(vec and ws7b.frames and json.loads(ws7b.frames[0][1:]) == {"service": "ollama"},
          "the ollama embedder's lane asks for \"ollama\"", ws7b.frames[:1])
    await asyncio.wait_for(t7, 5)
    ws7, t7 = await park(relay, upstream, 7)

    # ── 4. the switch-time probe ────────────────────────────────────────────
    print("4 the switch-time probe dials a lane origin directly")
    probes: list = []

    async def _probe(base_url, model, dim=None, **k):
        probes.append((base_url, k.get("peer_forward")))
        return False, "stub: stop after the probe"
    real_probe = ME.probe_embedding_endpoint
    ME.probe_embedding_endpoint = _probe
    try:
        from server.routes import embedding as E
        await E.apply_embedding_switch(object(), 1, "nomic-embed-text", provider="local",
                                       base_url=e8, dim=768, bastion="box8")
        await E.apply_embedding_switch(object(), 1, "bge-m3", provider="local",
                                       base_url="http://10.0.0.9:11434", dim=1024, bastion="box9")
    finally:
        ME.probe_embedding_endpoint = real_probe
    check(len(probes) == 2 and probes[0] == (e8, None), "lane origin: probed with no peer forward", probes)
    check(len(probes) == 2 and probes[1][1], "mesh bastion: probed through the peer forward", probes)

    # set_embed_bastion on a machine with an embedder on BOTH servers: the
    # switch targets the lane of the server holding the chosen model.
    BOTH = bastion(14, caps=["embed", "llm-openai"], models=["nomic-embed-text"], llm=["bge-m3"])
    rows.append(BOTH)
    ws14, t14 = await park(relay, upstream, 14)
    switched: list = []

    async def _switch(pool, eid, model, **k):
        switched.append((model, k.get("base_url")))
        return {"ok": True}
    real_switch = E.apply_embedding_switch
    E.apply_embedding_switch = _switch
    try:
        hb14 = HBPool({b["hostname"]: b for b in rows})
        o14 = await EB.resolve_endpoint_for_hostname(hb14, 1, "box14", "nomic-embed-text")
        l14 = await EB.resolve_endpoint_for_hostname(hb14, 1, "box14", "bge-m3")
        await EB.set_embed_bastion(hb14, 1, "box14", model="bge-m3")
        await EB.set_embed_bastion(hb14, 1, "box14", model="nomic-embed-text")
    finally:
        E.apply_embedding_switch = real_switch
        rows.remove(BOTH)
    returned += [o14, l14]
    check(o14 and l14 and o14 != l14 and switched == [("bge-m3", l14), ("nomic-embed-text", o14)],
          "set_embed_bastion: the model's own server's lane (llm for bge-m3, ollama for nomic)",
          (switched, o14, l14))
    await ws14.close()
    await asyncio.wait_for(t14, 5)

    # ── 5. picture embeddings ───────────────────────────────────────────────
    print("5 media_config_state")
    from mastercode import media_engine as MED
    from mastercode.memory_engine import _BP_UNREADABLE_KEY  # noqa: F401

    async def _bp_local(pool, eid):
        return {"media_embedding_model": "nomic-embed-text", "media_embedding_base_url": "http://127.0.0.1:1",
                "media_embedding_wire": "openai", "media_embedding_dim": next(iter(MED.MEDIA_COLUMN_BY_DIM)),
                "media_embedding_bastion": "box8"}
    real_load = ME._load_behavioral_params
    ME._load_behavioral_params = _bp_local
    try:
        mcfg, _ = await MED.media_config_state(hb, 1)
    finally:
        ME._load_behavioral_params = real_load
    returned.append(mcfg.get("base_url"))
    check(mcfg.get("base_url") == e8 and not mcfg.get("peer_forward"),
          "address-less bastion: the lane origin, no peer forward", mcfg)

    # ── 6. autowire ─────────────────────────────────────────────────────────
    print("6 embed_autowire.autowire_decision")
    from mastercode import embed_autowire as AW
    m, why = AW.autowire_decision(False, LOCAL_LLM, True)
    check(m == "nomic-embed-text", "address-less pairing, embedding model on its llm server: wired", (m, why))
    m, why = AW.autowire_decision(False, MESH_LLM, True)
    check(m is None, "mesh bastion with no `embed` capability: not wired (unchanged)", (m, why))

    # ── 7. PII ──────────────────────────────────────────────────────────────
    print("7 pii_client.resolve_endpoint and a sterilize through the lane")
    os.environ.pop("PII_SERVICE_URL", None)
    os.environ.pop("MESH_OVERLAY_CIDR", None)
    os.environ["PII_API_KEY"] = "k" * 32
    ep = await pii_client.resolve_endpoint(PiiPool([{"token_id": 11, "reported_ip": None, "pii_port": None}]), 1)
    check(ep is None, "address-less beat, lane not connected: None", ep)
    ws11, t11 = await park(relay, upstream, 11)
    ep = await pii_client.resolve_endpoint(PiiPool([{"token_id": 11, "reported_ip": None, "pii_port": None}]), 1)
    returned.append(ep)
    check(ep and serve_relay.is_lane_origin(ep), "address-less beat, lane connected: the pii lane origin", ep)
    mesh = await pii_client.resolve_endpoint(PiiPool([{"token_id": 12, "reported_ip": "10.0.0.12", "pii_port": 18081}]), 1)
    check(mesh == "http://10.0.0.12:18081", "mesh beat: http://<ip>:<pii_port> (unchanged)", mesh)
    both = await pii_client.resolve_endpoint(PiiPool([
        {"token_id": 13, "reported_ip": None, "pii_port": None},      # newest, lane down
        {"token_id": 11, "reported_ip": "", "pii_port": None}]), 1)
    check(both == ep, "the newest beat that resolves wins; a lane that is down is skipped", both)
    real_resolve = pii_client.resolve_endpoint

    async def _fixed(pool, eid):
        return ep
    pii_client.resolve_endpoint = _fixed
    try:
        n0 = len(SEEN)
        res = await pii_client.sterilize("Alice lives here", "s1")
    finally:
        pii_client.resolve_endpoint = real_resolve
    check(res.ok and res.text == "[NAME_1] lives here", "the sterilize answer is the stand-in's", res)
    check(any(s[1] == "/sterilize" for s in SEEN[n0:]), "the stand-in received /sterilize", SEEN[n0:])
    first = ws11.frames[0] if ws11.frames else b""
    check(first[:1] == b"S" and json.loads(first[1:]) == {"service": "pii"},
          "first frame names the service \"pii\" and nothing else", first)
    await asyncio.wait_for(t11, 5)

    # ── 8. no route from the Host to the machine ────────────────────────────
    print("8 every lane down: no caller returns an address")
    for ws, t in ((ws7, t7), (ws8, t8)):
        await ws.close()
        await asyncio.wait_for(t, 5)
    for _ in range(100):
        if not any(serve_relay.connected(t) for t in (7, 8, 11)):
            break
        await asyncio.sleep(0.01)
    check(not any(serve_relay.connected(t) for t in (7, 8, 11)), "every lane is down")
    got = {b["hostname"] for b in await EB.list_embed_bastions(None, 1)}
    check(got == {"box9"}, "list_embed_bastions: only the mesh bastion", got)
    down = [await EB.resolve_endpoint_for_hostname(hb, 1, h, "nomic-embed-text") for h in ("box7", "box8")]
    check(down == [None, None], "resolve_endpoint_for_hostname: None for both machines", down)
    cfg = await ME._byo_embedding_config(BPPool(beats, {
        "embedding_model": "nomic-embed-text", "embedding_bastion": "box8",
        "embedding_base_url": e8}), 1)
    check(cfg.get("backend_down") is True and "base_url" not in cfg,
          "the embed reader marks the lane down; the pinned origin is not used", cfg)

    async def _bp_pinned(pool, eid):
        return {"media_embedding_model": "nomic-embed-text", "media_embedding_base_url": e8,
                "media_embedding_wire": "openai", "media_embedding_dim": next(iter(MED.MEDIA_COLUMN_BY_DIM)),
                "media_embedding_bastion": "box8"}
    ME._load_behavioral_params = _bp_pinned
    try:
        mcfg, unread = await MED.media_config_state(hb, 1)
    finally:
        ME._load_behavioral_params = real_load
    check(mcfg and not unread and mcfg.get("backend_down") is True and not mcfg.get("base_url")
          and not mcfg.get("peer_forward"),
          "media_config_state: still configured, lane down, the pinned lane origin is not used "
          "(not dialled, not sent through the peer forward)", mcfg)
    pe =await pii_client.resolve_endpoint(PiiPool([{"token_id": 11, "reported_ip": None, "pii_port": None}]), 1)
    check(pe is None, "pii_client: None", pe)
    check(all(isinstance(e, str) and e.startswith("http://127.0.0.1:") for e in returned),
          "every endpoint returned for an address-less bastion was this Host's own loopback listener", returned)

    srv.close()
    await serve_relay.get_serve_gateway().close()


asyncio.run(main())
print(f"\n{CHECKS - len(FAILS)}/{CHECKS} passed")
sys.exit(1 if FAILS else 0)
