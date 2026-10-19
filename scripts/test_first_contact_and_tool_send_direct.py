#!/usr/bin/env python3
"""The two host↔host paths that were still bridge-only, and the shared knock.

Container-free: every pool and every transport is stubbed, so this runs on every
merge rather than skipping without a Postgres.

Background. The bridge relay keeps its connections in an in-process dict
per DaemonSet pod with no forwarding between them, so it only ever delivered
within a node — measured, 8 hosts across 4 nodes = 5 of 28 possible
pairs, with a Host alone on its node able to reach none of the other seven. The
Circles composer moved to the direct peer lane first. Two paths did not, and both
are first-class user actions:

  * send_message_service — the `host_message` TOOL's send. Every host↔host DM the
    MODEL sends, as opposed to the ones a person types into Circles.
  * request_by_phone_service — first contact by phone alias. A knock that cannot
    arrive means no slip is ever issued, and a slip is the sole authority for
    host↔host messaging, so this one gates the entire relationship.

What is pinned:

  1. A slip-holding tool send takes the direct lane, and the relay is not also
     called. Calling both is not belt-and-braces here: message_id dedupes the
     second delivery, so the relay call is pure latency on the happy path.
  2. A send with NO slip goes straight to the relay. `message` is slip-only on the
     receiving side, so there is nothing to present and the direct lane would only
     ever be refused.
  3. The id on the wire is NOT the id of our own copy. They are different messages
     in different pods' inboxes; sharing one lets a future reader that dedupes
     across both directions collapse the pair and lose the sent side.
  4. The same id reaches BOTH transports, or the dedupe that makes the fallback
     safe cannot fire.
  5. A knock tries the peer lane first and falls back on a LANE refusal.
  6. A RECEIVER refusal is raised, never retried — the relay would hand the same
     card to the same pod and the same gate. This is the one that regressed once
     before, by being raised inside a try that swallowed it.
  7. An older image (no peer_client module) falls back rather than failing.
  8. The CALLERS fall back and 409, not just the helper. Sections 5-7 exercise
     _knock_first directly, and on its own that left request_by_phone_service's
     own use of it uncovered — it had neither the fallback nor the refusal branch
     and every gate stayed green while an older-image peer got a 500 and the
     request went nowhere.

Run:  APP_ROOT=<repo> python3 scripts/test_first_contact_and_tool_send_direct.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import types

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain"), os.path.join(_ROOT, "system-apps")]

FAILS: list[str] = []


def check(label: str, cond: bool, extra: object = "") -> None:
    print(("  PASS  " if cond else "  FAIL  ") + label
          + (("\n        " + str(extra)) if extra and not cond else ""))
    if not cond:
        FAILS.append(label)


ME, PEER = 42, 4242


def _install_peer_client(*, send=None, knock=None):
    """Replace server.peer.peer_client, keeping the package's REAL __path__ so its
    siblings (notably server/peer/deliver.py, which carries the wire call) still
    import. An empty __path__ turns a broken stub into a ModuleNotFoundError that
    reads like a missing module."""
    pkg = sys.modules.get("server.peer")
    if pkg is None or not getattr(pkg, "__path__", None):
        pkg = types.ModuleType("server.peer")
        pkg.__path__ = [os.path.join(_ROOT, "server", "peer")]
        sys.modules["server.peer"] = pkg
    mod = types.ModuleType("server.peer.peer_client")

    class PeerUnavailable(Exception):
        pass

    mod.PeerUnavailable = PeerUnavailable
    if send is not None:
        mod.send_to_peer = send
    if knock is not None:
        mod.knock_peer = knock
    sys.modules["server.peer.peer_client"] = mod
    pkg.peer_client = mod
    sys.modules.pop("server.peer.deliver", None)   # re-import against this stub
    return PeerUnavailable


async def main() -> int:
    _stub_main = types.ModuleType("server.main")
    _stub_main.app = None
    sys.modules.setdefault("server.main", _stub_main)

    import server.routes.messages as M
    import server.routes.contacts as C

    # ── the host_message tool's send ────────────────────────────────────────────
    print("\n── 1. the tool's DM takes the direct lane when a slip is held ──")
    wire: list[tuple] = []
    relayed: list[tuple] = []

    async def cap_send(peer_entity_id, op, slip, params, *, timeout=10.0):
        wire.append((peer_entity_id, op, slip, params))
        return {"ok": True, "data": {"message_id": "r1", "person_stored": None}}

    _install_peer_client(send=cap_send)

    class _Pool:
        async def fetchval(self, sql="", *a, **k):
            # The topic a send with none named goes into (messages._send_topic,
            # the cards): this peer has sent nothing yet, so the original
            # conversation, which puts no nested payload on either lane.
            if "topic_id" in sql:
                return None
            return "11111111-1111-1111-1111-111111111111"
        async def fetchrow(self, *a, **k):
            return {"peer_entity_id": PEER, "handle": "peer", "display_name": "P"}
        async def execute(self, *a, **k):
            return None

    async def _svc(op, params):
        relayed.append((op, params))
        return {}

    M._bridge_svc = _svc
    # No _resolve_target stub: the first cut set one, and the function is named
    # _resolve_target_service — so the line did nothing while reading like cover for
    # the handle-resolution path. Every case here passes to_entity_id, which
    # short-circuits resolution before any bridge call.
    import server.routes.circles as circles
    circles._held_slip_for = lambda t: asyncio.sleep(0, {"terms": {}, "sig": "x"})

    res = await M.send_message_service(_Pool(), ME, to_entity_id=PEER, body="hi")
    check("the send reports ok", res.get("ok") is True, res)
    check("it went down the direct lane", len(wire) == 1, wire)
    check("...and the relay was NOT also called — message_id dedupes the second "
          "delivery, so relaying too is pure latency", not relayed, relayed)
    if wire:
        p = wire[0][3]
        check("the wire carries a message_id", bool(p.get("message_id")), p)
        check("...which is NOT our own copy's id (different message, different pod)",
              p.get("message_id") != res.get("message_id"),
              (p.get("message_id"), res.get("message_id")))

    print("\n── 2. no slip -> straight to the relay ──")
    wire.clear(); relayed.clear()
    circles._held_slip_for = lambda t: asyncio.sleep(0)
    res = await M.send_message_service(_Pool(), ME, to_entity_id=PEER, body="hi")
    check("nothing went down the direct lane — `message` is slip-only on the far "
          "side, so there is nothing to present", not wire, wire)
    check("the relay carried it", len(relayed) == 1, relayed)
    if relayed:
        check("...and the SAME id reaches the relay, or the dedupe that makes the "
              "fallback safe cannot fire", bool(relayed[0][1].get("message_id")),
              relayed[0][1])

    print("\n── 3. the direct lane declines -> the relay still carries it ──")
    wire.clear(); relayed.clear()
    circles._held_slip_for = lambda t: asyncio.sleep(0, {"terms": {}, "sig": "x"})

    async def refusing(peer_entity_id, op, slip, params, *, timeout=10.0):
        wire.append((peer_entity_id, op, slip, params))
        return {"ok": False, "error": "slip expired"}

    _install_peer_client(send=refusing)
    res = await M.send_message_service(_Pool(), ME, to_entity_id=PEER, body="hi")
    check("it tried direct", len(wire) == 1, wire)
    check("...then relayed", len(relayed) == 1, relayed)
    check("and the send still reports ok", res.get("ok") is True, res)

    # ── first contact ───────────────────────────────────────────────────────────
    print("\n── 4. a knock tries the peer lane first ──")
    knocks: list[tuple] = []
    bridged: list[tuple] = []

    async def cap_knock(peer_entity_id, params, *, timeout=10.0):
        knocks.append((peer_entity_id, params))
        return {"ok": True, "data": {"message_id": "k1"}}

    async def _bop(op, params=None):
        bridged.append((op, params))
        return {"message_id": "b1"}

    C._bridge_op = _bop
    _install_peer_client(knock=cap_knock)
    data = await C._knock_first(PEER, {"body": "hi"}, what="test")
    check("a successful knock returns the peer's data", (data or {}).get("message_id") == "k1", data)
    check("...and the relay is untouched", not bridged, bridged)

    print("\n── 5. a RECEIVER refusal is raised, never retried ──")
    knocks.clear(); bridged.clear()

    async def receiver_no(peer_entity_id, params, *, timeout=10.0):
        return {"ok": False, "receiver": True, "error": "one is already waiting"}

    _install_peer_client(knock=receiver_no)
    raised = None
    try:
        await C._knock_first(PEER, {"body": "hi"}, what="test")
    except C._KnockRefused as e:
        raised = str(e)
    check("the far side's no is raised", raised == "one is already waiting", raised)
    check("...and the relay is NOT tried — it would hand the same card to the same "
          "pod and the same gate", not bridged, bridged)

    print("\n── 6. a LANE refusal falls back, because nothing was stored ──")
    async def lane_no(peer_entity_id, params, *, timeout=10.0):
        return {"ok": False, "error": "unknown op"}

    _install_peer_client(knock=lane_no)
    data = await C._knock_first(PEER, {"body": "hi"}, what="test")
    check("a lane refusal returns None so the caller relays", data is None, data)

    print("\n── 7. an older image falls back rather than failing ──")
    sys.modules["server.peer.peer_client"] = None
    data = await C._knock_first(PEER, {"body": "hi"}, what="test")
    check("no peer_client module -> None, not an exception", data is None, data)

    print("\n── 8. the phone-alias request uses the same knock ──")
    knocks.clear(); bridged.clear()
    _install_peer_client(knock=cap_knock)

    async def _bop2(op, params=None):
        bridged.append((op, params))
        if op == "phone.alias.lookup":
            return {"found": True, "entity_id": PEER, "display_name": "P",
                    "entity_uuid": "u"}
        return {"message_id": "b1"}

    C._bridge_op = _bop2
    res = await C.request_by_phone_service(_Pool(), alias="+1555", note="hi")
    check("the request reports ok", res.get("ok") is True, res)
    check("it knocked directly", len(knocks) == 1, knocks)
    check("...and the only bridge call was the alias LOOKUP, not the delivery — "
          "resolution is platform-side on purpose, delivery is not",
          [op for op, _ in bridged] == ["phone.alias.lookup"], bridged)

    print("\n── 9. the phone request FALLS BACK, and 409s on a refusal ──")
    # Section 8 drove only the happy knock, and sections 5-7 called _knock_first
    # DIRECTLY — so the helper was covered and the caller's use of it was not. The
    # relay fallback and the refusal branch were both missing from this service and
    # every gate stayed green. That is what these two drive.
    knocks.clear(); bridged.clear()

    async def lane_down(peer_entity_id, params, *, timeout=10.0):
        return {"ok": False, "error": "unknown op"}

    _install_peer_client(knock=lane_down)
    C._bridge_op = _bop2
    res = await C.request_by_phone_service(_Pool(), alias="+1555", note="hi")
    check("an unreachable lane still SENDS the request, via the relay",
          res.get("ok") is True and "inbox.send" in [op for op, _ in bridged], bridged)

    knocks.clear(); bridged.clear()

    async def receiver_no2(peer_entity_id, params, *, timeout=10.0):
        return {"ok": False, "receiver": True, "error": "one is already waiting"}

    _install_peer_client(knock=receiver_no2)
    err = None
    try:
        await C.request_by_phone_service(_Pool(), alias="+1555", note="hi")
    except C.ContactsError as e:
        err = e.status
    except Exception as e:
        err = f"{type(e).__name__} (not a ContactsError -> 500 to the caller)"
    check("a receiver refusal is a 409, not an unhandled raise", err == 409, err)
    check("...and it is NOT relayed to the same gate for the same answer",
          "inbox.send" not in [op for op, _ in bridged], bridged)

    print()
    if FAILS:
        print(f"FAILURES: {len(FAILS)}")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print(f"first-contact + tool-send direct-lane checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
