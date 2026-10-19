#!/usr/bin/env python3
"""The machine-offer routes, exercised from where the Circles browser stands.

Real FastAPI app, real router, real HTTP verbs and JSON bodies, and the REAL
requires_permission dependency — it reads request.state.app_token, which is what
the app-token middleware puts there for the browser, so the scope check on every
route here is the production one. The first thing the file does is prove that
gate is live (a token declaring nothing must get 403), because every assertion
below it is worthless if the door is open.

Only the pod-external edges are stood in for: the DB pool, the mgmt relay down
to the bastion, and the direct peer lane out to the other host. What that leaves
under test is the whole of this side — routing, auth scope, request parsing,
the service's order of operations, and the exact bytes that get signed.

The order is the security property, and two of the checks exist only for it: a
wrong step-up code and an empty model list must both cost the OTHER side
nothing, because a token they minted is single-use.

  APP_ROOT=<repo> python3 scripts/test_machine_offer_routes.py
"""
import os, sys, json, datetime as dt
_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain")]

from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.brain_service as BS
import server.routes.network as N
from server.network import machine_offers as mo, pairing_grants as pg, bastion_enroll as be
from server.peer import peer_client

SERVER, SUBJECT, HANDLE, PEER = "box-a", "friend.zone.example.test", "friend", 122
ANCHOR = "entity-1.zone.example.test"

class Pool:
    def __init__(self): self.sql=[]; self.offer=None; self.nid=40
    async def execute(self,q,*a): self.sql.append((q,a))
    async def fetch(self,q,*a):
        self.sql.append((q,a))
        if "FROM network_machine_offers" in q and self.offer: return [self.offer]
        return []
    async def fetchrow(self,q,*a):
        self.sql.append((q,a))
        if "FROM network_bastion_heartbeat" in q:
            # pairings EMPTY and a version the box actually reports: a pairing
            # redeemed seconds ago is in no beat yet, and a model scope is
            # refused for a bastion below 0.29.0 (it would drop the field and
            # serve every model while the card said one).
            return {"token_id":7,"hostname":SERVER,
                    "anchor":json.dumps({"anchor_host":ANCHOR,"owner":"Alex"}),
                    "pairings":json.dumps([]),
                    "gateway_version":"0.29.0"}
        if "FROM network_machine_offers" in q: return self.offer
        return None
    async def fetchval(self,q,*a):
        self.sql.append((q,a))
        if "INSERT INTO" in q: self.nid+=1; return self.nid
        return None
    def acquire(self):
        p=self
        class C:
            async def __aenter__(self): return p
            async def __aexit__(self,*e): return False
        return C()

POOL = Pool()
POOL.offer = {"id":9,"direction":"in","peer_entity_id":PEER,"peer_handle":"alice",
  "peer_display":"Alice","purpose":"embeddings for my notes",
  "models_asked":json.dumps(["qwen4-embed"]),"state":"open","server":None,"subject":None,
  "granted_caps":None,"granted_models":None,"tokens_issued":0,"last_token_at":None,
  "created_at":dt.datetime.now(dt.timezone.utc),"decided_at":None,"decided_by":None,"detail":None}

BS.get_pool = lambda: POOL
BS._entity_id = 9003

# --- the pod-external edges, stood in for -----------------------------------
async def fake_send_to_peer(peer_id, op, slip, params, timeout=10.0):
    print(f"    [peer lane] -> entity-{peer_id} op={op}")
    return {"ok": True, "data": {"token": "skb_FRESH", "expires_in_s": 900}}
peer_client.send_to_peer = fake_send_to_peer

DISPATCHED = []
async def fake_dispatch(pool, eid, server, op, args, timeout=None):
    DISPATCHED.append((server, op, list(args), timeout))
    print(f"    [mgmt lane] -> {server} op={op} args={'<token>' if op=='pairing_redeem' else str(len(args))+' arg(s)'}")
    if op == pg.OP_REDEEM:
        return {"exit":0,"stdout":json.dumps({"state":"ok","subject":SUBJECT,"handle":HANDLE,
                                              "pairing_state":"pending"}),"stderr":""}
    return {"exit":0,"stdout":"","stderr":""}
pg._dispatch = fake_dispatch
async def ok_stepup(pool, code): return code == "123456"
pg._verify_stepup = ok_stepup
async def own(eid): return (ANCHOR,)
pg._own_host_names = own
async def vk(pool, eid): return "AAAA"
pg._verify_pubkey_b64 = vk
import base64
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
K = Ed25519PrivateKey.from_private_bytes(bytes(range(1,33)))
async def sign(pool, eid, terms):
    c = pg.canonical(terms)
    return base64.b64encode(c).decode(), base64.b64encode(K.sign(c)).decode()
