#!/usr/bin/env python3
"""
Embed bastion-identity lane — the invariant ratchet for the 2026-07-09/10
failure class. What broke: use-bastion persisted a RAW TUNNEL IP as
embedding_base_url; the bastion re-enrolled (new peer, new IP) and the host
kept embedding at the dead address forever — the UI said "serving your
embeddings" while nothing served anything.

The architecture this pins: the persisted truth is the bastion's IDENTITY
(`embedding_bastion` = hostname). The endpoint is DERIVED — re-resolved from
the bastion's latest heartbeat on every embed call. Gates:

  1. apply_embedding_switch(bastion=...) persists the identity.
  2. A pure-BYO switch (base_url, no bastion) CLEARS any stale identity.
  3. The reader resolves the identity to the LIVE heartbeat address — never
     the address persisted at switch time.
  4. A configured bastion with NO live address refuses the BYO lane loudly
     (returns {"backend_down": True, ...}) instead of silently dialing the
     stale pin — and the flag stops the caller from dropping to the bridge
     (the configured backend is the ONLY lane; no cross-lane fallback).
  5. set_embed_bastion threads the identity into the one write path.
  6. Bastion-lane embeds ride the Hub's host→peer forward (CONNECT tunnel) —
     the app container has no kernel route to the WG overlay (the wg-gw
     concentrator whose node routes used to carry this is retired 2026-07-10),
     so a "direct" dial is a guaranteed dead end. The reader config carries
     peer_forward, and _embed_via_byo actually tunnels through it.

Runs inside the image (all deps present): python3 /app/scripts/test_embed_identity_lane.py
"""

import asyncio
import json
import logging
import sys

logging.disable(logging.CRITICAL)

from server.routes import embedding as E            # noqa: E402
import mastercode.memory_engine as ME               # noqa: E402
import mastercode.embed_backends as EB              # noqa: E402


class FakePool:
    """Just enough asyncpg surface for the switch + reader paths."""

    def __init__(self, bp: dict):
        self.bp = dict(bp)
        self.hb = None  # heartbeat row for resolve_endpoint_for_hostname

    async def fetchrow(self, q, *a):
        if "network_bastion_heartbeat" in q:
            return self.hb
        if "behavioral_params" in q:
            return {"behavioral_params": json.dumps(self.bp)}
        return None

    async def fetchval(self, q, *a):
        return 0

    async def execute(self, q, *a):
        if "entity_registry" in q and a:
            self.bp = json.loads(a[0])


async def _probe_ok(*a, **k):
    return True, "ok"


