#!/usr/bin/env python3
"""The contact card for another Host — server/hosts/card.py.

Anywhere the dashboard names another Host, show a contact
card instead of a bare string. One resolver; everything from this Host's OWN
records; "Host", never "entity".

Pinned here, through the real resolver with a fake pool that answers the SQL it
writes (the SQL itself was run against the real schema on PG 18 on 2026-09-22):
  1. names come in priority order: the user's own contact name, then what that
     Host said it is called, then a roster name, then its announced handle,
     then "Host #N";
  2. the address is the peering's full name, exactly as that peering carries
     it, else none: a bare handle (announced, typed or in a peering) never
     becomes an address, because another Host's zone is not this Host's and a
     name built from the handle in this one would point at nothing;
  3. relationship: an active contact > pending/blocked > shares a machine >
     not connected; "can message" means a LIVE held slip;
  4. this Host itself and junk ids are left out; a store that errors does not
     blank the card; contact number and user stay None (never guessed);
  5. nothing on a card says "entity";
  6. the route is registered and reads under read:address_book.

Container-free: APP_ROOT=<repo> python3 scripts/test_host_card.py
"""
import asyncio
import inspect
import json
import os
import sys

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain")]

FAILS = []


def check(cond, msg, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + msg + (f"\n        {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(msg)


ME = 9003


class Pool:
    def __init__(self, contacts=(), said=(), slips=(), roster=(), shared=(), broken=()):
        self.data = {"contacts": list(contacts), "said": list(said),
                     "slips": list(slips), "roster": list(roster), "shared": list(shared)}
        self.execs = []
        self.broken = set(broken)

    async def fetch(self, sql, *a):
        s = " ".join(sql.split())
        want = set(a[0]) if a else set()
        for key, marker, idcol in (("contacts", "FROM contacts", "peer_entity_id"),
                                   ("said", "FROM inbox_messages", "from_entity_id"),
                                   ("slips", "FROM held_slips", "peer_entity_id"),
                                   ("roster", "FROM group_members", "member_entity_id"),
                                   ("shared", "FROM peer_cards", "peer_entity_id")):
            if marker in s:
                if key in self.broken:
                    raise RuntimeError("store down")
                return [r for r in self.data[key] if r[idcol] in want]
        return []

    async def execute(self, sql, *a):
        self.execs.append((" ".join(sql.split()), a))
        return "INSERT 0 1"


async def main() -> int:
    from server.hosts import card as C
    import server.network.peer_discovery as PD

    peerings = []

    async def fake_peerings(pool, eid):
        return list(peerings)
    PD.list_peerings = fake_peerings
    from server.hosts import self_card as SC
    own = {"card": None}

    async def fake_own(*, fresh=False):
        return own["card"]
    SC.own_card = fake_own

    print("1. names, in priority order")
    pool = Pool(contacts=[{"peer_entity_id": 10, "handle": "typed-by-user", "display_name": "My Bravo",
                           "status": "active"}],
                said=[{"from_entity_id": 10, "name": "Bravo", "handle": "259c83f46775ec66"},
                      {"from_entity_id": 11, "name": "Charlie", "handle": None},
                      {"from_entity_id": 13, "name": None, "handle": "feedfacefeedface"}],
                roster=[{"member_entity_id": 12, "display_name": "Delta"}])
    # The peering's name is in a zone of its own (a Host on another machine):
    # the card must carry it as given, whatever this Host's zone is.
    PEER_NAME = "aaaabbbbccccdddd.cseries.example"
    peerings[:] = [{"peer_entity_id": 13, "peer_host_name": PEER_NAME,
                    "peer_handle": "aaaabbbbccccdddd", "current": True}]
    got = await C.host_cards(pool, ME, [10, 11, 12, 13, 14])
    names = {k: v["display_name"] for k, v in got.items()}
    check(names == {10: "My Bravo", 11: "Charlie", 12: "Delta", 13: "aaaabbbbccccdddd", 14: "Host #14"},
          "contact name > what the Host said > roster > handle > Host #N", names)

    print("2. addresses")
    check(got[10]["address"] is None,
          "a bare handle gives no address, even a 16-hex one a Host announced about itself",
          got[10]["address"])
    check(got[13]["address"] == PEER_NAME,
          "a peering's full name is the address, as given, whatever zone it is in",
          got[13]["address"])
    check(got[11]["address"] is None and got[14]["address"] is None,
          "no peering, no address — a name or a handle alone never becomes one")
    peerings[:] = [{"peer_entity_id": 15, "peer_host_name": None,
                    "peer_handle": "aaaabbbbccccdddd", "current": True},
                   {"peer_entity_id": 16, "peer_host_name": "", "peer_handle": "bbbbccccddddeeee",
                    "current": True}]
    nameless = await C.host_cards(Pool(), ME, [15, 16])
    check(nameless[15]["address"] is None and nameless[16]["address"] is None,
          "a peering with no full name has no address — its handle is not one")
    check(nameless[15]["display_name"] == "aaaabbbbccccdddd",
          "... though the handle still names the Host on its card")
    typed = await C.host_cards(Pool(contacts=[{"peer_entity_id": 20, "handle": "entity-20",
                                               "display_name": None, "status": "active"}]), ME, [20])
    check(typed[20]["address"] is None, "a contact handle like 'entity-20' is not an address")

    print("3. relationship and messaging")
    pool = Pool(contacts=[{"peer_entity_id": 30, "handle": "a", "display_name": "A", "status": "active"},
                          {"peer_entity_id": 31, "handle": "b", "display_name": "B", "status": "pending"},
                          {"peer_entity_id": 32, "handle": "c", "display_name": "C", "status": "blocked"}],
                slips=[{"peer_entity_id": 30, "live": True}, {"peer_entity_id": 31, "live": False}])
    peerings[:] = [{"peer_entity_id": 32, "peer_host_name": "x.cseries.localhost", "peer_handle": "x", "current": True},
                   {"peer_entity_id": 33, "peer_host_name": "y.cseries.localhost", "peer_handle": "y", "current": False}]
    got = await C.host_cards(pool, ME, [30, 31, 32, 33, 34])
    rel = {k: (v["relationship"], v["relationship_label"]) for k, v in got.items()}
    check(rel[30] == ("contact", "Contact") and rel[31] == ("pending", "Connection pending")
          and rel[32] == ("blocked", "Blocked"), "a contact's status decides first", rel)
    check(rel[33] == ("shares_machine", "Shares a machine with you") and got[33]["shares_machine"] is False,
          "a peering is 'shares a machine'; `shares_machine` is only true while current", got[33])
    check(rel[34] == ("known", "Not connected"), "otherwise: not connected")
    check(got[30]["can_message"] is True and got[31]["can_message"] is False,
          "can message = a LIVE held slip")

    print("4. what is left out, and what is never guessed")
    got = await C.host_cards(Pool(), ME, [ME, "x", -1, 0, None, 40, 40])
    check(list(got) == [40], "this Host itself, junk and duplicates are dropped", list(got))
    check(got[40]["contact_number"] is None and got[40]["user"] is None,
          "contact number and user are None — only ever what that Host shares")
    got = await C.host_cards(Pool(contacts=[{"peer_entity_id": 41, "handle": "h", "display_name": "Kept",
                                             "status": "active"}], broken={"said", "slips"}), ME, [41])
    check(got[41]["display_name"] == "Kept" and got[41]["can_message"] is False,
          "a store that errors does not blank the card", got.get(41))
    peerings.clear()
    got = await C.host_cards(Pool(), ME, list(range(100, 100 + C.MAX_IDS + 50)))
    check(len(got) == C.MAX_IDS, "at most MAX_IDS cards per call", len(got))

    print("4b. what a Host shared on an accepted connection, and 'My host'")
    own["card"] = {"owner_name": "Alex", "phone": "+15550100003", "account_hosts": [ME, 60]}
    got = await C.host_cards(Pool(shared=[{"peer_entity_id": 61, "owner_name": "Dana", "phone": "+15550100161"}]),
                             ME, [60, 61, 62])
    check(got[60]["relationship_label"] == "My host" and got[60]["user"] == "Alex" and got[60]["mine"] is True,
          "a host of the same account reads 'My host', its user is the user", got[60])
    check(got[61]["relationship_label"] == "Dana's host" and got[61]["user"] == "Dana"
          and got[61]["contact_number"] == "+15550100161" and got[61]["connection_label"] == "Not connected",
          "a shared card fills user, number and \"<name>'s host\"; the connection is its own line", got[61])
    check(got[62]["user"] is None and got[62]["contact_number"] is None
          and got[62]["relationship_label"] == "Not connected",
          "nothing shared: nothing guessed")
    own["card"] = None
    got = await C.host_cards(Pool(), ME, [60])
    check(got[60]["relationship_label"] == "Not connected" and got[60]["mine"] is False,
          "no own card (older bridge): no 'My host' claimed")

    print("4c. storing what a peer shares — the number only if it is theirs")
    from server.hosts import peer_cards as PCD
    import types as _t
    sys.modules.setdefault("server.main", _t.ModuleType("server.main"))
    import server.routes.contacts as CT
    looked = []

    async def fake_bridge(op, params):
        looked.append((op, params))
        if op == "phone.alias.lookup":
            owner = {"+15550100170": 70}.get(params["alias"], 999)
            return {"found": True, "entity_id": owner, "alias": params["alias"]}
        raise RuntimeError(op)
    CT._bridge_op = fake_bridge
    pool = Pool()
    r = await PCD.store_peer_card(pool, 70, {"owner_name": " Dana\x07 ", "phone": "+15550100170"})
    check(r == {"owner_name": "Dana", "phone": "+15550100170"} and pool.execs
          and pool.execs[-1][1][:3] == (70, "Dana", "+15550100170"),
          "a verified number and a cleaned name are kept", (r, pool.execs[-1:]))
    r = await PCD.store_peer_card(pool, 71, {"owner_name": "Eve", "phone": "+15550100170"})
    check(r == {"owner_name": "Eve", "phone": None},
          "a number the platform says is ANOTHER host's is dropped", r)
    n = len(pool.execs)
    check(await PCD.store_peer_card(pool, 72, {"phone": "not a number"}) is None and len(pool.execs) == n,
          "nothing usable: nothing written")
    check(await PCD.store_peer_card(pool, 73, "junk") is None, "a non-object card is ignored")
    check(PCD.clean_name("x" * 200) == "x" * PCD.MAX_NAME, "a name is capped")

    print("4e. THROUGH THE REAL handle_inbox_append: a grant is honoured only if we asked")
    from server.bridge import memory_handlers as MH
    from server import brain_service

    class InboxPool:
        """Answers what handle_inbox_append asks; `contacts` decides admission."""
        def __init__(self, contacts):
            self.contacts, self.execs = contacts, []

        async def fetchval(self, sql, *a):
            q = " ".join(sql.split())
            if "FROM contacts" in q:
                if "kind = 'human'" in q:
                    return 1 if ("human", str(a[0]).lower()) in self.contacts else None
                return 1 if ("host", int(a[0])) in self.contacts else None
            return "00000000-0000-0000-0000-000000000001"

        async def fetchrow(self, sql, *a):
            # The block check (contacts.block_refuses): nobody here is blocked.
            # Block itself is pinned against a real database in
            # test_circles_block_pg.py.
            if "AS host_blocked" in sql:
                return {"host_blocked": False, "person_blocked": False, "person_on_host": False}
            return {"message_id": "00000000-0000-0000-0000-000000000001", "inserted": True}

        async def fetch(self, sql, *a):
            return []

        async def execute(self, sql, *a):
            self.execs.append(" ".join(sql.split()))
            return "INSERT 0 1"

    stored_cards, sent_back = [], []

    async def rec_store(pool, peer, card):
        stored_cards.append((peer, card))

    async def rec_recip(grantee):
        sent_back.append(grantee)
    import server.hosts.peer_cards as PCD2
    real_store, real_recip = PCD2.store_peer_card, MH.reciprocate_grant
    PCD2.store_peer_card = rec_store
    MH.reciprocate_grant = rec_recip
    brain_service._entity_id = ME
    grant = {"to_entity_id": ME, "from_entity_id": 666, "message_kind": "endpoint_grant",
             "subject": "Connection accepted", "body": "", "body_format": "text/plain",
             "message_id": "11111111-1111-1111-1111-111111111111",
             "payload": {"contact_slip": {"terms": {}}, "from_handle": "evil",
                         "card": {"owner_name": "Alex", "phone": "+15550100003"}}}

    async def run_grant(contacts, g=grant):
        stored_cards.clear(); sent_back.clear()
        pool = InboxPool(contacts)
        brain_service.get_pool = lambda: pool
        await MH.handle_inbox_append(dict(g))
        await asyncio.sleep(0)            # let the reciprocate task run
        return pool

    pool = await run_grant(set())
    check(not stored_cards and not sent_back and not any("held_slips" in q for q in pool.execs),
          "a stranger's endpoint_grant (relay-shaped): no slip, no card stored, nothing sent back",
          (stored_cards, sent_back))
    pool = await run_grant({("host", 666)})
    check(stored_cards and stored_cards[-1][0] == 666 and sent_back == [666],
          "a grant from a host we ASKED: its card is stored and ours goes back", (stored_cards, sent_back))
    human = dict(grant, from_user_email="dana@example.com",
                 payload={**grant["payload"], "kind": "human"})
    await run_grant({("host", 666)}, human)
    check(not stored_cards, "a person's grant never stores a host card")
    PCD2.store_peer_card, MH.reciprocate_grant = real_store, real_recip

    print("4f. a phone request records that we asked; the placeholder yields to the real handle")
    src_phone = inspect.getsource(CT.request_by_phone_service)
    check("INSERT INTO contacts" in src_phone and "'pending'" in src_phone and 'f"host {int(target_id)}"' in src_phone,
          "request_by_phone_service writes a pending 'host <id>' contact")
    up = Pool()
    async def not_claimed(pool, handle, pid):
        return None
    CT.handle_claimed_by = not_claimed
    await CT.upsert_contact_from_grant(up, handle="259c83f46775ec66", kind="host",
                                       display_name="Bravo", peer_entity_id=80)
    dels = [(q, a) for q, a in up.execs if q.startswith("DELETE FROM contacts")]
    check(dels and dels[0][1] == (80, "259c83f46775ec66") and "handle = 'host ' || $1::text" in dels[0][0],
          "only the 'host <id>' placeholder for THAT host is removed", dels)

    print("4g. numbers and names, hardened")
    pool = Pool()
    await PCD.store_peer_card(pool, 70, {"owner_name": "Dana", "phone": "+15550100170"})
    keep = pool.execs[-1][1]
    check(keep[2] == "+15550100170" and keep[3] is False, "a verified number replaces", keep)
    await PCD.store_peer_card(pool, 70, {"owner_name": "Dana", "phone": "+15550100999"})
    keep = pool.execs[-1][1]
    check(keep[2] is None and keep[3] is True, "an unverifiable number KEEPS the verified one", keep)
    await PCD.store_peer_card(pool, 70, {"owner_name": "Dana"})
    keep = pool.execs[-1][1]
    check(keep[2] is None and keep[3] is False, "no number offered: it is cleared", keep)
    nbsp = "My host" + "\u00a0" * 60 + "x"
    check(PCD.clean_name(nbsp) == "My host x" and PCD.clean_name("Al\u200bex\u202e") == "Alex",
          "format characters dropped, odd spaces collapsed", (PCD.clean_name(nbsp),))
    import datetime as _dtt
    stale = {70: {"phone": "+15550100170", "phone_verified_at": _dtt.datetime(2020, 1, 1, tzinfo=_dtt.timezone.utc)},
             71: {"phone": "+15550100171", "phone_verified_at": _dtt.datetime(2020, 1, 1, tzinfo=_dtt.timezone.utc)}}
    pool = Pool()
    await PCD.reverify_stale_numbers(pool, stale)
    check(stale[70]["phone"] == "+15550100170" and stale[71]["phone"] is None
          and any(q.startswith("UPDATE peer_cards SET phone = NULL") and a == (71,) for q, a in pool.execs),
          "a stale number still theirs is kept; one no longer theirs is cleared", pool.execs)

    print("4h. the re-check never stalls a card read; a host grant needs a HOST contact")
    import time as _time_mod
    async def slow_bridge(op, params):
        await asyncio.sleep(10)
        return {}
    CT._bridge_op = slow_bridge
    many = {100 + i: {"phone": f"+1555010{i:04d}", "phone_verified_at": None} for i in range(25)}
    t0 = _time_mod.monotonic()
    await PCD.reverify_stale_numbers(Pool(), many)
    took = _time_mod.monotonic() - t0
    check(took < PCD.REVERIFY_DEADLINE_S + 1 and all(r["phone"] for r in many.values()),
          "a hung bridge costs at most the deadline, and unanswered numbers are left as they were",
          round(took, 2))
    CT._bridge_op = fake_bridge
    q = []

    class QPool:
        async def fetchval(self, sql, *a):
            q.append(" ".join(sql.split()))
            return None
    await MH._grant_admissible(QPool(), "endpoint_grant", 666)
    check(q and "kind = 'host'" in q[-1], "a host grant is checked against HOST contacts only", q[-1:])

    print("4d. the card travels only on an accepted connection")
    from server.bridge import memory_handlers as M
    from server.peer import handlers as H
    check("card" in H._ALLOWED_GRANT_KEYS, "a grant may carry it (peer lane allow-list)")
    src = inspect.getsource(M.handle_inbox_append)
    knock_src = inspect.getsource(H.inbox_request)
    check("card" not in H._ALLOWED_PAYLOAD_KEYS and "store_peer_card" not in knock_src,
          "a bare connection request can never carry one")
    acc = inspect.getsource(CT.accept_request_service)
    check('grant_payload["card"] = mine' in acc, "accepting a host sends ours")
    rec = inspect.getsource(M.reciprocate_grant)
    check("card=mine" in rec and '"card": mine' in rec, "the reciprocal slip sends ours, on both lanes")

    print("5. no 'entity' on a card")
    peerings[:] = [{"peer_entity_id": 50, "peer_host_name": "z.cseries.localhost", "peer_handle": "z", "current": True}]
    blob = json.dumps(await C.host_cards(Pool(), ME, [50, 51]))
    check("entity" not in blob.lower(), "the word never appears in a card", blob[:300])

    print("6. the route")
    from server.routes import host_cards as R
    paths = {getattr(r, "path", "") for r in R.router.routes}
    check({"/api/hosts/cards", "/api/hosts/{host_id}/card"} <= paths, "both routes exist", paths)
    src = inspect.getsource(R)
    check(src.count('requires_permission("read:address_book")') == 2,
          "both read under read:address_book")
    main_src = open(os.path.join(_ROOT, "server", "main.py")).read()
    check("app.include_router(host_cards_routes.router)" in main_src, "registered in main")

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): " + "; ".join(FAILS))
        return 1
    print("ok  one resolver, from this Host's own records, and it says Host")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
