#!/usr/bin/env python3
"""The reciprocal slip after an accept travels the direct peer lane.

THE BUG: when host A accepts host R's connection request, R issues A
a message slip back so A can message first. That slip went by the bridge relay
only (memory_handlers._reciprocate -> inbox.send), and the relay reaches only
entities on its own DaemonSet pod — so across nodes A never received it and its
first message was refused for want of a slip.

Pinned here:
  1. contact.slip is classified: slip-gated on `message`, never slipless, so an
     unaccepted host cannot call it;
  2. the handler stores ONLY a slip issued by the presenting host, to us, for
     dm:contact / message — anything else is refused before any write;
  3. what it stores names the peer from the cert and our own records, as a
     permission_grant (the kind that writes held_slips and never reciprocates);
  4. _reciprocate_direct: direct delivery or a receiver's refusal is final;
     no held slip, an unreachable peer or a lane refusal falls back to the relay;
  5. reciprocate_grant: the relay is called only when the direct lane did not
     deliver, and both carry ONE message_id so a timeout cannot land two cards;
  6. a permission_grant's slip is kept only from a host that is a pending or
     active contact — a deleted contact cannot come back by sending one;
  7. the two `$n::interval` windows are timedeltas (asyncpg rejects a str, which
     made the knock cap fail open and every pinned egress rule fail).

Container-free: APP_ROOT=<repo> python3 scripts/test_reciprocal_slip_direct.py
"""
import asyncio
import inspect
import os
import sys

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain")]

FAILS = []


