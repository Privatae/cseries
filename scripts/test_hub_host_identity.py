#!/usr/bin/env python3
"""The Hub's WireGuard key is this Host's own.

Until this change the operator made every Host's Hub private key and kept it in
a Kubernetes Secret, and the bridge handed its public half to every machine that
paired. The key now lives in this Host's kernel_secrets (hub_identity), reaches
the Hub over its loopback control API, and machines move to it in band through
the rotate directive while the Hub still answers the platform's old key beside
it. What this gate holds:

  1. the key is write-once: two first callers racing on a fresh pod store and
     return ONE key, and its public half is the RFC 7748 derivation;
  2. every answer the Hub's POST /identity and GET /identity can give is handled
     without raising (204, 409 loud, 404 quiet, 400, unreachable);
  3. the platform's old key is retired only when the Hub holds ours and every
     machine it serves is on ours and has said, on a beat of its own sent
     after its move, that the move is saved;
  4. a pairing hands the machine THIS Host's key, not the bridge's, and still
     pairs when the bridge has none; an old Hub keeps the bridge's;
  5. the heartbeat tells a machine still on the old key to move, and leaves a
     moved one alone; a client that cannot probe is never told;
  6. run through the real reconcile loop, an old hub image or a Hub holding a
     different key is reported once, not every tick (the PDP arm that follows
     it clears its own reports on every success);
  7. right after a tick arms the Hub, a beat is told the Hub's new key, never
     the platform's (the machine would probe it and move back) nor the bridge's
     stale copy of it (nothing answers it);
  8. a Hub that does not answer is not an old Hub: pairing is refused with the
     token given back, and no directive goes out, instead of both falling back
     to the bridge's key, which the Hub may have retired;
  9. a key move is decided on the Hub's answer now: a Hub that restarted since
     the tick is not asked to take a machine on a key it does not hold yet.

Stub Hub (aiohttp on 127.0.0.1), stub pool, stub bridge. No DB, no pod, no
network beyond loopback.

    APP_ROOT=<repo> python3 scripts/test_hub_host_identity.py
"""
import asyncio
import base64
import logging
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from aiohttp import web  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from server.network import bastion_enroll, hub_identity, hub_peers, topology  # noqa: E402

FAILED = []


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  [{detail}]"))
    if not cond:
        FAILED.append(label)


# ── stub pool ────────────────────────────────────────────────────────────────

class _Conn:
    """kernel_secrets with INSERT ... ON CONFLICT DO NOTHING semantics. Every call
    yields once, so two coroutines started together interleave the way two
    requests on a fresh pod do: both read "no row" before either writes."""

    def __init__(self, pool):
        self.pool = pool

    async def execute(self, sql, *args):
        await asyncio.sleep(0)
        if sql.lstrip().startswith("INSERT INTO kernel_secrets"):
            self.pool.inserts += 1
            self.pool.secrets.setdefault(args[0], args[1])

    async def fetchrow(self, sql, *args):
        await asyncio.sleep(0)
        if "FROM kernel_secrets" in sql:
            v = self.pool.secrets.get(args[0])
            return None if v is None else {"secret": v}
        raise AssertionError(f"unexpected fetchrow: {sql}")


class _Acquire:
    def __init__(self, pool):
        self.pool = pool

    async def __aenter__(self):
        return _Conn(self.pool)

    async def __aexit__(self, *a):
        return False


class FakePool:
    def __init__(self, bastions=()):
        self.secrets = {}
        self.inserts = 0
        self.bastions = list(bastions)
        self.token_used = False

    def acquire(self):
        return _Acquire(self)

    async def fetch(self, sql, *args):
        if "FROM mcp_client_tokens" in sql:
            return [dict(b) for b in self.bastions]
        raise AssertionError(f"unexpected fetch: {sql}")

    async def fetchrow(self, sql, *args):
        if sql.startswith("UPDATE bastion_pairing_tokens"):
            self.token_used = True
            return {"name": "nas"}
        if "INSERT INTO mcp_client_tokens" in sql:
            return {"id": 7}
        raise AssertionError(f"unexpected fetchrow: {sql}")


# ── stub Hub control API ─────────────────────────────────────────────────────

class StubHub:
    def __init__(self):
        self.post_status = 204
        self.get_status = 200
        self.verifykey_status = 204
        self.get_body = {"public_key": None, "legacy_public_key": None, "peers": []}
        self.arm_sets_key = True       # a 204 POST makes GET report the posted key
        self.posts = []
        self.gets = 0
        self.runner = None

    async def start(self):
        async def post(request):
            body = await request.json()
            self.posts.append(body)
            if self.post_status == 204 and self.arm_sets_key:
                raw = base64.b64decode(body["key"])
                self.get_body = dict(self.get_body,
                                     public_key=hub_identity.public_from_private(raw))
                if body.get("retire_legacy"):
                    self.get_body["legacy_public_key"] = None
            return web.Response(status=self.post_status)

        async def get(request):
            self.gets += 1
            if self.get_status != 200:
                return web.Response(status=self.get_status)
            return web.json_response(self.get_body)

        async def verifykey(request):
            return web.Response(status=self.verifykey_status)

        app = web.Application()
        app.router.add_post("/identity", post)
        app.router.add_get("/identity", get)
        app.router.add_post("/verifykey", verifykey)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        hub_peers.HUB_CONTROL = f"http://127.0.0.1:{self.runner.addresses[0][1]}"
        hub_identity._status = (0.0, None)
        return self

    async def stop(self):
        await self.runner.cleanup()


