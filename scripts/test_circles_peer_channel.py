#!/usr/bin/env python3
"""The peer channel in Circles — open while the peering is live, and unclearable.

The Circles channel is always open for bastion peers.

Two behaviours, both against a REAL Postgres because both are SQL:

  1. A host we share a machine with appears in Circles → Hosts BEFORE anything
     has been said. It has to be LISTED rather than greeted: a host↔host DM is
     slip-gated (the receiver treats a bridge-signed slip as the sole authority
     for message_kind='message' and refuses without one, messages.py:227-236),
     and a peer we just met holds no slip — so an auto-greeting would land in
     our thread and bounce off theirs, leaving a conversation that looks
     two-sided and is not.

  2. Clearing that thread is REFUSED while the peering is live, because the
     Hosts list filters `archived_at IS NULL` (init-db.sql:1815 declares the
     column) and archiving would hide a Host you are still sharing a machine
     with.

Run (the gates SKIP without a database, and a skip proves nothing):

    eval "$(scripts/peer_test_db.sh)"     # starts a throwaway PG, exports PEER_TEST_DSN
    python3 scripts/test_circles_peer_channel.py
    scripts/peer_test_db.sh --stop

The old recipe here (docker run -p 15499:5432 ... cseries-postgres:smoke) could
never work: that image is unix-socket-only BY DESIGN — entrypoint-postgres.sh
writes listen_addresses = '' on every boot — so the published port mapped to a
closed door and this gate skipped silently. peer_test_db.sh reaches it over its
unix socket instead, with no TCP port at all. Measured 2026-09-07: it had never run.
"""
from __future__ import annotations

import asyncio
import os
import sys

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(__file__), "..")
# server.routes.* pulls in server.main, which imports the brain modules bare
# (ceigas_domain_controller, mastercode.*) — so brain/ has to be on the path too.
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain"), os.path.join(_ROOT, "system-apps"), os.path.join(_ROOT, "scripts")]

import asyncpg  # noqa: E402

DSN = os.environ.get("PEER_TEST_DSN")     # peer_test_db.sh's socket DSN; there is no TCP default

failures: list[str] = []
notes: list[str] = []


def check(cond, msg):
    if cond:
        notes.append("  PASS  " + msg)
    else:
        failures.append(msg)


ME, PEER = 42, 4242