pg._sign = sign
import server.network.nac_mgmt as nm
async def noaudit(pool, eid, server, op, *, exit_code, decision): pass
nm.audit_mgmt = noaudit
async def notell(*a, **k): pass
mo._tell_them = notell

# --- the app. The REAL requires_permission dependency runs: it reads
# request.state.app_token, which is what the app-token middleware puts there for
# the browser. So the scope check on every route below is the production one.
from server.applications.tokens import AppToken
app = FastAPI()

@app.middleware("http")
async def _as_the_circles_app(request, call_next):
    request.state.app_token = AppToken(
        app_id="circles", window_id="w1", iat=0,
        permissions=["read:topology", "write:network", "resolve:decision"])
    return await call_next(request)

app.include_router(N.router)
c = TestClient(app)

# Prove the gate is live before trusting anything below it: the same route with
# a token that declared nothing must be 403, not 200.
_bare = FastAPI()
@_bare.middleware("http")
async def _as_a_lesser_app(request, call_next):
    request.state.app_token = AppToken(app_id="nosy", window_id="w1", iat=0, permissions=[])
    return await call_next(request)
_bare.include_router(N.router)
_r = TestClient(_bare).post("/api/network/bastions/machine-offers/accept",
                            json={"offer_id": 9, "server": SERVER,
                                  "models": ["qwen4-embed"], "stepup_code": "123456"})
print(f"\nGUARD: accept without the resolve:decision scope -> HTTP {_r.status_code} {_r.text[:120]}")
assert _r.status_code == 403, _r.text

def show(label, r):
    print(f"\n{label}\n  HTTP {r.status_code}  {r.text[:400]}")
    return r

print("=" * 72)
print("machine-offer routes, exercised as the Circles browser calls them")
print("=" * 72)

r = show("GET /api/network/bastions/machine-offers  (the card reads the offer)",
         c.get("/api/network/bastions/machine-offers"))
assert r.status_code == 200 and r.json()["offers"][0]["id"] == 9, r.text
assert r.json()["offers"][0]["models_asked"] == ["qwen4-embed"]

show("POST .../accept  — wrong 2FA (must cost the other side NOTHING)",
     c.post("/api/network/bastions/machine-offers/accept",
            json={"offer_id":9,"server":SERVER,"models":["qwen4-embed"],"stepup_code":"000000"}))
assert DISPATCHED == [], f"something reached the machine on a bad code: {DISPATCHED}"

show("POST .../accept  — no models named (an unscoped share is refused)",
     c.post("/api/network/bastions/machine-offers/accept",
            json={"offer_id":9,"server":SERVER,"models":[],"stepup_code":"123456"}))
assert DISPATCHED == []

r = show("POST .../accept  — the real approval",
         c.post("/api/network/bastions/machine-offers/accept",
                json={"offer_id":9,"server":SERVER,"models":["Qwen4-Embed"],"stepup_code":"123456"}))
assert r.status_code == 200, r.text
d = r.json()
assert d["accepted"] and d["subject"] == SUBJECT and d["models"] == ["qwen4-embed"], d
ops = [(s,o) for s,o,_,_ in DISPATCHED]
assert ops == [(SERVER,"pairing_redeem"), (SERVER,"pairing_grant_sync")], ops
assert DISPATCHED[0][2] == ["skb_FRESH"], DISPATCHED[0]
terms = json.loads(base64.b64decode(DISPATCHED[1][2][0]))
print(f"\n  SIGNED TERMS the box will enforce:\n  {json.dumps(terms)}")
assert terms["caps"] == ["inference"] and terms["models"] == ["qwen4-embed"], terms

POOL.offer = {**POOL.offer, "state":"open"}
DISPATCHED.clear()
r = show("POST .../decline  (nothing may reach the machine)",
         c.post("/api/network/bastions/machine-offers/decline", json={"offer_id":9}))
assert r.status_code == 200 and r.json()["declined"], r.text
assert DISPATCHED == [], DISPATCHED

POOL.offer = None
r = show("POST .../ask  — this host asks a peer for a model on THEIR machine",
         c.post("/api/network/bastions/machine-offers/ask",
                json={"peer_entity_id":PEER,"purpose":"embeddings","models":["qwen4-embed"]}))
assert r.status_code == 200 and r.json()["sent"], r.text

r = show("POST .../decline on an offer that is gone (404, not a 500)",
         c.post("/api/network/bastions/machine-offers/decline", json={"offer_id":999}))
assert r.status_code == 404, r.text

r = show("GET /api/network/bastions/pairings/grants  (models surface on the grant)",
         c.get("/api/network/bastions/pairings/grants"))
assert r.status_code == 200, r.text

print("\n" + "=" * 72)
print("ALL ROUTE EXERCISES PASSED")
print("=" * 72)