class Captured(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


class capture_logs:
    """Capture hub_peers' and hub_identity's records, and put both loggers back."""

    def __init__(self):
        self.cap = Captured()
        self.loggers = [logging.getLogger(hub_peers.__name__),
                        logging.getLogger(hub_identity.__name__)]

    def __enter__(self):
        self.saved = [(lg.level, lg.propagate) for lg in self.loggers]
        for lg in self.loggers:
            lg.addHandler(self.cap)
            lg.setLevel(logging.DEBUG)
            lg.propagate = False
        return self.cap

    def __exit__(self, *a):
        for lg, (level, propagate) in zip(self.loggers, self.saved):
            lg.removeHandler(self.cap)
            lg.setLevel(level)
            lg.propagate = propagate
        return False


def reset_reports():
    hub_peers._reset_say_once()
    getattr(hub_identity, "_said", set()).clear()


ORIG_CONTROL = hub_peers.HUB_CONTROL
LEGACY = base64.b64encode(b"L" * 32).decode()
M1 = base64.b64encode(b"\x01" * 32).decode()
M2 = base64.b64encode(b"\x02" * 32).decode()
BRIDGE = LEGACY   # what the bridge reports: the operator-made key


# ── 1. the key ───────────────────────────────────────────────────────────────

async def part_key():
    print("== 1. the Hub key is this Host's, write-once")
    pool = FakePool()
    a, b = await asyncio.gather(hub_identity.ensure_hub_wg_key(pool),
                                hub_identity.ensure_hub_wg_key(pool))
    check("two first callers racing get the SAME key, and it is the stored one",
          a == b == pool.secrets.get("hub_wg"), (a.hex()[:8], b.hex()[:8]))
    check("both raced to write, and only one write stuck", pool.inserts == 2 and len(a) == 32,
          pool.inserts)
    again = await hub_identity.ensure_hub_wg_key(pool)
    check("a later call returns the same key and writes nothing", again == a and pool.inserts == 2)
    # RFC 7748 §6.1, Alice.
    priv = bytes.fromhex("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a")
    pub = bytes.fromhex("8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a")
    check("the public key is the X25519 derivation (RFC 7748 test vector), base64 like WireGuard",
          hub_identity.public_from_private(priv) == base64.b64encode(pub).decode())


# ── 2. the Hub's answers ─────────────────────────────────────────────────────

async def part_api():
    print("== 2. POST/GET /identity: every answer, never a raise")
    hub = await StubHub().start()
    logs = capture_logs()
    cap = logs.__enter__()
    try:
        pool = FakePool()
        hub.post_status = 204
        ok = await hub_identity.arm_hub_identity(pool)
        raw = pool.secrets["hub_wg"]
        check("204 -> armed, sending this Host's key base64 and retire_legacy false",
              ok is True and hub.posts[-1] == {"key": base64.b64encode(raw).decode(),
                                               "retire_legacy": False}, hub.posts[-1:])
        ok = await hub_identity.arm_hub_identity(pool, retire_legacy=True)
        check("retire_legacy is passed through when asked", ok and hub.posts[-1]["retire_legacy"] is True)

        reset_reports()
        cap.records.clear()
        hub.post_status = 409
        ok = await hub_identity.arm_hub_identity(pool)
        check("409 (the Hub holds a different key) -> not armed, and said at ERROR",
              ok is False and any(r.levelno == logging.ERROR for r in cap.records),
              [r.getMessage()[:60] for r in cap.records])
        cap.records.clear()
        await hub_identity.arm_hub_identity(pool)
        check("...once, not every tick", not cap.records, [r.getMessage()[:60] for r in cap.records])

        for status, label in [(404, "404 (hub image predates /identity)"), (400, "400 (refused body)"),
                              (500, "500")]:
            reset_reports()
            hub.post_status = status
            check(f"{label} -> not armed", await hub_identity.arm_hub_identity(pool) is False)

        hub.get_status, hub.get_body = 200, {"public_key": "P", "legacy_public_key": None, "peers": []}
        got = await hub_identity.fetch_status()
        check("GET 200 -> the Hub's view", got == hub.get_body, got)
        hub.get_status = 404
        old = await hub_identity.fetch_status()
        check("GET 404 (old hub) -> PRE_IDENTITY: a Hub that serves only the operator's key",
              old is not None and old == getattr(hub_identity, "PRE_IDENTITY", object()), old)
        hub.get_status = 500
        check("GET 500 -> None: an answer that says nothing about which key it serves",
              await hub_identity.fetch_status() is None)
    finally:
        logs.__exit__()
        await hub.stop()

    hub_peers.HUB_CONTROL = "http://127.0.0.1:1"
    try:
        armed = await hub_identity.arm_hub_identity(FakePool())
        status = await hub_identity.fetch_status()
        check("unreachable hub -> not armed and no status, never a raise", armed is False and status is None)
        check("...and unreachable is told apart from a Hub that predates /identity",
              status != old, (status, old))
    except Exception as e:
        check("unreachable hub -> not armed and no status, never a raise", False, repr(e))
    finally:
        hub_peers.HUB_CONTROL = ORIG_CONTROL


# ── 3. retiring the platform's key ───────────────────────────────────────────

def part_retire():
    print("== 3. the platform's key is retired only when nothing needs it")
    own = "OWN"

    def st(peers, legacy=LEGACY, public=own):
        return {"public_key": public, "legacy_public_key": legacy,
                "peers": [{"public_key": k, "on": on, "last_handshake": 1} for k, on in peers]}

    r = hub_identity.should_retire_legacy
    pending = hub_identity._beat_save_pending
    pending.clear()
    # The tick's served set: each machine under its own bastion token. A beat
    # carries the token its bearer resolved to (routes/network.py).
    hub_identity._served = {M1: {1}, M2: {2}}
    tok = {M1: 1, M2: 2}

    def says(m, saved_pending, token=None):
        hub_identity.note_heartbeat(m, {"token_id": tok[m] if token is None else token,
                                        "endpoint_save_pending": saved_pending})

    for m in (M1, M2):
        says(m, False)
    check("legacy running, ours held, every machine on ours -> retire",
          r(st([(M1, "host"), (M2, "host")]), own, {M1, M2}) is True)
    check("one machine still on the legacy key -> keep it",
          r(st([(M1, "host"), (M2, "legacy")]), own, {M1, M2}) is False)
    check("a machine the Hub has not seen handshake yet -> keep it",
          r(st([(M1, "host"), (M2, None)]), own, {M1, M2}) is False)
    check("a machine the Hub does not list at all -> keep it",
          r(st([(M1, "host")]), own, {M1, M2}) is False)
    check("no legacy key running -> nothing to retire", r(st([(M1, "host")], legacy=None), own, {M1}) is False)
    check("the Hub does not hold OUR key -> never retire (it would hold nothing we gave it)",
          r(st([(M1, "host")], public="SOMEONE-ELSE"), own, {M1}) is False)
    check("no machines at all -> retire at once (none pinned the old key)", r(st([]), own, set()) is True)
    check("a machine that is not ours to serve (bearer gone) does not hold it open",
          r(st([(M1, "host"), (M2, "legacy")]), own, {M1}) is True)

    # The handshake view shows the key a machine RUNS on, not the one on its disk.
    says(M2, True)
    check("a machine running ours whose config save keeps failing -> keep the legacy key "
          "(its restart would come up on it)", r(st([(M1, "host"), (M2, "host")]), own, {M1, M2}) is False)
    says(M2, False, token=1)
    check("another paired machine's bearer cannot report M2's save as done (its beat names M2's key "
          "but carries its own token) -> keep it", r(st([(M1, "host"), (M2, "host")]), own, {M1, M2}) is False)
    says(M2, False)
    check("...and once its save lands -> retire", r(st([(M1, "host"), (M2, "host")]), own, {M1, M2}) is True)
    pending.pop(M2)
    check("a machine that has not beaten since this process started -> keep it (its save is unknown)",
          r(st([(M1, "host"), (M2, "host")]), own, {M1, M2}) is False)
    hub_identity.note_heartbeat(M2, {"wg_pubkey": M2, "token_id": 2})
    check("a client too old to report its save is judged on its handshake alone",
          r(st([(M1, "host"), (M2, "host")]), own, {M1, M2}) is True)
    before = dict(pending)
    hub_identity.note_heartbeat("", {"token_id": 1, "endpoint_save_pending": True})
    check("a beat with no machine key records nothing", pending == before, pending)
    for i in range(50):
        hub_identity.note_heartbeat(f"STRANGER{i}", {"token_id": 1, "endpoint_save_pending": False})
    hub_identity.note_heartbeat(M2, {"endpoint_save_pending": True})
    check("beats for keys the Hub does not serve, or with no token, record nothing (the record stays "
          "the size of the fleet)", pending == before, sorted(pending)[:5])


async def part_converge():
    print("== 3b. one reconcile tick: arm, then retire on the next once everyone moved")
    hub = await StubHub().start()
    try:
        pool = FakePool(bastions=[{"id": 1, "pubkey": M1, "tunnel_ip": "2001:db8::a:1"},
                                  {"id": 2, "pubkey": M2, "tunnel_ip": "2001:db8::a:2"}])
        hub.get_body = {"public_key": None, "legacy_public_key": LEGACY,
                        "peers": [{"public_key": M1, "on": "legacy", "last_handshake": 1},
                                  {"public_key": M2, "on": "legacy", "last_handshake": 1}]}
        ok = await hub_identity.converge_identity(pool)
        check("first tick: the Hub gets our key, legacy kept", ok and hub.posts[-1]["retire_legacy"] is False)
        hub.get_body["peers"][0]["on"] = "host"
        await hub_identity.converge_identity(pool)
        check("one machine moved, one not: legacy kept", hub.posts[-1]["retire_legacy"] is False)
        hub.get_body["peers"][1]["on"] = "host"
        hub_identity._beat_save_pending.clear()
        # GONE beat while it was still served, and was unpaired before this tick.
        hub_identity._served["GONE"] = {9}
        hub_identity.note_heartbeat(M1, {"token_id": 1, "endpoint_save_pending": False})
        hub_identity.note_heartbeat(M2, {"token_id": 2, "endpoint_save_pending": True})
        hub_identity.note_heartbeat("GONE", {"token_id": 9, "endpoint_save_pending": True})
        await hub_identity.converge_identity(pool)
        check("both run on ours but one has not saved it: legacy kept",
              hub.posts[-1]["retire_legacy"] is False)
        check("a machine no longer served is forgotten", "GONE" not in hub_identity._beat_save_pending,
              hub_identity._beat_save_pending)
        hub_identity.note_heartbeat(M2, {"token_id": 2, "endpoint_save_pending": False})
        await hub_identity.converge_identity(pool)
        check("both moved: the tick retires the platform's key",
              hub.posts[-1]["retire_legacy"] is True and hub.get_body["legacy_public_key"] is None)
    finally:
        await hub.stop()
        hub_peers.HUB_CONTROL = ORIG_CONTROL


async def part_move_unsaved():
    print("== 3c. a move is judged saved only on a beat sent after the move (bastion 0.47.2 order)")
    # The client's order (client-go run.go beat, rotate.go probeAndCommit): a beat's
    # body is built BEFORE its answer is read; the answer's directive makes it probe
    # our key (the Hub records that handshake on "host"), commit, and save. A failed
    # save is reported on the NEXT beat, a minute later. The 15 s tick runs between.
    saved = topology._desired_bastion_endpoint
    topology._desired_bastion_endpoint = _desired
    hub = await StubHub().start()
    try:
        pool = FakePool(bastions=[{"id": 1, "pubkey": M1, "tunnel_ip": "2001:db8::a:1"}])
        own = await hub_identity.hub_wg_pubkey(pool)
        hub_identity._served = {}
        hub_identity._beat_save_pending.clear()
        hub.get_body = {"public_key": own, "legacy_public_key": LEGACY,
                        "peers": [{"public_key": M1, "on": "legacy", "last_handshake": 1}]}
        await hub_identity.converge_identity(pool)
        check("the tick learns the fleet (M1 under its token), legacy kept",
              hub.posts[-1]["retire_legacy"] is False)

        d = await topology._rotate_directive(pool, _beat(E6, endpoint_save_pending=False))
        check("beat k (nothing pending yet) is told to move to our key",
              d is not None and d["endpoint_pubkey"] == own, d)
        hub.get_body["peers"][0]["on"] = "host"   # its probe handshook on ours; the save then failed
        await hub_identity.converge_identity(pool)
        check("the tick between the move and the next beat keeps the legacy key (beat k's 'nothing "
              "pending' was built before the move)", hub.posts[-1]["retire_legacy"] is False)
        getattr(hub_identity, "_unsaved_said", set()).clear()
        logs = capture_logs()
        cap = logs.__enter__()
        try:
            await topology._rotate_directive(pool, _beat(E6, endpoint_save_pending=True))
            await hub_identity.converge_identity(pool)
            check("beat k+1 reports the failed save: legacy kept", hub.posts[-1]["retire_legacy"] is False)
            await topology._rotate_directive(pool, _beat(E6, endpoint_save_pending=True))
            await hub_identity.converge_identity(pool)
        finally:
            logs.__exit__()
        said = [r.getMessage() for r in cap.records
                if r.levelno == logging.WARNING and "cannot save" in r.getMessage()]
        check("the hold is said at WARNING naming the machine (it can last as long as the save fails, "
              "and the machine says so only in its own log), once over two ticks",
              len(said) == 1 and M1[:12] in said[0], said)
        # An IPv4-only network: it beats from the v4 entry and is sent the endpoint
        # directive on every beat. That is not a key move and forgets nothing.
        await topology._rotate_directive(pool, _beat(E4, endpoint_save_pending=False))
        await hub_identity.converge_identity(pool)
        check("its save lands (beating from the v4 entry, endpoint directive every beat): retired",
              hub.posts[-1]["retire_legacy"] is True and hub.get_body["legacy_public_key"] is None)
    finally:
        topology._desired_bastion_endpoint = saved
        hub_identity._status = (0.0, None)
        await hub.stop()
        hub_peers.HUB_CONTROL = ORIG_CONTROL


# ── 4. pairing ───────────────────────────────────────────────────────────────

CSR = "-----BEGIN CERTIFICATE REQUEST-----\nnot parsed here\n-----END CERTIFICATE REQUEST-----"


async def _redeem(pool, bridge_pubkey, rearmed):
    async def fake_bridge(op, params):
        out = {"operator_crt": "CRT", "ca_crt": "CA", "endpoint": "198.51.100.7:52007",
               "endpoints": ["[2001:db8::7]:51820", "198.51.100.7:52007"],
               "host_tunnel_ip": "2001:db8::e:3", "bastion_tunnel_ip": "2001:db8::a:7"}
        if bridge_pubkey is not None:
            out["endpoint_pubkey"] = bridge_pubkey
        return out

    async def fake_rearm(pool_, secret_hash):
        rearmed.append(secret_hash)

    async def fake_push(*a, **k):
        return True

    saved = (bastion_enroll._bridge_op, bastion_enroll._rearm_pairing_token, hub_peers.push_bastion_peer)
    bastion_enroll._bridge_op = fake_bridge
    bastion_enroll._rearm_pairing_token = fake_rearm
    hub_peers.push_bastion_peer = fake_push
    try:
        return await bastion_enroll.redeem(pool, entity_id=9003, handle="alex", secret="s3cret",
                                           bastion_pubkey=M1, operator_csr=CSR)
    finally:
        bastion_enroll._bridge_op, bastion_enroll._rearm_pairing_token, hub_peers.push_bastion_peer = saved


async def part_redeem():
    print("== 4. a pairing pins THIS Host's key")
    hub = await StubHub().start()
    try:
        pool, rearmed = FakePool(), []
        hub.get_body = {"public_key": None, "legacy_public_key": LEGACY, "peers": []}
        out = await _redeem(pool, BRIDGE, rearmed)
        own = hub_identity.public_from_private(pool.secrets["hub_wg"])
        check("fresh pod, Hub not armed yet: it is armed on the spot and the machine gets OUR key, "
              "not the bridge's", out["endpoint_pubkey"] == own and own != BRIDGE and hub.posts,
              out["endpoint_pubkey"])

        pool, rearmed = FakePool(), []
        hub.posts.clear()
        hub.get_body = {"public_key": None, "legacy_public_key": None, "peers": []}
        out = await _redeem(pool, None, rearmed)
        check("the bridge has no Hub key at all (the platform stopped making them): still pairs, on ours",
              out["endpoint_pubkey"] == hub_identity.public_from_private(pool.secrets["hub_wg"]),
              out.get("endpoint_pubkey"))

        pool, rearmed = FakePool(), []
        hub.get_status = 404
        out = await _redeem(pool, BRIDGE, rearmed)
        check("an old Hub (no /identity) serves only the operator's key: the machine gets that one",
              out["endpoint_pubkey"] == BRIDGE, out["endpoint_pubkey"])

        pool, rearmed = FakePool(), []
        hub.get_status = 200
        hub.post_status = 409
        hub.get_body = {"public_key": "SOMEONE-ELSES", "legacy_public_key": None, "peers": []}
        try:
            await _redeem(pool, BRIDGE, rearmed)
            check("a Hub holding a key that is not ours: refused, never paired to it", False, "no raise")
        except HTTPException as e:
            check("a Hub holding a key that is not ours: refused (503), never paired to it",
                  e.status_code == 503, e.status_code)
        check("...and the single-use token is given back for a retry", len(rearmed) == 1, rearmed)
    finally:
        await hub.stop()
        hub_peers.HUB_CONTROL = ORIG_CONTROL


# ── 5. the heartbeat's rotate directive ──────────────────────────────────────

E6, E4 = "[2001:db8::7]:51820", "198.51.100.7:52007"


async def _desired(wg_pubkey):
    # The bridge's answer for an operator-keyed Host: its hub_pubkey column keeps
    # the operator-made key after the Hub stops answering it (bridge/adapter.py
    # op_bastion_desired_endpoint, `hub_pubkey or None`).
    return {"endpoint": E4, "endpoints": [E6, E4], "endpoint_pubkey": BRIDGE}


def _beat(ep, version="0.47.0", machine=M1, token=1, **extra):
    # token_id is what the route resolves the bearer to (routes/network.py).
    return {"endpoint": ep, "wg_pubkey": machine, "gateway_version": version,
            "token_id": token, **extra}


async def part_directive():
    print("== 5. the heartbeat moves machines onto the Host's key")
    pool = FakePool()
    own = await hub_identity.hub_wg_pubkey(pool)
    hub = await StubHub().start()
    hub.arm_sets_key = False

    saved = topology._desired_bastion_endpoint
    topology._desired_bastion_endpoint = _desired

    def hub_view(status):
        # What the Hub answers now, and the tick's copy of it.
        hub.get_status, hub.get_body = 200, status
        hub_identity._status = (time.monotonic(), status)

    beat = _beat

    try:
        hub_identity._served = {M1: {1}}
        hub_identity._beat_save_pending.clear()
        hub_view({"public_key": own, "legacy_public_key": LEGACY,
                  "peers": [{"public_key": M1, "on": "host", "last_handshake": 1}]})
        await topology._rotate_directive(pool, beat(E6, endpoint_save_pending=True))
        check("every beat records whether the machine has saved its last move",
              hub_identity._beat_save_pending.get(M1) is True, hub_identity._beat_save_pending)
        hub_view({"public_key": own, "legacy_public_key": LEGACY,
                  "peers": [{"public_key": M1, "on": "legacy", "last_handshake": 1}]})
        await topology._rotate_directive(pool, beat(E6, endpoint_save_pending=False))
        check("a beat that tells the machine to move leaves its save unknown (what it said was built "
              "before the move)", M1 not in hub_identity._beat_save_pending, hub_identity._beat_save_pending)
        d = await topology._rotate_directive(pool, beat(E6))
        check("on the preferred endpoint but still on the platform's key: told to move, to OUR key",
              d is not None and d["endpoint_pubkey"] == own and d["endpoints"] == [E6, E4], d)
        d = await topology._rotate_directive(pool, beat(E6, version="0.13.9"))
        check("a client that cannot probe before committing is never told (it would swap blind)", d is None, d)
        # It beats every minute for as long as the move lasts: one WARNING, not one per beat.
        tlog = logging.getLogger("privatae.network.topology")
        tcap = Captured()
        saved_level, saved_prop = tlog.level, tlog.propagate
        tlog.addHandler(tcap)
        tlog.setLevel(logging.DEBUG)
        tlog.propagate = False
        getattr(topology, "_withheld_said", set()).clear()
        try:
            for _ in range(3):
                await topology._rotate_directive(pool, beat(E6, version="0.13.9"))
        finally:
            tlog.removeHandler(tcap)
            tlog.setLevel(saved_level)
            tlog.propagate = saved_prop
        warned = [r for r in tcap.records if r.levelno == logging.WARNING
                  and "withheld" in r.getMessage()]
        check("a withheld directive is reported once per box and situation, not on every beat",
              len(warned) == 1, [r.getMessage() for r in tcap.records])

        # ...and again whenever its situation CHANGES, back to an earlier one
        # included: the newest WARNING about a box must say where it is now.
        E4B = "198.51.100.8:52007"
        where = {"endpoint": E4}

        async def desired_now(wg_pubkey):
            return {"endpoint": where["endpoint"], "endpoints": [E6, where["endpoint"]],
                    "endpoint_pubkey": BRIDGE}

        async def withheld_warnings(steps, box_ep=E6):
            """Beat from box_ep once per (bridge endpoint, Hub view) step; the
            WARNINGs said."""
            cap = Captured()
            lv, prop = tlog.level, tlog.propagate
            tlog.addHandler(cap)
            tlog.setLevel(logging.DEBUG)
            tlog.propagate = False
            topology._withheld_said.clear()
            try:
                for ep, view in steps:
                    where["endpoint"] = ep
                    hub_view(view)
                    await topology._rotate_directive(pool, beat(box_ep, version="0.13.9"))
            finally:
                tlog.removeHandler(cap)
                tlog.setLevel(lv)
                tlog.propagate = prop
            return [r.getMessage() for r in cap.records
                    if r.levelno == logging.WARNING and "withheld" in r.getMessage()]

        topology._desired_bastion_endpoint = desired_now
        on_legacy = {"public_key": own, "legacy_public_key": LEGACY,
                     "peers": [{"public_key": M1, "on": "legacy", "last_handshake": 1}]}
        retired = {"public_key": own, "legacy_public_key": None,
                   "peers": [{"public_key": M1, "on": "host", "last_handshake": 1}]}
        # The relay moves and moves back while the box waits for a client update.
        said = await withheld_warnings([(E4, on_legacy), (E4B, on_legacy), (E4, on_legacy)])
        check("a withheld box whose situation changes back to an earlier one is reported again",
              len(said) == 3 and E4B not in said[-1] and E4 in said[-1], said)
        # A box on the v4 entry: the bridge's endpoint comes to match it (nothing
        # withheld), then moves away again. A new episode, not the one already reported.
        said = await withheld_warnings([(E4B, retired), (E4, retired), (E4B, retired)],
                                       box_ep=E4)
        check("a withheld situation that resolved and then recurs is reported again",
              len(said) == 2, said)
        topology._desired_bastion_endpoint = _desired
        hub_view(on_legacy)

        hub_view({"public_key": own, "legacy_public_key": LEGACY,
                  "peers": [{"public_key": M1, "on": "host", "last_handshake": 1}]})
        d = await topology._rotate_directive(pool, beat(E6))
        check("moved (the Hub saw its handshake on ours) and on the preferred endpoint: left alone", d is None, d)
        d = await topology._rotate_directive(pool, beat(E4))
        check("moved but not on the preferred endpoint: the endpoint directive carries OUR key",
              d is not None and d["endpoint_pubkey"] == own, d)

        hub_view({"public_key": own, "legacy_public_key": None,
                  "peers": [{"public_key": M1, "on": "host", "last_handshake": 1}]})
        check("legacy retired, on the preferred endpoint: nothing to say",
              await topology._rotate_directive(pool, beat(E6)) is None)

        hub.get_status = 404   # an old Hub: no /identity
        hub_identity._status = (0.0, None)
        d = await topology._rotate_directive(pool, beat(E4))
        check("an old Hub: the endpoint directive keeps the bridge's key, as before",
              d is not None and d["endpoint_pubkey"] == BRIDGE, d)
        check("an old Hub, preferred endpoint: nothing (no key move without a Hub that holds ours)",
              await topology._rotate_directive(pool, beat(E6)) is None)
    finally:
        topology._desired_bastion_endpoint = saved
        hub_identity._status = (0.0, None)
        await hub.stop()
        hub_peers.HUB_CONTROL = ORIG_CONTROL


# ── 6. the reconcile loop's reports speak once ───────────────────────────────

async def _run_ticks(pool, n):
    """Run reconcile_hub_peers_loop itself for n ticks: the real converge_identity
    and the real arm_hub_pdp against the stub Hub, peers converge stubbed out."""
    from server import loop_splay
    from server.network import nac_stepup
    ticks = []

    async def fake_converge(pool_):
        ticks.append(1)
        if len(ticks) >= n:
            raise asyncio.CancelledError

    async def no_splay(*a, **k):
        return None

    async def verify_pub(pool_, eid):
        return bytes(range(32))

    saved = (hub_peers._converge_once, loop_splay.splay, nac_stepup.entity_verify_pubkey,
             hub_peers.RECONCILE_INTERVAL_S, os.environ.get("ENTITY_ID"))
    hub_peers._converge_once, loop_splay.splay = fake_converge, no_splay
    nac_stepup.entity_verify_pubkey = verify_pub
    hub_peers.RECONCILE_INTERVAL_S = 0
    os.environ["ENTITY_ID"] = "122"
    try:
        await hub_peers.reconcile_hub_peers_loop(pool)
    except asyncio.CancelledError:
        pass
    finally:
        (hub_peers._converge_once, loop_splay.splay, nac_stepup.entity_verify_pubkey,
         hub_peers.RECONCILE_INTERVAL_S, eid) = saved
        if eid is None:
            os.environ.pop("ENTITY_ID", None)
        else:
            os.environ["ENTITY_ID"] = eid
    return len(ticks)


async def part_loop_reports():
    print("== 6. the loop says each Hub-key condition once, not every 15 s")
    logs = capture_logs()
    cap = logs.__enter__()
    hub = await StubHub().start()
    try:
        for get_status, post_status, level, label in [
                (404, 404, logging.WARNING, "a Hub image that predates /identity (expected on every pod "
                                            "until the hub rolls)"),
                (200, 409, logging.ERROR, "a Hub holding a DIFFERENT key")]:
            reset_reports()
            hub.get_status, hub.post_status, hub.verifykey_status = get_status, post_status, 204
            hub.get_body = {"public_key": "SOMEONE-ELSES", "legacy_public_key": None, "peers": []}
            cap.records.clear()
            n = await _run_ticks(FakePool(), 4)
            said = [r for r in cap.records if r.levelno == level and "WireGuard key" in r.getMessage()]
            check(f"{label}, PDP armed fine: said once over {n} ticks", len(said) == 1,
                  [r.getMessage()[:50] for r in said])
    finally:
        logs.__exit__()
        await hub.stop()
        hub_peers.HUB_CONTROL = ORIG_CONTROL


# ── 7. the machine is never handed a key the Hub has just stopped being ──────

async def part_after_arm():
    print("== 7. right after the tick arms the Hub, the heartbeat hands out the Hub's new key")
    saved = topology._desired_bastion_endpoint
    topology._desired_bastion_endpoint = _desired
    hub = await StubHub().start()
    try:
        for legacy, label in [(LEGACY, "Hub restarted with the operator's key beside none of ours"),
                              (None, "Hub restarted keyless (operator Secret already deleted)")]:
            pool = FakePool()
            own = await hub_identity.hub_wg_pubkey(pool)
            hub.posts.clear()
            hub.get_status, hub.post_status = 200, 204
            hub.get_body = {"public_key": None, "legacy_public_key": legacy,
                            "peers": [{"public_key": M1, "on": None, "last_handshake": 0}]}
            await hub_identity.converge_identity(pool)
            # M1 moved to our key before the restart; it handshakes on ours the moment
            # the Hub holds it, and beats from the v4 entry (an IPv4-only network).
            d = await topology._rotate_directive(pool, _beat(E4))
            check(f"{label}: a beat in the same tick is told OUR key, never "
                  f"{'the operator key it would probe and fall back onto' if legacy else 'the bridge key nothing answers'}",
                  hub.posts and d is not None and d["endpoint_pubkey"] == own,
                  None if d is None else ("LEGACY" if d["endpoint_pubkey"] == LEGACY else d["endpoint_pubkey"][:8]))
    finally:
        topology._desired_bastion_endpoint = saved
        hub_identity._status = (0.0, None)
        await hub.stop()
        hub_peers.HUB_CONTROL = ORIG_CONTROL


# ── 8. a Hub we cannot hear is not an old Hub ────────────────────────────────

async def part_unreachable():
    print("== 8. a Hub that does not answer is never treated as one that serves the bridge's key")
    saved = topology._desired_bastion_endpoint
    topology._desired_bastion_endpoint = _desired
    try:
        for how in ("unreachable", "GET 500"):
            hub = None
            if how == "unreachable":
                hub_peers.HUB_CONTROL = "http://127.0.0.1:1"
            else:
                hub = await StubHub().start()
                hub.get_status = 500
            try:
                hub_identity._status = (0.0, None)
                pool, rearmed = FakePool(), []
                try:
                    out = await _redeem(pool, BRIDGE, rearmed)
                    check(f"pairing, Hub {how}: refused, never pinned to the bridge's key", False,
                          "paired on " + ("BRIDGE" if out["endpoint_pubkey"] == BRIDGE
                                          else str(out["endpoint_pubkey"])[:8]))
                except HTTPException as e:
                    check(f"pairing, Hub {how}: refused (503) and the token given back",
                          e.status_code == 503 and len(rearmed) == 1, (e.status_code, rearmed))
                hub_identity._status = (0.0, None)
                d = await topology._rotate_directive(pool, _beat(E4))
                check(f"heartbeat, Hub {how}: no directive this beat (the bridge's key may be one "
                      "the Hub retired)", d is None, d)
            finally:
                if hub is not None:
                    await hub.stop()
                hub_peers.HUB_CONTROL = ORIG_CONTROL
    finally:
        topology._desired_bastion_endpoint = saved
        hub_identity._status = (0.0, None)


# ── 9. a key move rests on what the Hub holds NOW ────────────────────────────

async def part_restarted():
    print("== 9. a Hub that restarted since the tick is not asked to take a machine on our key")
    saved = topology._desired_bastion_endpoint
    topology._desired_bastion_endpoint = _desired
    hub = await StubHub().start()
    try:
        pool = FakePool()
        own = await hub_identity.hub_wg_pubkey(pool)
        # The tick's copy: armed with ours, M1 still on the operator's key.
        hub_identity._status = (time.monotonic(), {
            "public_key": own, "legacy_public_key": LEGACY,
            "peers": [{"public_key": M1, "on": "legacy", "last_handshake": 1}]})
        # The Hub sidecar restarted since: the operator's key is back, ours is gone
        # until the next tick re-arms it.
        hub.get_status = 200
        hub.get_body = {"public_key": None, "legacy_public_key": LEGACY,
                        "peers": [{"public_key": M1, "on": "legacy", "last_handshake": 2}]}
        d = await topology._rotate_directive(pool, _beat(E6))
        check("M1 on the preferred entry: not told to move to a key the Hub does not hold now "
              "(its probe would cost the live tunnel 12 s and mark the entry failed)", d is None, d)
        gets = hub.gets
        hub.get_body = {"public_key": own, "legacy_public_key": LEGACY,
                        "peers": [{"public_key": M1, "on": "host", "last_handshake": 3}]}
        hub_identity._status = (time.monotonic(), hub.get_body)
        await topology._rotate_directive(pool, _beat(E6))
        check("...and a machine with nothing to move costs the Hub no extra read", hub.gets == gets,
              hub.gets - gets)
    finally:
        topology._desired_bastion_endpoint = saved
        hub_identity._status = (0.0, None)
        await hub.stop()
        hub_peers.HUB_CONTROL = ORIG_CONTROL


async def main():
    await part_key()
    await part_api()
    part_retire()
    await part_converge()
    await part_move_unsaved()
    await part_redeem()
    await part_directive()
    await part_loop_reports()
    await part_after_arm()
    await part_unreachable()
    await part_restarted()


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    asyncio.run(main())
    if FAILED:
        print(f"\nFAIL: {len(FAILED)} hub host-identity check(s) failed")
        sys.exit(1)
    print("\nOK: hub host-identity gates pass")