def check(cond, msg, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + msg + (f"\n        {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(msg)


ME, PEER = 164, 163


def slip(issuer=PEER, grantee=ME, resource="dm:contact", ops=("message",)):
    return {"contract_id": "slip-x", "signature": "sig",
            "terms": {"issuer": issuer, "grantee": grantee, "resource": resource,
                      "allowed_ops": list(ops), "expires_at": "2026-12-01T00:00:00+00:00"}}


async def main() -> int:
    from server import brain_service
    from server.peer import handlers as H
    from server.peer import peer_server as PS
    from server.bridge import memory_handlers as M
    brain_service._entity_id = ME

    print("1. classification")
    check(PS._OP_ACTION.get("contact.slip") == "message",
          "contact.slip rides the message slip", PS._OP_ACTION.get("contact.slip"))
    check("contact.slip" not in PS._SLIPLESS_OPS, "and is never slipless")
    check(H.PEER_HANDLERS.get("contact.slip") is H.contact_slip, "the op is registered")

    print("2. the carried slip must be ours, from them, for messaging only")
    stored = []

    async def fake_append(payload):
        stored.append(payload)
        return {"ok": True, "message_id": "m1"}

    async def fake_identify(peer_id):
        return {"from_handle": "probe-a", "from_display_name": "Probe A"}

    H.handle_inbox_append = fake_append
    H._identify = fake_identify
    for label, bad in (("issued by a third host", slip(issuer=999)),
                       ("granted to someone else", slip(grantee=999)),
                       ("for a wider resource", slip(resource="context:*")),
                       ("for more than messaging", slip(ops=("message", "read"))),
                       ("with no terms", {"contract_id": "x"}),
                       ("absent", None)):
        r = await H.contact_slip(PEER, {}, {"contact_slip": bad} if bad is not None else {})
        check(r.get("ok") is False and not stored, f"a slip {label} is refused, nothing stored", r)

    print("3. a good one is stored as a permission_grant naming the cert's peer")
    r = await H.contact_slip(PEER, {}, {"contact_slip": slip(),
                                        "from_handle": "chosen-by-sender"})
    p = stored[-1] if stored else {}
    check(r.get("ok") is True and p.get("message_kind") == "permission_grant",
          "stored as permission_grant", p)
    check(p.get("from_entity_id") == PEER and p["payload"]["contact_slip"]["terms"]["issuer"] == PEER,
          "keyed by the cert's peer id")
    check(p["payload"].get("from_handle") == "probe-a" and p["payload"].get("lane") == "peer",
          "its name comes from our records, not the sender's params", p.get("payload"))

    print("4. _reciprocate_direct")
    import types
    _stub_main = types.ModuleType("server.main")   # circles imports server.main
    _stub_main.app = None
    sys.modules.setdefault("server.main", _stub_main)
    import server.routes.circles as C
    import server.peer.peer_client as PC
    sent = []
    outcomes = {}

    async def held_none(peer):
        return None

    async def held_some(peer):
        return {"contract_id": "slip-a-issued-us"}

    async def send(peer, op, s, params, **kw):
        sent.append((peer, op, s, params))
        o = outcomes["next"]
        if isinstance(o, Exception):
            raise o
        return o

    PC.send_to_peer = send
    C._held_slip_for = held_none
    check(await M._reciprocate_direct(PEER, {"k": 1}) is False and not sent,
          "no held slip -> relay, nothing sent")
    C._held_slip_for = held_some
    for label, o, want in (("delivered", {"ok": True, "data": {}}, True),
                           ("the receiver refused", {"ok": False, "receiver": True, "error": "no"}, True),
                           ("a lane refusal (older image)", {"ok": False, "error": "unknown op 'contact.slip'"}, False),
                           ("unreachable", PC.PeerUnavailable("timeout"), False)):
        outcomes["next"] = o
        got = await M._reciprocate_direct(PEER, {"k": 1})
        check(got is want, f"{label} -> {'final' if want else 'relay'}", got)
    check(sent and sent[-1][1] == "contact.slip" and sent[-1][2] == {"contract_id": "slip-a-issued-us"}
          and sent[-1][3] == {"contact_slip": {"k": 1}, "message_id": None},
          "sent as contact.slip under the slip the accepter issued us", sent[-1] if sent else None)

    print("5. reciprocate_grant: direct first, the relay only as fallback, one id for both")
    relayed, direct_calls = [], []

    async def fake_bridge(op, params):
        if op == "contract.issue":
            return {"slip": {"contract_id": "slip-back"}}
        if op == "host.self_card":        # our own card, shared with the acceptor
            return {"owner_name": "Alex", "phone": "+15550100003", "account_hosts": [ME]}
        relayed.append((op, params))
        return {}

    from server.hosts import self_card as SCD
    SCD._cache.update(at=0.0, card=None)

    import server.routes.contacts as CT
    CT._bridge_op = fake_bridge
    real_direct = M._reciprocate_direct

    def direct(result):
        async def d(grantee, back, *, message_id=None, card=None):
            direct_calls.append((grantee, back, message_id))
            return result
        return d

    M._reciprocate_direct = direct(True)
    lane = await M.reciprocate_grant(PEER)
    check(lane == "peer" and not relayed and direct_calls[-1][:2] == (PEER, {"contract_id": "slip-back"}),
          "delivered direct -> the relay is NOT called", (lane, relayed))
    M._reciprocate_direct = direct(False)
    lane = await M.reciprocate_grant(PEER)
    rel = relayed[-1][1] if relayed else {}
    check(lane == "relay" and relayed and relayed[-1][0] == "inbox.send"
          and rel.get("message_kind") == "permission_grant"
          and (rel.get("payload") or {}).get("contact_slip") == {"contract_id": "slip-back"},
          "direct unavailable -> the relay carries it", relayed)
    check(rel.get("payload", {}).get("card") == {"owner_name": "Alex", "phone": "+15550100003"},
          "the relay carries our card (owner name + number) too", rel.get("payload"))
    check(rel.get("message_id") and rel.get("message_id") == direct_calls[-1][2],
          "the relay carries the SAME message_id the direct attempt used (no double card)",
          (rel.get("message_id"), direct_calls[-1][2]))
    M._reciprocate_direct = real_direct

    print("6. a permission_grant is kept only from a host that is our contact")

    class P:
        def __init__(self, row):
            self.row, self.q = row, []

        async def fetchval(self, sql, *a):
            self.q.append((" ".join(sql.split()), a))
            return self.row

    check(await M._grant_admissible(P(None), "endpoint_grant", PEER) is False
          and await M._grant_admissible(P(1), "endpoint_grant", PEER) is True,
          "an endpoint_grant is kept only if we asked that host (every transport)")
    pool = P(1)
    check(await M._grant_admissible(pool, "permission_grant", PEER) is True
          and "status IN ('pending', 'active')" in pool.q[-1][0] and pool.q[-1][1] == (PEER,),
          "a permission_grant from a pending/active contact is kept", pool.q)
    check(await M._grant_admissible(P(None), "permission_grant", PEER) is False,
          "from a deleted (or never-made) contact it is not")

    class Boom(P):
        async def fetchval(self, sql, *a):
            raise RuntimeError("pool down")
    check(await M._grant_admissible(Boom(None), "permission_grant", PEER) is False,
          "a failed check keeps nothing (fails closed)")
    src = inspect.getsource(M.handle_inbox_append)
    check("await _grant_admissible(pool, message_kind, from_entity_id," in src,
          "handle_inbox_append applies the gate before storing the slip")

    print("6b. accepting without a card id still answers that host's open requests")
    import server.routes.contacts as CT2

    class AcceptPool:
        """Answers what accept_request_service asks; records every write."""
        def __init__(self):
            self.execs = []

        async def fetchval(self, sql, *a):
            return None                      # handle not claimed by anyone else

        async def fetchrow(self, sql, *a):
            return {"id": 7, "status": "pending", "handle": "probe-a",
                    "peer_entity_id": PEER, "kind": "host"}

        async def fetch(self, sql, *a):
            return []

        async def execute(self, sql, *a):
            self.execs.append((" ".join(sql.split()), a))
            return "UPDATE 1"

    async def no_bridge(op, params):
        raise RuntimeError("no bridge in this test")

    real_bridge_op = CT2._bridge_op
    CT2._bridge_op = no_bridge
    try:
        for label, mid, want in (("no card id", None, True), ("a card id", "11111111-1111-1111-1111-111111111111", False)):
            pool = AcceptPool()
            try:
                await CT2.accept_request_service(pool, handle="probe-a", message_id=mid,
                                                 from_entity_id=PEER, display_name="Probe A")
            except Exception as exc:  # noqa: BLE001 — delivery may fail; the archive is what is tested
                pool.execs.append(("raised " + type(exc).__name__, ()))
            swept = [a for q, a in pool.execs
                     if "UPDATE inbox_messages SET archived_at = now()" in q
                     and "message_kind = 'endpoint_request'" in q
                     # a PERSON's card from that host is never swept with it
                     and "from_user_email IS NULL" in q]
            check((swept == [(PEER,)]) if want else not swept,
                  f"accept with {label}: {'archives' if want else 'does not sweep'} "
                  f"{PEER}'s open request cards", pool.execs[-3:])
    finally:
        CT2._bridge_op = real_bridge_op

    print("7. the interval windows bind (asyncpg takes a timedelta for $n::interval)")
    import datetime as _dtm
    from server.network import nac_egress as NE
    check(isinstance(H.KNOCK_SLOT_WINDOW, _dtm.timedelta)
          and H.KNOCK_SLOT_WINDOW == _dtm.timedelta(days=7),
          "KNOCK_SLOT_WINDOW is a timedelta (a str made the knock cap fail open)")
    check(isinstance(NE._PIN_LIVE_WINDOW, _dtm.timedelta),
          "_PIN_LIVE_WINDOW is a timedelta (a str failed every pinned egress rule)")

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): " + "; ".join(FAILS))
        return 1
    print("ok  the reciprocal slip goes direct, and only a well-formed one is kept")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
