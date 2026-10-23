#!/usr/bin/env python3
"""Serve the REAL network router over a REAL socket so the machine-offer routes can be
curled the way the Circles browser reaches them.

scripts/test_machine_offer_routes.py drives the same routes in-process, which is
what runs on every gate. This one exists for the other half of the question:
real TCP, real HTTP parsing, real curl, bound v6 the way the prod kernel binds
(uvicorn --host ::). Run it, then curl http://[::1]:8877/api/network/...

Everything pod-external is stubbed at the boundary — the pool, the mgmt relay
down to the bastion, the peer lane out to the other host — and each of those
writes a line to /tmp/t2596_trace.log, so you can see exactly what left this
host for a given call. The refusal paths are the interesting ones: their trace
must be EMPTY, because the token the other host mints is single-use.

  python3 scripts/e2e_machine_offer_curl.py &     # then curl [::1]:8877
"""
import os, sys, json, base64, datetime as dt
_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain")]

from fastapi import FastAPI
import server.brain_service as BS
import server.routes.network as N
from server.network import machine_offers as mo, pairing_grants as pg
from server.peer import peer_client
from server.applications.tokens import AppToken
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

SERVER, SUBJECT, HANDLE, PEER = "box-a", "friend.zone.example.test", "friend", 9122
ANCHOR = "entity-1.zone.example.test"
TRACE = os.environ.get("T2596_TRACE", "/tmp/t2596_trace.log")
BASTION_VERSION = os.environ.get("T2596_BASTION_VERSION", "0.29.0")


def trace(line):
    with open(TRACE, "a") as f:
        f.write(line + "\n")


class Pool:
    def __init__(self):
        self.offer = None
        self.nid = 40

    async def execute(self, q, *a):
        if "state='accepted'" in q or "state='declined'" in q:
            self.offer = {**self.offer, "state": "accepted" if "accepted" in q else "declined"}

    async def fetch(self, q, *a):
        ok = self.offer and self.offer["state"] == "open"
        return [self.offer] if "FROM network_machine_offers" in q and ok else []

    async def fetchrow(self, q, *a):
        if "FROM network_bastion_heartbeat" in q:
            return {"token_id": 7, "hostname": SERVER,
                    "anchor": json.dumps({"anchor_host": ANCHOR, "owner": "Alex"}),
                    "pairings": json.dumps([]),          # no beat since the redeem
                    "gateway_version": BASTION_VERSION}
        if "FROM network_machine_offers" in q:
            return self.offer
        return None

    async def fetchval(self, q, *a):
        if "INSERT INTO" in q:
            self.nid += 1
            return self.nid
        return None

    def acquire(self):
        p = self

        class C:
            async def __aenter__(self):
                return p

            async def __aexit__(self, *e):
                return False
        return C()


POOL = Pool()
POOL.offer = {"id": 9, "direction": "in", "peer_entity_id": PEER, "peer_handle": "alice",
              "peer_display": "Alice", "purpose": "embeddings for my notes",
              "models_asked": json.dumps(["qwen4-embed"]), "state": "open", "server": None,
              "subject": None, "granted_caps": None, "granted_models": None, "tokens_issued": 0,
              "last_token_at": None, "created_at": dt.datetime.now(dt.timezone.utc),
              "decided_at": None, "decided_by": None, "detail": None}
BS.get_pool = lambda: POOL
BS._entity_id = 9003


async def fake_send_to_peer(peer_id, op, slip, params, timeout=10.0):
    trace(f"PEER-LANE  -> entity-{peer_id}  op={op}")
    return {"ok": True, "data": {"token": "skb_FRESH", "expires_in_s": 900}}
peer_client.send_to_peer = fake_send_to_peer


async def fake_dispatch(pool, eid, server, op, args, timeout=None):
    trace(f"MGMT-LANE  -> {server}  op={op}  "
          f"args={'<the token>' if op == pg.OP_REDEEM else f'{len(args)} arg(s)'}  timeout={timeout}")
    if op == pg.OP_REDEEM:
        return {"exit": 0, "stdout": json.dumps({"state": "ok", "subject": SUBJECT,
                                                 "handle": HANDLE, "pairing_state": "pending"}),
                "stderr": ""}
    if op == pg.OP_GRANT_SYNC:
        trace("SIGNED-TERMS " + base64.b64decode(args[0]).decode())
    return {"exit": 0, "stdout": "", "stderr": ""}
pg._dispatch = fake_dispatch

_BURNED = set()


async def burn_on_use(pool, code):
    """Like the real one: the counter is recorded and a code works ONCE."""
    if code != "123456" or code in _BURNED:
        return False
    _BURNED.add(code)
    return True
pg._verify_stepup = burn_on_use


async def own(eid):
    return (ANCHOR,)
pg._own_host_names = own


async def vk(pool, eid):
    return "AAAA"
pg._verify_pubkey_b64 = vk

K = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))


async def sign(pool, eid, terms):
    c = pg.canonical(terms)
    return base64.b64encode(c).decode(), base64.b64encode(K.sign(c)).decode()
pg._sign = sign

import server.network.nac_mgmt as nm


async def noaudit(pool, eid, server, op, *, exit_code, decision):
    pass
nm.audit_mgmt = noaudit


async def notell(*a, **k):
    pass
mo._tell_them = notell

app = FastAPI()


@app.middleware("http")
async def app_token(request, call_next):
    """The app-token middleware's job: stamp request.state.app_token with the
    scopes the caller's token declared. `x-app-scopes` stands in for the token
    so a curl can be a Circles that holds them, or one that does not."""
    scopes = request.headers.get("x-app-scopes")
    if scopes is not None:
        request.state.app_token = AppToken(app_id="circles", window_id="w1", iat=0,
                                           permissions=[s for s in scopes.split(",") if s])
    return await call_next(request)

app.include_router(N.router)

if __name__ == "__main__":
    import uvicorn
    from gate_loopback import IDLE_KEEP_ALIVE_S
    open(TRACE, "w").close()
    uvicorn.run(app, host="::1", port=int(os.environ.get("T2596_PORT", "8877")), log_level="warning",
                timeout_keep_alive=IDLE_KEEP_ALIVE_S)
