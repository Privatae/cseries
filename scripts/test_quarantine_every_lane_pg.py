#!/usr/bin/env python3
"""A quarantined machine gets no lane to this Host; a released one gets them back
(server/network/quarantine_gate.py).

Drives the real lane endpoints and routes on a fresh clone of the kernel's schema,
with one paired bastion (an invented machine) and its own bearer:

  G  the check: by token, by the server the row is keyed on, and FAILING CLOSED
     (no pool, a read that raises); the job gate's quarantine set fails closed too
  W  each worker socket (/bastion-egress, -mgmt, -pgwire, -harness, -embed, -git, -serve)
     is closed 4403 at the door while held, and admitted once released
  C  quarantine_bastion cuts every socket the machine already holds open, at once
  H  the HTTP routes (git-credential, context-source, dcn-propose) answer 403 while
     held and pass the gate once released
  B  the heartbeat while held is ACCEPTED BUT RECORDED ONLY: last_seen_at moves,
     nothing it reports is written, the reply grants nothing; released, a beat is
     a beat again
  M  /mcp refuses the held machine even when the network rules admit it, with or
     without a bound fingerprint; released, it is admitted
  L  the lane port (local_bastion, :8811): quarantine closes the connections the
     machine holds there; another machine's stay open

Run:  python3 scripts/test_quarantine_every_lane_pg.py
      (TEST_PG_DSN / PEER_TEST_DSN / TEST_DSN names a server; without one it
      starts a throwaway one through docker, as the other _pg gates do.)
"""
import asyncio
import contextlib
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import types

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain"), os.path.join(_ROOT, "scripts")]
_TMP = tempfile.TemporaryDirectory(prefix="quarantine-lanes-")
os.environ.setdefault("TM_JOBS_ROOT", os.path.join(_TMP.name, "tm-jobs"))

import asyncpg  # noqa: E402

SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    print("SKIP-LOUD: no test server (TEST_PG_DSN/PEER_TEST_DSN/TEST_DSN) and docker unavailable — "
          "this gate needs a real database for the quarantine table and the bearer rows.")
    sys.exit(2)

from kernel_schema import kernel_schema_db, throwaway_server  # noqa: E402

ENTITY = 1
TOKEN = 9101
HOST = "invented-desk"
BEARER = "mcp_" + "q" * 40
OTHER_TOKEN = 9102                    # a second machine, never quarantined
OTHER_BEARER = "mcp_" + "o" * 40

FAILS: list = []
CHECKS = 0


def check(cond, msg, extra=None):
    global CHECKS
    CHECKS += 1
    print(("  PASS  " if cond else "  FAIL  ") + msg + (("\n        " + str(extra)[:500]) if extra and not cond else ""))
    if not cond:
        FAILS.append(msg)


def load_main():
    """server.main imported without the container's dirs (as test_api_route_guards does)."""
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
    return m


class FakeWS:
    """A worker socket as the front door hands it over: the operator cert's CN on a
    loopback peer, the bearer as the first frame. `hold` keeps receive() pending until
    the socket is closed (a live idle worker); otherwise the bastion hangs up at once."""

    def __init__(self, bearer=BEARER, *, hold=False, lane=None):
        self.headers = {"x-client-cert-cn": "operator"}
        self.client = types.SimpleNamespace(host="::1")
        self.hello = "BEARER " + bearer + (f" lane={lane}" if lane else "")
        self.hold = hold
        self.closed: list = []
        self._gone = asyncio.Event()

    async def accept(self):
        pass

    async def receive_text(self):
        return self.hello

    async def receive(self):
        if self.hold:
            await self._gone.wait()
        return {"type": "websocket.disconnect", "code": 1000}

    async def receive_bytes(self):
        await self._gone.wait()
        raise RuntimeError("closed")

    async def send_bytes(self, b):
        pass

    async def send_text(self, t):
        pass

    async def close(self, code=1000):
        self.closed.append(code)
        self._gone.set()


class BrokenPool:
    async def fetchval(self, *a, **k):
        raise asyncpg.exceptions.UndefinedTableError("relation does not exist")

    async def fetch(self, *a, **k):
        raise asyncpg.exceptions.UndefinedTableError("relation does not exist")


def request(body: dict | None = None, path="/api/network/bastions/heartbeat"):
    from starlette.requests import Request
    raw = json.dumps(body or {}).encode()
    sent = {"done": False}

    async def receive():
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": raw, "more_body": False}
    scope = {"type": "http", "method": "POST", "path": path, "query_string": b"",
             "headers": [(b"content-type", b"application/json"),
                         (b"content-length", str(len(raw)).encode())]}
    return Request(scope, receive)


async def status_of(coro):
    from fastapi import HTTPException
    try:
        r = await coro
        return getattr(r, "status_code", 200)
    except HTTPException as e:
        return e.status_code


