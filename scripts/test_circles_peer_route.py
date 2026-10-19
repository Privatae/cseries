#!/usr/bin/env python3
"""The peer-channel ROUTE, over real HTTP, from where the browser stands.

scripts/test_circles_peer_channel.py drives the helper functions. That is not
the same thing as the endpoint: a route has a status code, an error envelope and
a permission dependency, and Circles calls it over HTTP. This mounts the real
`circles.router` (circles.py:40) on a real ASGI app and issues real requests
against a real Postgres.

What it pins — DELETE /api/circles/thread/entity/{peer}:

  A peering may hold open an EMPTY channel. It may never lock a real
  conversation.

    409 while the peering is live AND the thread is empty, with an envelope
        naming the machine to revoke. The Hosts list filters archived_at IS
        NULL (init-db.sql:1815), so archiving would hide a Host you are still
        sharing a machine with. Refused, never a silent no-op.
    200 when the thread has real messages, EVEN while the peering is live —
        peer_entity_id comes from a handle the BASTION supplies, so refusing on
        any live peering would let a hostile box name a conversation the user
        can then never clear. The peering re-appears afterwards through
        _list_bastion_peers_without_thread, so the channel is still open.
    200 once the peering goes dormant.

Run (the gates SKIP without a database, and a skip proves nothing):

    eval "$(scripts/peer_test_db.sh)"     # starts a throwaway PG, exports PEER_TEST_DSN
    python3 scripts/test_circles_peer_route.py
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
import types

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(__file__), "..")
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
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4, timeout=10)

    async with pool.acquire() as c:
        await c.execute(
            "INSERT INTO mcp_client_tokens (id, entity_id, name, token_hash, url_handle, operator_kind) "
            "VALUES (7, $1, 'desk', '\\x00'::bytea, 'desk-7', 'bastion')", ME)
        await c.execute(
            "INSERT INTO network_bastion_heartbeat (token_id, hostname) VALUES (7, 'desktop-01')")
        # NO messages yet. The guard's rule is that a peering may hold open an
        # EMPTY channel and may never lock a real conversation, so the empty
        # case is the one that must refuse.

    # circles.py does `from server import brain_service, main as server_main`
    # (circles.py:34) at module scope, and server.main mounts /app/dashboard/base
    # at IMPORT time — a container path. Stub it; nothing under test touches it.
    _stub_main = types.ModuleType("server.main")
    _stub_main.app = None
    sys.modules.setdefault("server.main", _stub_main)

    from server.network import peer_discovery
    import server.routes.messages as msgs
    import server.routes.circles as circles
    import server.bridge.memory_handlers as mh

    async def fake_resolve(to_entity_id, to_handle, *, not_found_status):
        return PEER
    msgs._resolve_target_service = fake_resolve

    async def fake_inbox(payload):
        return {"ok": True}
    mh.handle_inbox_append = fake_inbox

    # brain_service.get_pool() (brain_service.py:181) is what the route reads,
    # and _entity_id() returns brain_service._entity_id (circles.py:136-138).
    class Svc:
        _entity_id = ME

        @staticmethod
        def get_pool():
            return pool
    circles.brain_service = Svc()

    await peer_discovery.record_and_notify(
        pool, 7, "desktop-01",
        [{"host_name": "beta.host.example.test", "handle": "beta", "since": ""}])

    # ── a real ASGI app with the REAL router ────────────────────────────
    from fastapi import FastAPI
    import httpx

    app = FastAPI()
    app.include_router(circles.router)

    # requires_permission(perm) builds a NEW closure on each call
    # (middleware.py:201-220), so keying an override on
    # requires_permission("write:circles") would target a DIFFERENT function
    # object than the one circles.py built at import time — the override would
    # silently not apply, every request would come back 401, and that is easy to
    # misread as a route failure. Override the dependency objects the router
    # actually holds: _READ/_WRITE are lists of Depends (circles.py:67-68).
    # Auth is not what this test is for; it is covered where it is defined.
    for dep in list(circles._READ) + list(circles._WRITE):
        app.dependency_overrides[dep.dependency] = lambda: None

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. LIVE peering, EMPTY channel — refused, and it says why. This is the
        #    case this exists for: the channel to a Host you share a machine
        #    with stays open.
        r = await client.request("DELETE", f"/api/circles/thread/entity/{PEER}")
        check(r.status_code == 409,
              f"DELETE on a live peering with no history is refused (got {r.status_code}: {r.text[:200]})")
        blob = r.text
        check("desktop-01" in blob, f"the refusal names the machine to revoke: {blob[:220]}")
        check("peer_channel_open" in blob,
              f"the refusal carries a machine-readable code: {blob[:220]}")

        # 2. LIVE peering, REAL CONVERSATION — must CLEAR, not refuse.
        #
        #    THE FINDING THIS PINS: peer_entity_id is resolved from a handle the
        #    BASTION supplies, so the machine chooses which entity id lands in
        #    the peering. If the guard refused on any live peering, a hostile or
        #    compromised box could announce a "peer" whose handle resolves to
        #    someone the user actually talks to, and the user could never clear
        #    that conversation again — a machine-controlled lock on a user
        #    action, in the surface built to reassure them. Refusing ONLY when
        #    there is nothing to clear keeps the open channel and removes the
        #    lock: the peering re-appears afterwards via
        #    _list_bastion_peers_without_thread, so nothing is lost either way.
        async with pool.acquire() as c:
            await c.execute(
                "INSERT INTO inbox_messages (thread_id, message_kind, from_entity_id, "
                "to_entity_id, from_address, to_address, subject, body) "
                "VALUES (gen_random_uuid(), 'message', $1, $2, 'beta@peer', 'me@host', "
                "'hello', 'hello')",
                PEER, ME)
        r = await client.request("DELETE", f"/api/circles/thread/entity/{PEER}")
        check(r.status_code == 200,
              f"a real conversation clears even while the peering is live (got {r.status_code}: {r.text[:200]})")
        async with pool.acquire() as c:
            archived = await c.fetchval(
                "SELECT count(*) FROM inbox_messages WHERE archived_at IS NOT NULL")
        check(archived == 1,
              f"the conversation really was archived, so the lock cannot exist (archived {archived})")

        # And with the history now archived, the channel is empty again — so the
        # peering holds it open once more, exactly as it did at the start.
        r = await client.request("DELETE", f"/api/circles/thread/entity/{PEER}")
        check(r.status_code == 409,
              f"once cleared, the empty channel is held open again (got {r.status_code})")

        # 3. DORMANT peering — nothing holds the channel open any more, so even
        #    an empty thread clears.
        async with pool.acquire() as c:
            await c.execute(
                "UPDATE network_bastion_peer SET last_seen_at = now() - INTERVAL '9 days'")
        r = await client.request("DELETE", f"/api/circles/thread/entity/{PEER}")
        check(r.status_code == 200,
              f"DELETE on a dormant peering succeeds (got {r.status_code}: {r.text[:200]})")

    await pool.close()
    for line in notes:
        print(line)
    if failures:
        print(f"\nFAIL — {len(failures)} problem(s):")
        for f in failures:
            print("  · " + f)
        return 1
    print(f"\nok — {len(notes)} route checks passed over real HTTP")
    return 0


if __name__ == "__main__":
    from kernel_schema import kernel_schema_db, server_reachable
    if not server_reachable(DSN):
        print(f"SKIP — no test database at {DSN or 'PEER_TEST_DSN (not set)'}.  Start one:  eval \"$(scripts/peer_test_db.sh)\"")
        sys.exit(2)
    with kernel_schema_db(DSN) as _db:
        sys.exit(asyncio.run(main(_db)))
