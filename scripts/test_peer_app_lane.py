#!/usr/bin/env python3
"""The peer lane carries apps.

What is pinned here, and why each one:

  1. app.deliver's slip ACTION comes from its own params (app:<app_id>), so a
     slip for app X presented for app Y is refused by the server, not the
     handler. Malformed params yield no action, and no action is an unknown
     op — never "no slip needed".
  2. app.grant is gated on the MESSAGE slip: only an accepted contact can hand
     us an app slip, and the carried slip must name (them -> us) or it is not
     stored — a peer cannot park a third host's slip under its own id.
  3. The receiver refuses an app that is not installed or never declared
     read:peer_app, so rows cannot land where nothing reads them; and a
     retried message_id is a duplicate, not a second row.
  4. The outbox classifier: a transport fault is retried with backoff, a
     receiver's no and a slip refusal are final, ok is delivered.
  5. The scopes exist, community apps may declare them, the handlers are
     registered, and the migration declares the three tables.

Container-free: handlers driven directly with a stubbed pool and registry.

Run:  python3 scripts/test_peer_app_lane.py
"""
import asyncio
import os
import sys
import uuid

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain")]

FAILS = []
CHECKS = 0


def check(cond, msg, extra=""):
    global CHECKS
    CHECKS += 1
    print(("  PASS  " if cond else "  FAIL  ") + msg + (("\n        " + str(extra)) if extra else ""))
    if not cond:
        FAILS.append(msg)


class StubPool:
    """Answers the inbox INSERT (returning an id once per message_id), the
    slip upsert, and records every statement."""

    def __init__(self):
        self.seen_ids = set()
        self.execs = []
        self.rows = []

    async def fetchval(self, sql, *args):
        self.execs.append((sql, args))
        if "INSERT INTO peer_app_inbox" in sql:
            mid = args[4]
            if mid in self.seen_ids:
                return None
            self.seen_ids.add(mid)
            self.rows.append(args)
            return len(self.rows)
        return 0

    async def execute(self, sql, *args):
        self.execs.append((sql, args))
        return "OK"

    async def fetchrow(self, sql, *args):
        self.execs.append((sql, args))
        return None


class FakeApp:
    def __init__(self, permissions, status="active"):
        self.permissions = permissions
        self.status = status