async def main(dsn: str) -> int:
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    from server import brain_service
    brain_service._pool = pool
    brain_service._entity_id = ENTITY
    m = load_main()
    from server.network import (quarantine_gate as QG, nac_mgmt, nac_commit, nac_stepup,
                                mirror, pgwire_relay, git_relay, topology)
    from server.routes import network as net, gitea
    from server.routes.mcp import _nac_gate
    from server.synaptive_handler import _validate_bearer
    # A refused socket is held REFUSED_HOLD_S before it closes (section T tests
    # the hold); the lane checks here run with no hold, so they take no time.
    QG.REFUSED_HOLD_S = 0

    try:
        for tid, bearer, host in ((TOKEN, BEARER, HOST), (OTHER_TOKEN, OTHER_BEARER, "invented-nas")):
            await pool.execute(
                "INSERT INTO mcp_client_tokens (id, entity_id, name, token_hash, url_handle, operator_kind) "
                "VALUES ($1, $2, $3, $4, $3, 'bastion')",
                tid, ENTITY, f"q-{tid}", hashlib.sha256(bearer.encode()).digest())
            await topology.upsert_heartbeat(pool, ENTITY, {
                "token_id": tid, "hostname": host, "platform": "linux", "gateway_version": "0.58.1"})

        async def hold():
            return await nac_commit.quarantine_bastion(pool, ENTITY, HOST, None, "test")

        async def release():
            orig = nac_stepup.verify_stepup

            async def _ok(_pool, _code):
                return True
            nac_stepup.verify_stepup = _ok
            try:
                return await nac_commit.unquarantine_bastion(pool, ENTITY, HOST, None, "000000", "test")
            finally:
                nac_stepup.verify_stepup = orig

        # ── T a held machine's redials are slowed and logged once a minute ──────
        print("T the tarpit")
        import logging as _logging
        import time as _time
        records = []

        class _Grab(_logging.Handler):
            def emit(self, r):
                records.append(r.getMessage())
        grab = _Grab()
        QG.log.addHandler(grab)
        QG._refusal_log.clear()
        await hold()
        ran = []

        async def _body():
            ran.append(1)
        QG.REFUSED_HOLD_S = 0.3
        t0 = _time.monotonic()
        refused = await QG.run_lane(pool, TOKEN, "egress", _body)
        held_for = _time.monotonic() - t0
        check(refused is False and not ran, "T a held machine's socket is refused and nothing runs on it")
        check(held_for >= 0.3, "T the refused socket is held before it closes", held_for)
        check(QG.open_lanes(TOKEN) == [], "T a held socket is not left registered after it closes")
        QG.REFUSED_HOLD_S = 0
        for _ in range(5):
            await QG.run_lane(pool, TOKEN, "egress", _body)
        lines = [m for m in records if "egress lane REFUSED" in m]
        check(len(lines) == 1, "T six refusals on one lane within a minute write one line", lines)
        await QG.run_lane(pool, TOKEN, "mgmt", _body)
        check(len([m for m in records if "mgmt lane REFUSED" in m]) == 1,
              "T another lane of the same machine gets its own line")
        QG._refusal_log[(TOKEN, "egress")][0] -= QG.REFUSAL_LOG_EVERY_S + 1
        await QG.run_lane(pool, TOKEN, "egress", _body)
        lines = [m for m in records if "egress lane REFUSED" in m]
        check(len(lines) == 2 and "(5 more refused in the last minute)" in lines[-1],
              "T a minute on, the next line says how many were not written", lines)
        QG.log.removeHandler(grab)
        await release()

        # ── G the check ─────────────────────────────────────────────────────────
        print("G the check")
        check(await QG.is_quarantined(pool, TOKEN) is False, "G a machine never quarantined is not held")
        await hold()
        check(await QG.is_quarantined(pool, TOKEN) is True, "G quarantined by the Network app: held")
        check(await QG.is_quarantined(pool, OTHER_TOKEN) is False, "G another machine is not held")
        await release()
        check(await QG.is_quarantined(pool, TOKEN) is False, "G released: not held")
        await pool.execute("INSERT INTO nac_bastion_quarantine (server, token_id) VALUES ($1, NULL)", HOST)
        check(await QG.is_quarantined(pool, TOKEN) is True,
              "G a row keyed by server alone (the v069 key) holds the token that reported that name")
        await pool.execute("DELETE FROM nac_bastion_quarantine WHERE server = $1", HOST)
        check(await QG.is_quarantined(None, TOKEN) is True, "G no pool: held (fails closed)")
        check(await QG.is_quarantined(BrokenPool(), TOKEN) is True, "G a read that raises: held (fails closed)")
        q = await nac_mgmt.load_quarantined(BrokenPool())
        check(bool(q) and HOST in q and "anything" in q,
              "G the job gate's quarantine set, unreadable, holds every server (was: empty)")
        ok, why, _d = nac_mgmt._decide([{"server": "*", "ops": ["*"]}], set(), q, ENTITY, HOST, "uptime")
        check(not ok and "quarantined" in why, "G ...so a management job is refused, not run", (ok, why))
        check(await QG.run_lane(BrokenPool(), TOKEN, "egress", asyncio.sleep) is False,
              "G a worker socket is refused when the state cannot be read")

        # ── W each worker socket at its door ───────────────────────────────────
        print("W each worker socket at its door")
        relayed = {"pgwire": 0, "git": 0}
        pg_gone = asyncio.Event()

        async def _cfg(_pool, _eid):
            return {"enabled": True, "token_id": TOKEN, "bastion_name": HOST}

        async def _dbname(_pool):
            return "invented"

        async def _relay(ws, *a, **k):
            relayed["pgwire"] += 1
            if ws.hold:
                await pg_gone.wait()

        async def _git_serve(self):
            relayed["git"] += 1
            if self.ws.hold:
                await self.ws._gone.wait()
        mirror.get_config, mirror.db_name, pgwire_relay.relay = _cfg, _dbname, _relay
        git_relay.GitWorker.serve = _git_serve
        m._nac_egress_ref = object()

        lanes = {
            "egress": m.bastion_egress_relay, "mgmt": m.bastion_mgmt_relay,
            "pgwire": m.bastion_pgwire_relay, "harness": m.bastion_harness_relay,
            "embed": m.bastion_embed_relay, "git": m.bastion_git_relay,
            "serve": m.bastion_serve_relay,
        }
        await hold()
        for name, ep in lanes.items():
            ws = FakeWS()
            await asyncio.wait_for(ep(ws), 10)
            check(QG.CLOSE_CODE in ws.closed, f"W /bastion-{name}: quarantined, closed 4403", ws.closed)
        check(relayed == {"pgwire": 0, "git": 0},
              "W ...and the mirror stream and the git splice never started", relayed)
        sess = FakeWS(lane="session")
        await asyncio.wait_for(m.bastion_harness_relay(sess), 10)
        check(QG.CLOSE_CODE in sess.closed, "W /bastion-harness lane=session (terminal): closed 4403", sess.closed)
        await release()
        for name, ep in lanes.items():
            ws = FakeWS()
            await asyncio.wait_for(ep(ws), 10)
            check(QG.CLOSE_CODE not in ws.closed, f"W /bastion-{name}: released, admitted", ws.closed)
        check(relayed == {"pgwire": 1, "git": 1},
              "W ...the mirror stream and the git splice ran once each", relayed)

        # ── C quarantine cuts what is already open ──────────────────────────────
        print("C quarantine cuts what is already open")
        open_ws = {name: FakeWS(hold=True) for name in lanes}
        other = FakeWS(OTHER_BEARER, hold=True)
        tasks = {name: asyncio.ensure_future(ep(open_ws[name])) for name, ep in lanes.items()}
        other_t = asyncio.ensure_future(m.bastion_egress_relay(other))
        for _ in range(200):
            if len(QG.open_lanes(TOKEN)) == len(lanes) and QG.open_lanes(OTHER_TOKEN):
                break
            await asyncio.sleep(0.01)
        check(len(QG.open_lanes(TOKEN)) == len(lanes), "C every lane open on the released machine",
              QG.open_lanes(TOKEN))
        r = await hold()
        pg_gone.set()
        done, pending = await asyncio.wait(tasks.values(), timeout=10)
        check(not pending, "C every open lane ended once the machine was quarantined",
              [n for n, t in tasks.items() if t in pending])
        check(all(QG.CLOSE_CODE in open_ws[n].closed for n in lanes),
              "C ...each closed 4403", {n: open_ws[n].closed for n in lanes})
        check(r.get("lanes_cut") == len(lanes), "C quarantine_bastion reports the lanes it cut", r)
        check(QG.open_lanes(TOKEN) == [], "C the machine holds no lane", QG.open_lanes(TOKEN))
        check(not other_t.done() and QG.open_lanes(OTHER_TOKEN) == ["egress"],
              "C another machine's lane stays open")
        await other.close()
        await asyncio.wait_for(other_t, 10)

        # ── H the HTTP routes ───────────────────────────────────────────────────
        print("H the HTTP routes")
        auth = "Bearer " + BEARER

        async def _tok():
            return "invented-token"
        gitea._user_bearer_fresh = _tok
        gitea.user_login = lambda: "invented"
        routes = {
            "git-credential": lambda: net.bastion_git_credential(authorization=auth),
            "context-source": lambda: net.bastion_context_source(ticket="0" * 64, authorization=auth),
            "dcn-propose": lambda: net.bastion_dcn_propose(
                request({"root": "/srv/invented", "display_name": "x"}, "/api/network/bastions/dcn-propose"),
                authorization=auth),
        }
        # still quarantined from C
        for name, call in routes.items():
            s = await status_of(call())
            check(s == 403, f"H {name}: quarantined, 403", s)
        await release()
        for name, call in routes.items():
            s = await status_of(call())
            check(s != 403, f"H {name}: released, past the gate", s)

        # ── B the heartbeat ─────────────────────────────────────────────────────
        print("B the heartbeat")
        await hold()
        await pool.execute("UPDATE network_bastion_heartbeat SET last_seen_at = now() - interval '1 hour' "
                           "WHERE token_id = $1", TOKEN)
        before = await pool.fetchrow("SELECT * FROM network_bastion_heartbeat WHERE token_id=$1", TOKEN)
        exp_before = await pool.fetchval("SELECT expires_at FROM mcp_client_tokens WHERE id=$1", TOKEN)
        reply = await net.bastion_heartbeat(
            request({"hostname": "renamed-by-the-machine", "gateway_version": "9.9.9",
                     "models": ["invented-model"], "agents": [{"pid": 1, "signature": "x"}]}),
            authorization=auth)
        after = await pool.fetchrow("SELECT * FROM network_bastion_heartbeat WHERE token_id=$1", TOKEN)
        check(reply.get("quarantined") is True and reply.get("ok") is True,
              "B held: the beat is accepted and the reply says quarantined", reply)
        check(set(reply) == {"token_id", "last_seen_at", "ok", "quarantined"},
              "B ...and carries no directive, grant, update, mirror or approval", sorted(reply))
        check(after["last_seen_at"] > before["last_seen_at"], "B ...last_seen_at moves (the owner sees it alive)")
        check((after["hostname"], after["gateway_version"], after["models"]) ==
              (before["hostname"], before["gateway_version"], before["models"]),
              "B ...nothing the machine reported is written", dict(after))
        n_agents = await pool.fetchval("SELECT count(*) FROM nac_agent_activity WHERE token_id=$1", TOKEN)
        check(n_agents == 0, "B ...no agents ingested", n_agents)
        check(await pool.fetchval("SELECT expires_at FROM mcp_client_tokens WHERE id=$1", TOKEN) == exp_before,
              "B ...and the bearer's expiry is not slid")
        await release()
        reply = await net.bastion_heartbeat(
            request({"hostname": HOST, "gateway_version": "0.58.2"}), authorization=auth)
        gv = await pool.fetchval("SELECT gateway_version FROM network_bastion_heartbeat WHERE token_id=$1", TOKEN)
        check("quarantined" not in reply and reply.get("ok") is True and gv == "0.58.2",
              "B released: a beat is recorded in full again", (reply, gv))

        # ── M /mcp ──────────────────────────────────────────────────────────────
        print("M /mcp")

        class _Admit:
            async def evaluate(self, token_id):
                return {"allowed": True, "why": "admitted"}
        a = await _validate_bearer(pool, BEARER)
        a_fp = dict(a, hardware_fingerprint="invented-fp")
        await hold()
        check(await _nac_gate(a, nac=_Admit()) is not None,
              "M held: a bastion bearer with no bound fingerprint is refused")
        check(await _nac_gate(a_fp, nac=_Admit()) is not None,
              "M held: refused even when the network rules admit the machine")
        await release()
        check(await _nac_gate(a, nac=_Admit()) is None, "M released: admitted (no fingerprint)")
        check(await _nac_gate(a_fp, nac=_Admit()) is None, "M released: admitted (bound fingerprint)")

        # ── L the lane port ─────────────────────────────────────────────────────
        print("L the lane port")
        from server.network import local_bastion as LB

        class _Writer:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True
        mine, theirs = _Writer(), _Writer()
        LB._lane_conns.setdefault(TOKEN, set()).add(mine)
        LB._lane_conns.setdefault(OTHER_TOKEN, set()).add(theirs)
        await hold()
        check(mine.closed, "L quarantine closes the lane-port connection the machine holds")
        check(not theirs.closed, "L ...another machine's stays open")
        LB._lane_conns.pop(OTHER_TOKEN, None)
        await release()
    finally:
        brain_service._pool = None
        await pool.close()
    return 0


with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, \
        kernel_schema_db(_srv, entity_id=ENTITY) as _dsn:
    rc = asyncio.run(main(_dsn))
print()
if FAILS:
    print(f"FAILED — {len(FAILS)} of {CHECKS}")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
if CHECKS == 0:
    print("FAILED — no checks ran; a gate that asserts nothing is not a pass")
    sys.exit(1)
print(f"quarantine, every lane: {CHECKS}/{CHECKS} checks passed")
sys.exit(rc)