async def main(dsn: str) -> int:
    # The KERNEL's schema, on a fresh clone (scripts/kernel_schema.py): every
    # table below is the one the migrations build, never a copy.
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3, timeout=10)

    async with pool.acquire() as c:
        await c.execute(
            "INSERT INTO mcp_client_tokens (id, entity_id, name, token_hash, url_handle, operator_kind) "
            "VALUES (7, $1, 'desk', '\\x00'::bytea, 'desk-7', 'bastion')", ME)
        await c.execute(
            "INSERT INTO network_bastion_heartbeat (token_id, hostname) VALUES (7, 'desktop-01')")

    from server.network import peer_discovery
    import server.routes.messages as msgs
    # circles.py does `from server import brain_service, main as server_main` at
    # module scope, and server.main mounts /app/dashboard/base at IMPORT time —
    # a path that exists only inside the container. Importing the whole app to
    # reach one pure helper would test the container layout, not this feature.
    # A stub module satisfies the import; nothing under test touches it.
    import types
    _stub_main = types.ModuleType("server.main")
    _stub_main.app = None
    sys.modules.setdefault("server.main", _stub_main)
    import server.routes.circles as circles
    import server.bridge.memory_handlers as mh

    async def fake_resolve(to_entity_id, to_handle, *, not_found_status):
        return PEER
    msgs._resolve_target_service = fake_resolve

    async def fake_inbox(payload):
        return {"ok": True}
    mh.handle_inbox_append = fake_inbox

    # brain_service.get_pool() returns the pod's brain pool (brain_service.py:181);
    # the circles helpers call it, so point it at the test database.
    class Svc:
        @staticmethod
        def get_pool():
            return pool
    circles.brain_service = Svc()

    await peer_discovery.record_and_notify(
        pool, 7, "desktop-01",
        [{"host_name": "beta.host.example.test", "handle": "beta", "since": ""}])

    # The heartbeat row's peers array, as topology.upsert_heartbeat writes it
    # from the same beat: the box names beta and reports the pairing's state.
    import json

    async def report(*entries):
        async with pool.acquire() as c:
            await c.execute("UPDATE network_bastion_heartbeat SET peers = $1::jsonb WHERE token_id = 7",
                            None if entries == (None,) else json.dumps(list(entries)))

    BETA = "beta.host.example.test"

    def live_peer_for(peerings, peer_id):
        """The lookup delete_thread's archive guard uses for kind='entity'.
        Missing (an older peer_discovery), it answers a placeholder that every
        check below reads as wrong, so the run fails instead of stopping."""
        find = getattr(peer_discovery, "requestable_peering", None)
        return find(peerings, peer_id) if find else {"missing": "requestable_peering"}

    # 0. Named, but not as an active pairing: not listed. A peer listed here is
    #    one the user can send a connection request to, and the request route
    #    refuses every one of these.
    for entries, what in (([{"host_name": BETA, "state": "pending"}], "a pending pairing"),
                          ([{"host_name": BETA, "state": "revoked"}], "a revoked pairing"),
                          ([{"host_name": BETA}], "a pairing named with no state"),
                          ([{"host_name": BETA, "state": "active"},
                            {"host_name": BETA, "state": "revoked"}],
                           "a pairing named both active and revoked"),
                          ([None], "a box that never reported peers")):
        await report(*entries)
        check(await circles._list_bastion_peers_without_thread(ME, set()) == [],
              f"{what} is not listed in Circles")
        check(live_peer_for(await peer_discovery.list_peerings(pool, ME), PEER) is None,
              f"{what} does not hold the channel open (the archive guard's lookup finds nothing)")

    await report({"host_name": BETA, "state": "active"})

    # 1. The peer is in the Hosts list with NO messages exchanged.
    items = await circles._list_bastion_peers_without_thread(ME, set())
    check(len(items) == 1, f"a live peering appears in Hosts with no messages (got {len(items)})")
    if items:
        it = items[0]
        check(it["id"] == str(PEER), f"the entry is keyed by the peer's entity id (got {it['id']})")
        check(it["kind"] == "entity", "the entry is a host thread, not a person")
        check(it.get("peer") is True, "the entry is marked as a peering for the UI")
        check("desktop-01" in it["preview"],
              f"the preview says WHY they are listed, naming the machine: {it['preview']!r}")

    # 2. A peer we ALREADY have a thread with is not duplicated.
    dupe = await circles._list_bastion_peers_without_thread(ME, {str(PEER)})
    check(dupe == [], f"a peer with an existing thread is not listed twice (got {len(dupe)})")

    # 3. A peering with no resolved entity id cannot be opened, so it is not listed.
    async with pool.acquire() as c:
        await c.execute("UPDATE network_bastion_peer SET peer_entity_id = NULL")
    check(await circles._list_bastion_peers_without_thread(ME, set()) == [],
          "an unresolved peering is not listed (it could not be opened)")
    async with pool.acquire() as c:
        await c.execute("UPDATE network_bastion_peer SET peer_entity_id = $1", PEER)

    # 4. A DORMANT peering is history, not a live channel — Network's business.
    async with pool.acquire() as c:
        await c.execute("UPDATE network_bastion_peer SET last_seen_at = now() - INTERVAL '9 days'")
    check(await circles._list_bastion_peers_without_thread(ME, set()) == [],
          "a dormant peering drops out of Circles (it belongs in Network)")

    listed = await peer_discovery.list_peerings(pool, ME)
    check(len(listed) == 1 and listed[0]["current"] is False,
          "the dormant peering is KEPT and labelled, never deleted")

    # 5. The archive guard reads the same liveness the list does.
    async with pool.acquire() as c:
        await c.execute("UPDATE network_bastion_peer SET last_seen_at = now()")

    live = live_peer_for(await peer_discovery.list_peerings(pool, ME), PEER)
    check(live is not None, "a live peering is found by the archive guard's predicate")
    check(live is not None and live.get("introduced_by") == "desktop-01",
          "the refusal can name the machine to revoke")

    async with pool.acquire() as c:
        await c.execute("UPDATE network_bastion_peer SET last_seen_at = now() - INTERVAL '9 days'")
    gone = live_peer_for(await peer_discovery.list_peerings(pool, ME), PEER)
    check(gone is None, "once the peering goes dormant the thread clears normally")

    # 6. Revoking the bastion ends the peering outright — the escape hatch the
    #    refusal message points the user at has to actually work.
    async with pool.acquire() as c:
        await c.execute("UPDATE network_bastion_peer SET last_seen_at = now()")
        await c.execute("UPDATE mcp_client_tokens SET revoked_at = now() WHERE id = 7")
    after_revoke = await peer_discovery.list_peerings(pool, ME)
    check(after_revoke == [],
          f"revoking the bastion ends the peering, so the thread can be cleared (got {len(after_revoke)})")

    await pool.close()
    for line in notes:
        print(line)
    if failures:
        print(f"\nFAIL — {len(failures)} problem(s):")
        for f in failures:
            print("  · " + f)
        return 1
    print(f"\nok — {len(notes)} checks passed against a real Postgres")
    return 0


if __name__ == "__main__":
    from kernel_schema import kernel_schema_db, server_reachable
    if not server_reachable(DSN):
        print(f"SKIP — no test database at {DSN or 'PEER_TEST_DSN (not set)'}.  Start one:  eval \"$(scripts/peer_test_db.sh)\"")
        sys.exit(2)
    with kernel_schema_db(DSN) as _db:
        sys.exit(asyncio.run(main(_db)))
