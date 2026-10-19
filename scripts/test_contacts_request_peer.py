#!/usr/bin/env python3
"""Exercise POST /api/contacts/request-peer from the CALLER's position:
a real ASGI app, the real router, real HTTP, a real Postgres.

Only the two things outside this pod are stubbed — the bridge (inbox.send goes
to another entity) and auth. Everything else is the shipping code path."""
import asyncio, os, sys, types, json
_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain"), os.path.join(_ROOT, "scripts")]
import asyncpg

DSN = os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not DSN:
    print(f"SKIP — no test database. Start one:  bash {os.path.join(_ROOT, 'scripts/peer_test_db.sh')}")
    sys.exit(2)

ME, PEER, STRANGER = 3, 122, 999
FAILS = []
def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (("\n        " + str(extra)) if extra else ""))
    if not cond: FAILS.append(label)

async def main(dsn):
    # The KERNEL's schema, on a fresh clone (scripts/kernel_schema.py): contacts,
    # the bastion token, its heartbeat and its peerings are the tables the
    # migrations build, never a copy.
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    async with pool.acquire() as c:
        await c.execute("INSERT INTO mcp_client_tokens (id, entity_id, name, token_hash, url_handle, "
                        "operator_kind) VALUES (7, 3, 'desk', '\\x00'::bytea, 'desk-7', 'bastion')")
        # the box is STILL announcing a1b2… (current) and has stopped announcing dead…
        await c.execute("""INSERT INTO network_bastion_heartbeat (token_id, hostname, peers)
            VALUES (7, 'WORKSTATION-1', $1::jsonb)""",
            json.dumps([{"host_name": "a1b2c3d4e5f60718.host.example.test"}]))

    _stub = types.ModuleType("server.main"); _stub.app = None
    _stub.get_bridge_client = lambda: None
    sys.modules.setdefault("server.main", _stub)

    from server.network import peer_discovery
    import server.routes.contacts as contacts

    # request_peer_service does `from server import brain_service` INSIDE the
    # function, so it reads the REAL module — patching contacts.brain_service
    # does not reach it. Set the real one (this is what initialize() does in a
    # live pod) and patch the module-level reference the route helpers use.
    from server import brain_service as real_bs
    real_bs._entity_id = ME
    real_bs._pool = pool

    class Svc:
        _entity_id = ME
        @staticmethod
        def get_pool(): return pool
    contacts.brain_service = Svc()

    sent = []
    async def fake_bridge(op, params):
        sent.append((op, params))
        return {"message_id": "msg-1"}
    contacts._bridge_op = fake_bridge

    async with pool.acquire() as c:
        # one CURRENT peering (announced now) and one DORMANT (no longer announced)
        await c.execute("""INSERT INTO network_bastion_peer
            (token_id, peer_host_name, peer_handle, peer_entity_id)
            VALUES (7,'a1b2c3d4e5f60718.host.example.test','a1b2c3d4e5f60718',122),
                   (7,'dead00000000beef.host.example.test','dead00000000beef',777)""")

    from fastapi import FastAPI
    import httpx
    app = FastAPI()
    app.include_router(contacts.router)
    # This FastAPI version keeps an included router as a single _IncludedRouter
    # object, so app.routes does NOT contain the individual routes and walking it
    # overrides nothing — the requests then come back 401 and that reads like a
    # route failure. Walk the ROUTER's own routes. Auth is not what this
    # exercises; it is covered where it is defined.
    for r in contacts.router.routes:
        for d in getattr(getattr(r, "dependant", None), "dependencies", []) or []:
            if d.call is not None:
                app.dependency_overrides[d.call] = lambda: None

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        print("\n── 1. the happy path: a host that shares my machine ──")
        r = await client.post("/api/contacts/request-peer", json={"peer_entity_id": PEER})
        check(f"POST returns 200 (got {r.status_code})", r.status_code == 200, r.text[:300])
        if r.status_code == 200:
            b = r.json()
            check("it names the peer + the shared machine", b.get("machine") == "WORKSTATION-1", b)
            check("an endpoint_request went to the bridge",
                  sent and sent[-1][0] == "inbox.send"
                  and sent[-1][1]["message_kind"] == "endpoint_request"
                  and sent[-1][1]["to_entity_id"] == PEER, sent[-1] if sent else None)
            check("the request says WHY it arrived (via=bastion_peer)",
                  sent[-1][1]["payload"]["via"] == "bastion_peer", sent[-1][1]["payload"])
            row = await pool.fetchrow("SELECT handle, kind, status FROM contacts")
            check("a pending host contact was recorded",
                  row and row["kind"] == "host" and row["status"] == "pending", dict(row) if row else None)

        print("\n── 2. THE SECURITY PROPERTY: a stranger's id is refused ──")
        before = len(sent)
        r = await client.post("/api/contacts/request-peer", json={"peer_entity_id": STRANGER})
        check(f"a non-peer entity id is refused 404 (got {r.status_code})", r.status_code == 404, r.text[:200])
        check("and NOTHING was sent to the bridge", len(sent) == before)

        print("\n── 3. a DORMANT peering does not qualify ──")
        before = len(sent)
        r = await client.post("/api/contacts/request-peer", json={"peer_entity_id": 777})
        check(f"a peering the machine stopped announcing is refused (got {r.status_code})",
              r.status_code == 404, r.text[:200])
        check("and NOTHING was sent", len(sent) == before)

        print("\n── 4. self ──")
        r = await client.post("/api/contacts/request-peer", json={"peer_entity_id": ME})
        check(f"this host refuses to request itself (got {r.status_code})", r.status_code == 400, r.text[:200])

        print("\n── 5. FABLE B2: a human contact of the same name is NOT rewritten ──")
        # peer_handle is refreshed on every beat while peer_entity_id resolves
        # once, so a machine can swap the handle underneath a live peering. With
        # contacts unique on handle alone, the old upsert flipped the user's
        # HUMAN contact of that name to kind='host' — deleting them from Circles
        # > People with no trace. Two fixes: the handle can no longer BE an
        # address (topology._narrow_handle), and this can no longer change what a
        # contact IS.
        async with pool.acquire() as c:
            await c.execute("DELETE FROM contacts")
            await c.execute("INSERT INTO contacts (handle, kind, status, display_name) "
                            "VALUES ('alice@example.com', 'human', 'active', 'Alice')")
            # the machine now claims that address as its peer's handle
            await c.execute("UPDATE network_bastion_peer SET peer_handle = 'alice@example.com' "
                            "WHERE peer_entity_id = $1", PEER)
        r = await client.post("/api/contacts/request-peer", json={"peer_entity_id": PEER})
        check(f"the request still goes out (got {r.status_code})", r.status_code == 200, r.text[:200])
        row = await pool.fetchrow("SELECT kind, status, display_name FROM contacts "
                                  "WHERE handle = 'alice@example.com'")
        check("Alice is STILL a human contact",
              row and row["kind"] == "human", dict(row) if row else None)
        check("...still active, and still named",
              row and row["status"] == "active" and row["display_name"] == "Alice",
              dict(row) if row else None)
        n = await pool.fetchval("SELECT count(*) FROM contacts")
        check("and no host contact was invented under her address", n == 1, n)

        print("\n── 6. the DIRECT peer lane is tried FIRST, bridge only as fallback ──")
        # The request used to go bridge-only, and the bridge cannot route between its
        # own DaemonSet pods — so first contact was impossible whenever the peer
        # landed on a different one (503 "entity N not connected"). contacts.py now
        # knocks the peer lane first. These three pin each branch.
        import server.peer.peer_client as PCM

        async def ok_knock(peer_entity_id, params, **kw):
            calls.append(("peer", peer_entity_id)); return {"ok": True, "data": {"message_id": "peer-1"}}

        # TWO refusal shapes, and they must behave DIFFERENTLY (review: the old
        # test used a shape the server never produces and certified fallback on it).
        async def lane_refusing_knock(peer_entity_id, params, **kw):
            # No `receiver` key = the LANE refused (older pod, unknown op, bad
            # frame). The receiver stored nothing, so the bridge is a real retry.
            calls.append(("peer", peer_entity_id)); return {"ok": False, "error": "unknown op 'inbox.request'"}

        async def receiver_refusing_knock(peer_entity_id, params, **kw):
            # `receiver: True` = the FAR SIDE answered no. The bridge would hand
            # the same message to the same pod and the same gate, so retrying is
            # the same answer twice, not a fallback.
            calls.append(("peer", peer_entity_id))
            return {"ok": False, "receiver": True, "error": "a endpoint_request from you is already waiting"}

        async def dead_knock(peer_entity_id, params, **kw):
            calls.append(("peer", peer_entity_id)); raise PCM.PeerUnavailable("connect refused")

        async with pool.acquire() as c:
            await c.execute("UPDATE network_bastion_peer SET peer_handle='a1b2c3d4e5f60718' "
                            "WHERE peer_entity_id=$1", PEER)

        calls = []
        PCM.knock_peer = ok_knock
        before = len(sent)
        r = await client.post("/api/contacts/request-peer", json={"peer_entity_id": PEER})
        check(f"peer lane succeeds -> 200 (got {r.status_code})", r.status_code == 200, r.text[:200])
        check("...the peer lane was used", ("peer", PEER) in calls, calls)
        check("...and the BRIDGE was not called at all", len(sent) == before, sent[before:])

        calls = []; PCM.knock_peer = lane_refusing_knock; before = len(sent)
        r = await client.post("/api/contacts/request-peer", json={"peer_entity_id": PEER})
        check(f"LANE refusal -> still 200 via bridge (got {r.status_code})", r.status_code == 200, r.text[:200])
        check("...the bridge picked it up", len(sent) > before, (before, len(sent)))

        calls = []; PCM.knock_peer = receiver_refusing_knock; before = len(sent)
        r = await client.post("/api/contacts/request-peer", json={"peer_entity_id": PEER})
        # 409, not 502: the far side ANSWERED. The commonest answer is "one is
        # already waiting", which is a conflict — and a cold pod replying "pool
        # not ready" is not the person refusing, so the wording must not say so.
        check(f"RECEIVER refusal -> surfaced as a conflict, not retried (got {r.status_code})",
              r.status_code == 409, r.text[:200])
        check("...and it is not worded as the PERSON refusing",
              "refused the request" not in r.text, r.text[:200])
        check("...the receiver's own words reach the user",
              "already waiting" in r.text, r.text[:200])
        check("...and the bridge was NOT called — it is the same gate",
              len(sent) == before, sent[before:])

        calls = []; PCM.knock_peer = dead_knock; before = len(sent)
        r = await client.post("/api/contacts/request-peer", json={"peer_entity_id": PEER})
        check(f"peer unreachable -> still 200 via bridge (got {r.status_code})", r.status_code == 200, r.text[:200])
        check("...the bridge picked it up", len(sent) > before, (before, len(sent)))

        print("\n── 7. junk ──")
        r = await client.post("/api/contacts/request-peer", json={"peer_entity_id": "abc"})
        check(f"a non-numeric id is refused (got {r.status_code})", r.status_code in (400, 422), r.text[:200])

    await pool.close()
    print(f"\nFAILURES: {len(FAILS)}")
    return 1 if FAILS else 0

from kernel_schema import kernel_schema_db  # noqa: E402
with kernel_schema_db(DSN, entity_id=ME) as _db:
    sys.exit(asyncio.run(main(_db)))