async def main() -> int:
    from server.peer import app_lane as L
    from server.peer import peer_server as PS
    from server.peer import handlers as H
    from server import brain_service

    brain_service._entity_id = 3
    pool = StubPool()
    brain_service._pool = pool

    # ── 1. the deliver action is derived from params ────────────────────────
    check(L.action_for_deliver({"app_id": "veterans"}) == "app:veterans",
          "app.deliver's action is app:<app_id> from its own params")
    check(L.action_for_deliver({"app_id": "Bad App!"}) is None, "a malformed app_id yields no action")
    check(L.action_for_deliver({}) is None, "missing app_id yields no action")
    check("app.deliver" in PS._OP_ACTION_FROM_PARAMS, "peer_server derives app.deliver's action per request")
    check(PS._OP_ACTION.get("app.grant") == "message", "app.grant rides on the message slip")
    check("app.deliver" not in PS._SLIPLESS_OPS and "app.grant" not in PS._SLIPLESS_OPS,
          "neither app op is slipless")

    # The server path itself: an app.deliver with no valid app must be refused as
    # unknown, never dispatched. Driven through _serve with a handler table.
    class Trust:
        ready = True
        keys = {}
        revoked = set()

        async def ensure_key(self, k):
            return False

    called = []

    async def h(peer_id, slip_info, params):
        called.append(params)
        return {"ok": True}

    srv = PS.PeerServer(trust=Trust(), handlers={"app.deliver": h}, my_entity_id=3)
    r = await srv._serve(122, {"op": "app.deliver", "slip": {"x": 1}, "params": {"app_id": "no way"}})
    check(not r.get("ok") and "unknown op" in (r.get("error") or ""),
          "server refuses app.deliver whose params name no valid app, as an unknown op", r)
    check(not called, "and never reaches the handler")

    # The installed-app registry both receiver ops consult, stubbed once.
    from server.applications import registry as R
    apps = {"veterans": FakeApp(["read:peer_app", "write:peer_app"]),
            "mute": FakeApp(["read:calendar"]),
            "gone": FakeApp(["read:peer_app"], status="uninstalled")}

    async def fake_get(pool, app_id):
        return apps.get(app_id)

    R.get = fake_get

    # ── 2. app.grant stores only a slip naming (them -> us) ─────────────────
    def slip(issuer=122, grantee=3, ops=("app:veterans",)):
        return {"contract_id": "c-app-1",
                "terms": {"issuer": issuer, "grantee": grantee, "allowed_ops": list(ops),
                          "expires_at": "2026-12-01T00:00:00+00:00", "contract_id": "c-app-1"}}

    pool.execs.clear()
    r = await L.app_grant(122, {"allowed_ops": ["message"]}, {"app_id": "veterans", "app_slip": slip()})
    check(r.get("ok") and r.get("stored"), "a well-formed app slip from the presenting peer is stored", r)
    check(any("INSERT INTO peer_app_slips" in s for s, _ in pool.execs), "into peer_app_slips")
    stored_args = [a for s, a in pool.execs if "INSERT INTO peer_app_slips" in s][0]
    check(stored_args[0] == 122 and stored_args[1] == "veterans",
          "keyed by the CERT's peer id and the app id", stored_args[:2])

    r = await L.app_grant(122, {}, {"app_id": "veterans", "app_slip": slip(issuer=999)})
    check(not r.get("ok"), "a slip issued by a THIRD host is refused — cannot be parked under this peer", r)
    r = await L.app_grant(122, {}, {"app_id": "veterans", "app_slip": slip(grantee=7)})
    check(not r.get("ok"), "a slip naming another grantee is refused", r)
    r = await L.app_grant(122, {}, {"app_id": "veterans", "app_slip": slip(ops=("app:other",))})
    check(not r.get("ok"), "a slip whose allowed_ops do not cover this app is refused", r)
    r = await L.app_grant(122, {}, {"app_id": "veterans", "app_slip": "nope"})
    check(not r.get("ok"), "a non-object slip is refused", r)
    r = await L.app_grant(122, {}, {"app_id": "../x", "app_slip": slip()})
    check(not r.get("ok"), "a malformed app_id is refused", r)

    # ── 3. app.deliver: installed + declared, idempotent ────────────────────
    async def no_live(*a, **k):
        return None

    L._hand_to_live_process = no_live
    info = {"allowed_ops": ["app:veterans"]}
    mid = str(uuid.uuid4())
    params = {"app_id": "veterans", "action": "probe", "payload": {"c": ["abc"]}, "message_id": mid}
    r = await L.app_deliver(122, info, params)
    check(r.get("ok") and r.get("stored") and r.get("duplicate") is False,
          "a delivery for an installed, declared app is stored", r)
    check(pool.rows and pool.rows[-1][1] == 122,
          "peer_entity_id on the row is the CERT identity, never a param", pool.rows[-1][:3] if pool.rows else None)
    r2 = await L.app_deliver(122, info, params)
    check(r2.get("ok") and r2.get("duplicate") is True, "the same message_id again is a duplicate, not a row", r2)
    check(len(pool.rows) == 1, "exactly one row stored for two deliveries of one id")

    r = await L.app_deliver(122, {"allowed_ops": ["app:mute"]},
                            {**params, "app_id": "mute", "message_id": str(uuid.uuid4())})
    check(not r.get("ok") and "peer traffic" in (r.get("error") or ""),
          "an installed app that never declared read:peer_app is refused", r)
    r = await L.app_deliver(122, {"allowed_ops": ["app:gone"]},
                            {**params, "app_id": "gone", "message_id": str(uuid.uuid4())})
    check(not r.get("ok") and "not installed" in (r.get("error") or ""), "an uninstalled app is refused", r)
    r = await L.app_deliver(122, {"allowed_ops": ["app:nothere"]},
                            {**params, "app_id": "nothere", "message_id": str(uuid.uuid4())})
    check(not r.get("ok") and "not installed" in (r.get("error") or ""), "an unknown app is refused", r)
    r = await L.app_deliver(122, {"allowed_ops": ["app:other"]},
                            {**params, "message_id": str(uuid.uuid4())})
    check(not r.get("ok"), "a slip whose ops do not cover the named app is refused by the handler too", r)
    r = await L.app_deliver(122, info, {**params, "message_id": "not-a-uuid"})
    check(not r.get("ok"), "message_id must be a UUID", r)
    r = await L.app_deliver(122, info, {**params, "action": "bad action!", "message_id": str(uuid.uuid4())})
    check(not r.get("ok"), "a malformed action is refused", r)
    r = await L.app_deliver(122, info, {**params, "payload": {"big": "x" * (L.MAX_PAYLOAD_BYTES + 1)},
                                        "message_id": str(uuid.uuid4())})
    check(not r.get("ok"), "an oversized payload is refused", r)

    # ── 3b. app.grant is bounded to apps installed here that accept peers ───
    r = await L.app_grant(122, {}, {"app_id": "mute", "app_slip": slip(ops=("app:mute",))})
    check(not r.get("ok") and "peer traffic" in (r.get("error") or ""),
          "a slip for an installed app that never declared read:peer_app is not stored", r)
    r = await L.app_grant(122, {}, {"app_id": "nothere", "app_slip": slip(ops=("app:nothere",))})
    check(not r.get("ok") and "not installed" in (r.get("error") or ""),
          "a slip for an app not installed here is not stored (no unbounded rows per app_id string)", r)

    # ── 4. the outbox classifier ────────────────────────────────────────────
    from server.peer.peer_client import PeerUnavailable
    check(L.classify(None, PeerUnavailable("down"))[0] == "queued",
          "a transport fault stays queued (retried)")
    check(L.classify({"ok": False, "receiver": True, "error": "app 'x' is not installed on this host"}, None)[0] == "refused",
          "a receiver's own no about this message is final")
    check(L.classify({"ok": False, "error": "slip refused: expired"}, None)[0] == "refused",
          "a slip refusal at the lane is final")
    check(L.classify({"ok": True, "data": {}}, None)[0] == "delivered", "ok is delivered")
    # None of these is the peer's answer about THIS message.
    for err in ("this host is not ready", "could not store the delivery", "could not check the app",
                "handler error", "slip refused: revocation view not fresh (CRL unsynced)"):
        s, why = L.classify({"ok": False, "receiver": True, "error": err}, None)
        check(s == "queued", f"a receiver transient is retried, never final: {err!r}", (s, why))
    check(L.classify({"ok": False, "error": "unknown op 'app.deliver'"}, None)[0] == "queued",
          "an older image that lacks the op is retried (it may upgrade), not final")
    check(L.classify(None, L.LocalFailure("no slip on file"))[0] == "queued",
          "this side's own 'no slip on file' is retried, never filed as the peer's no")
    check(L.classify(None, L.FinalLocal("frame exceeds the lane's ceiling"))[0] == "refused",
          "a frame that can never be read is final — and says so")
    check(L._backoff_s(1) == L.RETRY_BASE_S and L._backoff_s(2) == 2 * L.RETRY_BASE_S
          and L._backoff_s(50) == L.RETRY_MAX_S, "backoff doubles from the base and caps")

    # ── 4b. _send / attempt driven end to end with stubs ────────────────────
    # The decision lives in _send's exceptions + classify; pin them together so
    # a change to either cannot silently turn a retry into a permanent drop.
    class RecPool(StubPool):
        def __init__(self, slip_row=None):
            super().__init__()
            self.slip_row = slip_row
            self.updates = []

        async def fetchrow(self, sql, *args):
            self.execs.append((sql, args))
            if "FROM peer_app_slips" in sql:
                return self.slip_row
            return None

        async def execute(self, sql, *args):
            self.updates.append((sql, args))
            return "OK"

    from server.peer import peer_client as PC
    wire = []

    async def fake_send(peer, op, slip_, params, **kw):
        wire.append((peer, op, params))
        return {"ok": True, "data": {"stored": True}}

    PC.send_to_peer = fake_send
    live_slip = {"contract_id": "c1", "terms": {"expires_at": "2099-01-01T00:00:00+00:00",
                                                "issued_at": "2099-01-01T00:00:00+00:00"}}
    base_row = {"id": 7, "app_id": "veterans", "peer_entity_id": 122, "op": L.OP_DELIVER,
                "action": "probe", "payload": {"k": "v"}, "message_id": uuid.uuid4(), "attempts": 0}

    # no slip on file -> queued with backoff, nothing on the wire
    p = RecPool(slip_row=None)
    brain_service._pool = p
    out = await L.attempt(p, dict(base_row))
    check(out["status"] == "queued" and "local_unavailable" in out["reason"],
          "attempt with no app slip on file is QUEUED (retried), not refused", out)
    check(not wire, "and nothing was sent")
    check(any("next_attempt_at" in s for s, _ in p.updates), "and a next attempt is scheduled")

    # slip on file -> sent, delivered
    p = RecPool(slip_row={"slip": live_slip, "contract_id": "c1"})
    brain_service._pool = p
    wire.clear()
    out = await L.attempt(p, dict(base_row))
    check(out["status"] == "delivered" and wire and wire[0][1] == L.OP_DELIVER,
          "with a live slip the row is sent and marked delivered", out)
    check(wire[0][2]["message_id"] == str(base_row["message_id"]), "the row's message_id rides on the wire")

    # oversize frame -> refused locally, nothing on the wire, reason says so
    wire.clear()
    big = dict(base_row, payload={"blob": "x" * (PC.MAX_FRAME_BYTES)})
    out = await L.attempt(p, big)
    check(out["status"] == "refused" and out["reason"].startswith("local:"),
          "a frame past the lane ceiling is refused LOCALLY with a local: reason", out)
    check(not wire, "and never put on the wire")

    # peer answers 'not ready' -> queued
    async def not_ready(peer, op, slip_, params, **kw):
        return {"ok": False, "receiver": True, "error": "this host is not ready"}
    PC.send_to_peer = not_ready
    out = await L.attempt(p, dict(base_row))
    check(out["status"] == "queued", "a peer that answers 'not ready' is retried", out)

    # peer answers not installed -> refused
    async def not_installed(peer, op, slip_, params, **kw):
        return {"ok": False, "receiver": True, "error": "app 'veterans' is not installed on this host"}
    PC.send_to_peer = not_installed
    out = await L.attempt(p, dict(base_row))
    check(out["status"] == "refused", "a peer that says the app is not installed is final", out)

    # ── 4c. the wire encoding is the one the size check measures ────────────
    from server.peer import peer_server as PS2
    check(PC.MAX_FRAME_BYTES == PS2._MAX_REQ, "the sender's frame ceiling equals the receiver's _MAX_REQ")
    fr = PC.encode_frame("app.deliver", {"a": 1}, {"b": [1, 2]})
    check(b", " not in fr and b": " not in fr and fr.endswith(b"\n"),
          "encode_frame is compact (no ', ' / ': ' expansion) and one line", fr)
    import inspect as _insp
    src = _insp.getsource(PS2.PeerServer.start)
    check("limit=" in src and "_MAX_REQ" in src,
          "the server's StreamReader limit is raised to hold a full _MAX_REQ frame (default 64 KiB raised past it)")

    # ── 4d. idempotency is per (app, peer) on both queues ───────────────────
    class EnqPool(StubPool):
        async def fetchrow(self, sql, *args):
            self.execs.append((sql, args))
            if "INSERT INTO peer_app_outbox" in sql:
                return None      # conflict: pretend the row exists
            return {"id": 3, "status": "delivered", "message_id": args[2], "attempts": 1}
    ep = EnqPool()
    row = await L.enqueue(ep, "veterans", 122, L.OP_DELIVER, "probe", {}, uuid.uuid4())
    ins = [s for s, _ in ep.execs if "INSERT INTO peer_app_outbox" in s][0]
    check("ON CONFLICT (app_id, peer_entity_id, message_id)" in ins,
          "the outbox insert conflicts on (app, peer, message_id), not message_id alone", ins)
    sel = [(s, a) for s, a in ep.execs if s.startswith("SELECT id, status")][0]
    check("app_id = $1 AND peer_entity_id = $2 AND message_id = $3" in sel[0] and sel[1][:2] == ("veterans", 122),
          "the re-select after a conflict is scoped to THIS app and THIS peer", sel)
    inbox_ins = [s for s, _ in pool.execs if "INSERT INTO peer_app_inbox" in s][0]
    check("ON CONFLICT (app_id, peer_entity_id, message_id)" in inbox_ins,
          "the inbox insert conflicts on (app, sender, message_id) — one peer cannot consume another's ids", inbox_ins)

    # ── 5. registration, scopes, migration ──────────────────────────────────
    check(H.PEER_HANDLERS.get("app.grant") is L.app_grant and H.PEER_HANDLERS.get("app.deliver") is L.app_deliver,
          "both ops are registered into PEER_HANDLERS")
    from server.applications.scopes import PERMISSIONS
    check({"read:peer_app", "write:peer_app"} <= PERMISSIONS, "the two scopes exist")
    from server.applications.build_contract import COMMUNITY_ALLOWED_SCOPES
    check({"read:peer_app", "write:peer_app"} <= COMMUNITY_ALLOWED_SCOPES,
          "community (store) apps may declare them — this is what the lane is for")
    mig = os.path.join(_ROOT, "migrations", "v153_peer_app_lane.sql")
    check(os.path.exists(mig), "migration v153 exists")
    if os.path.exists(mig):
        txt = open(mig).read()
        for t in ("peer_app_slips", "peer_app_outbox", "peer_app_inbox"):
            check(f"CREATE TABLE IF NOT EXISTS {t}" in txt, f"v153 declares {t}")
        check(txt.count("UNIQUE (app_id, peer_entity_id, message_id)") == 2,
              "both queues are UNIQUE on (app_id, peer_entity_id, message_id) — exactly that clause, twice")
        check("message_id       UUID         NOT NULL UNIQUE" not in txt,
              "and message_id is NOT unique on its own on either queue")
    # Uninstall takes the lane's state with it — both delete paths.
    import inspect as _insp2
    from server.applications import registry as R2, installer as I2
    s = _insp2.getsource(R2.uninstall)
    check("peer_app_slips" in s and "peer_app_outbox" in s and "DELETE FROM {t}" in s,
          "registry.uninstall deletes the app's slips and outbox")
    check('("peer_app_slips", "peer_app_outbox")' in s,
          "and NOT its inbox — that is user data and this path has no purge flag")
    s = _insp2.getsource(I2.uninstall_app)
    check('["peer_app_inbox"] if purge else []' in s,
          "installer.uninstall_app deletes the inbox only under purge=True, like every other artifact")

    # ── 5b. transient receiver answers are STRUCTURED, not prose ────────────
    r = await L.app_deliver(122, info, {**params, "message_id": str(uuid.uuid4()), "app_id": "veterans"})
    check(r.get("ok"), "sanity: a good delivery still lands")
    brain_service._pool = None
    r = await L.app_deliver(122, info, {**params, "message_id": str(uuid.uuid4())})
    check(r.get("ok") is False and r.get("retryable") is True, "no pool → retryable: True on the answer", r)
    r = await L.app_grant(122, {}, {"app_id": "veterans", "app_slip": slip()})
    check(r.get("ok") is False and r.get("retryable") is True, "no pool on grant → retryable: True", r)
    brain_service._pool = pool
    r = await L.app_deliver(122, {"allowed_ops": ["app:nothere"]},
                            {**params, "app_id": "nothere", "message_id": str(uuid.uuid4())})
    check(r.get("ok") is False and not r.get("retryable"), "not installed → NOT retryable (final)", r)
    srv2 = PS.PeerServer(trust=Trust(), handlers={"x": h}, my_entity_id=3)
    PS._SLIPLESS_OPS.add("x")
    try:
        async def raising(peer_id, slip_info, params):
            raise RuntimeError("boom")
        srv2._handlers["x"] = raising
        rr = await srv2._serve(122, {"op": "x", "params": {}})
        check(rr.get("retryable") is True and rr.get("error") == "handler error",
              "a handler that raises is reported retryable on the envelope", rr)

        async def transient(peer_id, slip_info, params):
            return {"ok": False, "retryable": True, "error": "this host is not ready"}
        srv2._handlers["x"] = transient
        rr = await srv2._serve(122, {"op": "x", "params": {}})
        check(rr.get("receiver") is True and rr.get("retryable") is True,
              "a handler's retryable flag rides through the receiver:true wrapper", rr)

        async def final(peer_id, slip_info, params):
            return {"ok": False, "error": "no"}
        srv2._handlers["x"] = final
        rr = await srv2._serve(122, {"op": "x", "params": {}})
        check(rr.get("receiver") is True and rr.get("retryable") is False,
              "a handler's plain refusal is retryable: False on the envelope", rr)
    finally:
        PS._SLIPLESS_OPS.discard("x")
    check(L.classify({"ok": False, "receiver": True, "retryable": True, "error": "REWORDED PROSE"}, None)[0] == "queued",
          "classify honours the flag before any prose — a reworded receiver string cannot become final")
    check(L.classify({"ok": False, "receiver": True, "retryable": False, "error": "this host is not ready"}, None)[0] == "refused",
          "and an explicit retryable: False is final even when the prose matches a transient token")
    check(L.classify({"ok": False, "receiver": True, "error": "this host is not ready"}, None)[0] == "queued",
          "while a flagless (older image) answer still falls back to its prose")

    # ── 5c. bookkeeping failure after an attempt still backs off ────────────
    class BrokenPool(RecPool):
        def __init__(self):
            super().__init__(slip_row={"slip": live_slip, "contract_id": "c1"})
            self.calls = 0

        async def execute(self, sql, *args):
            self.calls += 1
            self.updates.append((sql, args))
            if self.calls == 1:
                raise RuntimeError("db went away")
            return "OK"
    PC.send_to_peer = fake_send
    bp = BrokenPool()
    brain_service._pool = bp
    out = await L.attempt(bp, dict(base_row))
    check(out["status"] == "delivered", "the attempt itself succeeded", out)
    check(len(bp.updates) == 2 and "next_attempt_at" in bp.updates[1][0]
          and "bookkeeping_failed" in str(bp.updates[1][1]),
          "when recording the outcome fails, a backoff is still written so the row does not spin every tick",
          bp.updates)
    brain_service._pool = pool
    from server.routes import peer_apps as PR
    paths = {r.path for r in PR.router.routes}
    for p in ("/api/peer/app/peers", "/api/peer/app/grant", "/api/peer/app/send",
              "/api/peer/app/outbox", "/api/peer/app/inbox", "/api/peer/app/inbox/ack"):
        check(p in paths, f"route {p} exists")

    # ── 6. /send reports an existing row as what it is, never as queued ─────
    class FakeTok:
        app_id = "veterans"
        permissions = ["read:peer_app", "write:peer_app"]

    class FakeReq:
        class state:
            app_token = FakeTok()

    async def slip_ok(pool_, peer, app_id):
        return live_slip

    existing_id = uuid.uuid4()

    async def enqueue_existing(pool_, app_id, peer, op, action, payload, mid=None):
        return {"id": 9, "status": "delivered", "message_id": existing_id, "attempts": 1}

    attempted = []

    async def no_attempt(pool_, row):
        attempted.append(row)
        return {}

    PR.app_lane.app_slip_for = slip_ok
    PR.app_lane.enqueue = enqueue_existing
    PR.app_lane.attempt = no_attempt
    brain_service._pool = StubPool()
    out = await PR.send(PR.SendIn(peer_entity_id=122, action="probe", payload={}, message_id=str(existing_id)),
                        FakeReq())
    check(out.get("queued") is False and out.get("existing") is True and out.get("status") == "delivered",
          "/send with a message_id already sent to this peer reports the existing row, queued=False", out)
    check(not attempted, "and does not attempt it again")

    # ── 7. WHICH app: an app is itself; the host names an installed app ─────
    # kernel_op mints app_id 'host' (kernel_ops.py). Without this the host
    # would have driven the lane as an app called 'host' — coverage in name only.
    from fastapi import HTTPException as _HX
    got = await PR._app_id(FakeReq(), "other")
    check(got == "veterans", "an app's token is the app — an app_id it sends is IGNORED, never honoured", got)

    class HostTok:
        app_id = PR.HOST_TOKEN_APP_ID
        permissions = ["read:peer_app", "write:peer_app"]

    class HostReq:
        class state:
            app_token = HostTok()
    got = await PR._app_id(HostReq(), "veterans")
    check(got == "veterans", "the host names an installed app that accepts peers and drives it", got)
    got = await PR._app_id(HostReq(), "veterans", need="write:peer_app")
    check(got == "veterans", "and may drive the write half when the app declared write:peer_app", got)
    for bad, why in ((None, "no app named"), ("nothere", "not installed"), ("mute", "no read:peer_app"),
                     ("../x", "malformed"), ("host", "'host' is not an app")):
        try:
            await PR._app_id(HostReq(), bad)
            check(False, f"host with app_id={bad!r} ({why}) must be refused")
        except _HX as e:
            check(e.status_code in (400, 409), f"host with app_id={bad!r} ({why}) is refused ({e.status_code})", e.detail)
    # The receive-only app: the host may read for it, never grant or send as it.
    apps["listener"] = FakeApp(["read:peer_app"])
    got = await PR._app_id(HostReq(), "listener")
    check(got == "listener", "a read-only app can be read for by the host")
    try:
        await PR._app_id(HostReq(), "listener", need="write:peer_app")
        check(False, "a read-only app must not be driven to grant/send")
    except _HX as e:
        check(e.status_code == 409 and "write:peer_app" in str(e.detail),
              "the host cannot grant or send as an app that declared only read:peer_app", e.detail)
    import inspect as _insp3
    s = _insp3.getsource(PR.grant) + _insp3.getsource(PR.send)
    check(s.count('need="write:peer_app"') == 2, "grant and send both demand write:peer_app of the driven app")
    from server.applications import manifest as M
    check("host" in M.RESERVED_APP_IDS and "__shell__" in M.RESERVED_APP_IDS,
          "manifest reserves 'host' and '__shell__' as app ids")
    check(bool(M._ID_RE.match("host")), "sanity: the slug regex alone would have accepted 'host'")
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as td:
        p = pathlib.Path(td) / "app.yaml"
        p.write_text("id: host\nname: Host\nversion: 0.1.0\nentrypoint: index.html\npermissions: []\n")
        try:
            M.load(p)
            check(False, "a manifest with id 'host' must be refused")
        except M.ManifestError as e:
            check("reserved" in str(e), "a manifest with id 'host' is refused as reserved", e)
        except Exception as e:
            check(False, f"manifest load raised something other than ManifestError: {type(e).__name__}: {e}")
    check(PR.HOST_TOKEN_APP_ID == "host", "the host identity constant matches what kernel_ops mints")
    import inspect as _insp3
    from mastercode.runtime import kernel_ops as KO
    check('app_id="host"' in _insp3.getsource(KO), "kernel_ops still mints app_id=\"host\" — the constant is not a guess")
    check("/api/peer/app" in KO._EXPOSED_PREFIXES, "/api/peer/app is kernel_op-exposed (the host can reach the lane)")
    from mastercode.runtime.meta_tool_caps import ENTITY_SCOPE_TO_BITS as BITS
    check(BITS.get("read:peer_app") == BITS.get("read:circles") and BITS.get("write:peer_app") == BITS.get("write:circles"),
          "the lane's scopes carry the same bits as circles", (BITS.get("read:peer_app"), BITS.get("write:peer_app")))
    return 0


rc = asyncio.run(main())
print()
if FAILS:
    print(f"FAILED — {len(FAILS)} of {CHECKS}")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
if CHECKS == 0:
    print("FAILED — no checks ran; a gate that asserts nothing is not a pass")
    sys.exit(1)
print(f"peer app lane: {CHECKS}/{CHECKS} checks passed")
sys.exit(rc)