def main() -> int:
    # The probe gate stays ON in prod; stub it here so no HTTP leaves the test.
    ME.probe_embedding_endpoint = _probe_ok

    # ── 1. bastion switch persists the IDENTITY ─────────────────────────
    pool = FakePool({
        "embedding_model": "old-model", "embedding_provider": "local",
        "embedding_base_url": "http://[2001:db8::a:b]:11434",  # the dead pin
    })
    r = asyncio.run(E.apply_embedding_switch(
        pool, 3, "qwen3-embedding:4b", provider="local",
        base_url="http://[2001:db8::a:d]:11434", dim=2560, bastion="DESKTOP-X"))
    assert not r.get("error"), f"switch errored: {r}"
    assert pool.bp.get("embedding_bastion") == "DESKTOP-X", pool.bp
    assert pool.bp.get("embedding_model") == "qwen3-embedding:4b", pool.bp

    # ── 2. pure-BYO switch clears the identity ──────────────────────────
    r = asyncio.run(E.apply_embedding_switch(
        pool, 3, "some-external-model", provider="local",
        base_url="http://byo.example.internal:9000", dim=1024))
    assert not r.get("error"), f"BYO switch errored: {r}"
    assert "embedding_bastion" not in pool.bp, pool.bp

    # ── 3. reader resolves the identity LIVE, ignoring the switch-time pin ─
    pool = FakePool({
        "embedding_model": "qwen3-embedding:4b", "embedding_provider": "local",
        "embedding_base_url": "http://[2001:db8::a:b]:11434",  # stale pin
        "embedding_bastion": "DESKTOP-X", "embedding_dim": 2560,
    })
    pool.hb = {"reported_ip": "2001:db8::a:d"}  # the bastion's LIVE address
    cfg = asyncio.run(ME._byo_embedding_config(pool, 3))
    assert cfg.get("base_url") == "http://[2001:db8::a:d]:11434", \
        f"reader must resolve live, got {cfg}"

    # ── 4. no live address → refuse the lane, never dial the stale pin —
    #      AND flag "configured but down" so the caller can't drop to the
    #      bridge either (config-driven lanes take no cross-lane fallback;
    #      `{}` here used to silently send the vector to the bridge).
    pool.hb = None
    cfg = asyncio.run(ME._byo_embedding_config(pool, 3))
    assert cfg.get("backend_down") and not cfg.get("base_url"), \
        f"stale pin must never be dialed, and down must be flagged: {cfg}"

    # ── 5. set_embed_bastion threads the identity into the write path ───
    async def fake_validate(pool_, eid, server):
        return True, "ok", "http://[2001:db8::a:d]:11434"

    seen = {}

    async def fake_apply(pool_, eid, model, **kw):
        seen.update(kw, model=model)
        return {"ok": True}

    EB.validate_embed_bastion_target = fake_validate
    E.apply_embedding_switch = fake_apply
    asyncio.run(EB.set_embed_bastion(
        pool, 3, "DESKTOP-X", model="qwen3-embedding:4b", dim=2560))
    assert seen.get("bastion") == "DESKTOP-X", \
        f"set_embed_bastion must pass the identity: {seen}"

    # ── 6. bastion-lane reader carries peer_forward; embeds tunnel through it ─
    pool = FakePool({
        "embedding_model": "qwen3-embedding:4b", "embedding_provider": "local",
        "embedding_base_url": "http://[2001:db8::a:b]:11434",
        "embedding_bastion": "DESKTOP-X", "embedding_dim": 2560,
    })
    pool.hb = {"reported_ip": "2001:db8::a:d"}
    cfg = asyncio.run(ME._byo_embedding_config(pool, 3))
    assert cfg.get("peer_forward"), f"bastion-lane cfg must carry peer_forward: {cfg}"

    async def tunnel_roundtrip():
        tunneled = {"connect": None}

        async def handle(reader, writer):
            line = await reader.readline()          # CONNECT [ip]:port HTTP/1.1
            tunneled["connect"] = line.decode()
            while (await reader.readline()) not in (b"\r\n", b"\n", b""):
                pass
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            # now the tunneled POST arrives; drain headers, honor Content-Length
            clen = 0
            await reader.readline()                 # POST /v1/embeddings HTTP/1.1
            while True:
                h = await reader.readline()
                if h in (b"\r\n", b"\n", b""):
                    break
                if h.lower().startswith(b"content-length:"):
                    clen = int(h.split(b":")[1])
            if clen:
                await reader.readexactly(clen)
            # Answer CHUNKED, exactly like real ollama /v1/embeddings — the
            # 2026-07-10 probe failure was a helper that couldn't dechunk.
            body = json.dumps({"data": [{"embedding": [0.5] * 2560}]}).encode()
            half = len(body) // 2
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                         b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
                         + f"{half:x}\r\n".encode() + body[:half] + b"\r\n"
                         + f"{len(body)-half:x}\r\n".encode() + body[half:] + b"\r\n"
                         + b"0\r\n\r\n")
            await writer.drain()
            writer.close()

        srv = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        try:
            vec = await ME._embed_via_byo(
                "probe", "qwen3-embedding:4b",
                {"base_url": "http://[2001:db8::a:d]:11434",
                 "peer_forward": f"127.0.0.1:{port}"})
        finally:
            srv.close()
            await srv.wait_closed()
        return vec, tunneled["connect"]

    vec, connect_line = asyncio.run(tunnel_roundtrip())
    assert vec and len(vec) == 2560, f"tunneled embed returned no/short vector: {len(vec) if vec else None}"
    assert connect_line and "[2001:db8::a:d]:11434" in connect_line, \
        f"embed did not CONNECT through the peer forward: {connect_line!r}"

    print("OK identity persisted, cleared for pure-BYO, resolved live, "
          "stale pin refused, write path threads identity, "
          "bastion embeds tunnel via hub peer-forward")
    return 0


if __name__ == "__main__":
    sys.exit(main())
