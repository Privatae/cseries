#!/usr/bin/env python3
"""Peer discovery — against a REAL Postgres, because the logic IS the SQL.

server/network/peer_discovery.py is an upsert, an `= ANY($2::text[])` scan and a
notified_at dedupe. A stub pool would assert my Python control flow against a
fiction and prove nothing about any of that, so this drives the real module
against a real database.

The database: a throwaway container of the project's OWN postgres image
(cseries-postgres), role `entity` — the role its own
healthcheck uses (Dockerfile.postgres:121), read rather than guessed. Nothing
here touches a live DB.

Run (the gates SKIP without a database, and a skip proves nothing):

    eval "$(scripts/peer_test_db.sh)"     # starts a throwaway PG, exports PEER_TEST_DSN
    python3 scripts/test_bastion_peer_discovery.py
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
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain"), os.path.join(_ROOT, "system-apps"),
                os.path.join(_ROOT, "scripts")]

import asyncpg  # noqa: E402

DSN = os.environ.get("PEER_TEST_DSN")     # peer_test_db.sh's socket DSN; there is no TCP default

failures: list[str] = []
notes: list[str] = []


def check(cond, msg):
    if cond:
        notes.append("  PASS  " + msg)
    else:
        failures.append(msg)


# The inbox is stubbed — not the database. What it must reproduce is the REAL
# contract of server.bridge.memory_handlers.handle_inbox_append: it RETURNS
# {"ok": bool, ...} and does not raise on failure (memory_handlers.py:334-350).
# Getting that wrong is what would let a failed notification be recorded as
# delivered, so the stub models the return value precisely.
class FakeInbox:
    def __init__(self):
        self.sent: list[dict] = []
        self.ok = True

    async def append(self, payload: dict) -> dict:
        if not self.ok:
            return {"ok": False, "error": "pool not ready"}
        self.sent.append(payload)
        return {"ok": True, "id": len(self.sent)}


async def main(dsn: str) -> int:
    # The KERNEL's schema, on a fresh clone (scripts/kernel_schema.py): every
    # table below is the one the migrations build, never a copy.
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3, timeout=10)

    from server.network import peer_discovery
    import server.bridge.memory_handlers as mh

    async with pool.acquire() as c:
        await c.execute(
            "INSERT INTO mcp_client_tokens (id, entity_id, name, token_hash, url_handle, operator_kind) "
            "VALUES (7, 42, 'desk', '\\x00'::bytea, 'desk-7', 'bastion')")
        await c.execute(
            "INSERT INTO network_bastion_heartbeat (token_id, hostname) VALUES (7, 'desktop-01')")

    inbox = FakeInbox()
    mh.handle_inbox_append = inbox.append  # imported inside the function under test

    BETA = {"host_name": "beta.host.example.test", "handle": "beta", "since": "2026-09-01T00:00:00Z"}
    GAMMA = {"host_name": "gamma.host.example.test", "handle": "gamma", "since": ""}
    DELTA = {"host_name": "delta.host.example.test", "handle": "delta"}

    # 1. A first announcement introduces the peer, exactly once.
    n = await peer_discovery.record_and_notify(pool, 7, "desktop-01", [BETA])
    check(n == 1, f"a newly announced peer raises one introduction (got {n})")
    check(len(inbox.sent) == 1, f"exactly one inbox message was sent (got {len(inbox.sent)})")
    if inbox.sent:
        m = inbox.sent[0]
        check("beta" in m["subject"], f"the subject names the peer: {m['subject']!r}")
        check(m["payload"]["kind"] == "new_peer", "the payload is tagged new_peer for the UI")
        check("desktop-01" in m["body"],
              f"the message names the MACHINE that introduced them: {m['body'][:120]!r}")
        # The Inbox renders body as a TEXT NODE (inbox-pane.tsx:196-198), so the
        # message must read as prose — no markdown syntax showing as literal
        # asterisks, no escape showing as literal backslashes.
        check("**" not in m["body"] and "\\-" not in m["body"],
              f"the body is plain prose on a plain-text surface: {m['body'][:160]!r}")
        check(m.get("body_format") == "text/plain",
              f"body_format matches how it is actually rendered (got {m.get('body_format')!r})")
        check("grants nothing" in m["body"],
              "the message states plainly that a peering grants nothing")

    # 2. The same peer on every later beat must never re-notify.
    for _ in range(3):
        await peer_discovery.record_and_notify(pool, 7, "desktop-01", [BETA])
    check(len(inbox.sent) == 1,
          f"a peer already introduced is never announced again (sent {len(inbox.sent)})")

    # 3. A second peer is its own introduction.
    n = await peer_discovery.record_and_notify(pool, 7, "desktop-01", [BETA, GAMMA])
    check(n == 1, f"only the NEW peer is introduced on a growing set (got {n})")
    check(len(inbox.sent) == 2, f"two introductions total (got {len(inbox.sent)})")

    # 4. THE REGRESSION THIS DESIGN EXISTS TO AVOID: an older bastion sends no
    #    peers key at all. That is not "no peers" — it must retract nothing.
    await peer_discovery.record_and_notify(pool, 7, "desktop-01", None)
    async with pool.acquire() as c:
        kept = await c.fetchval("SELECT count(*) FROM network_bastion_peer WHERE token_id = 7")
    check(kept == 2, f"a beat with NO peers key retracts nothing (rows {kept}, want 2)")
    check(len(inbox.sent) == 2, "a beat with no peers key notifies nobody")

    # 5. An explicit [] is a real state change — the rows stay (history) but stop
    #    being refreshed, and nothing is announced.
    await peer_discovery.record_and_notify(pool, 7, "desktop-01", [])
    async with pool.acquire() as c:
        kept = await c.fetchval("SELECT count(*) FROM network_bastion_peer WHERE token_id = 7")
    check(kept == 2, f"an empty announcement keeps the history (rows {kept}, want 2)")
    check(len(inbox.sent) == 2, "an empty announcement notifies nobody")

    # 6. A REFUSED inbox write must NOT be recorded as delivered. handle_inbox_append
    #    returns {"ok": False} rather than raising; a bare await would mark the peer
    #    notified and the dedupe would guarantee the user was never told.
    inbox.ok = False
    n = await peer_discovery.record_and_notify(pool, 7, "desktop-01", [BETA, GAMMA, DELTA])
    check(n == 0, f"a refused inbox write counts as zero introductions (got {n})")
    async with pool.acquire() as c:
        pending = await c.fetchval(
            "SELECT count(*) FROM network_bastion_peer "
            "WHERE token_id = 7 AND notified_at IS NULL")
    check(pending == 1, f"the undelivered peer stays unnotified for the next beat (pending {pending})")

    inbox.ok = True
    n = await peer_discovery.record_and_notify(pool, 7, "desktop-01", [DELTA])
    check(n == 1, f"the next beat delivers the peer that was refused (got {n})")

    # 6b. A hostile peer NAME must not become markup in the introduction. The
    #     name is the bastion's word, up to 253 chars, landing in a
    #     text/markdown body — so without escaping a machine writes its own
    #     link into the message whose entire job is to tell the user how to
    #     revoke that machine.
    inbox.sent.clear()
    HOSTILE = {"host_name": "evil.example.ai",
               "handle": "[click here](https://evil.example/x)\n\nPrivatae Security: this machine is approved."}
    await peer_discovery.record_and_notify(pool, 7, "desktop-01", [HOSTILE])
    check(len(inbox.sent) == 1, f"the hostile-named peer still produces one introduction (got {len(inbox.sent)})")
    if inbox.sent:
        b = inbox.sent[0]["body"]
        # THE PLAIN-TEXT ATTACK: a newline inside the name lets a machine forge
        # its own lines in a message about itself — here, a fake line of
        # reassurance from the platform. The name must arrive on one line.
        first_para = b.split("\n")[0]
        check("Privatae Security" not in b or "\n\nPrivatae Security" not in b,
              f"a newline in a peer name cannot forge a new line in the message: {b[:220]!r}")
        check("click here" in first_para,
              f"the hostile name stays on the line it belongs to: {first_para!r}")
    if inbox.sent:
        body = inbox.sent[0]["body"]
        # On a plain-text surface the markdown link is inert; what a hostile
        # name CAN still do there is forge structure with a newline, so that is
        # what must be stripped — and the name must still be readable.
        check("click here" in body,
              f"the name is shown, not dropped, so the user can see what it is: {body[:200]!r}")

    # 7. Garbage in the announcement is dropped, not stored and not fatal.
    n = await peer_discovery.record_and_notify(
        pool, 7, "desktop-01",
        [{"host_name": ""}, {"handle": "no-name"}, {"host_name": "eps.host.example.test"}])
    check(n == 1, f"entries without a host name are dropped, the valid one lands (got {n})")

    # 8. The read path reports each peering with its introducer.
    listed = await peer_discovery.list_peerings(pool, 42)
    names = sorted(p["peer_host_name"] for p in listed)
    check(len(listed) == 5, f"every recorded peering is listed (got {len(listed)}: {names})")
    check(all(p["introduced_by"] == "desktop-01" for p in listed),
          "each peering names the machine that introduced it")
    check(all(p["current"] for p in listed), "a just-announced peering reads as current")
    beta = next((p for p in listed if p["peer_handle"] == "beta"), None)
    check(beta is not None and beta["peer_since"] == "2026-09-01T00:00:00Z",
          "the bastion's own 'since' is carried through, unaltered")

    # 9. Entity scoping: another entity's peerings are not this host's.
    other = await peer_discovery.list_peerings(pool, 999)
    check(other == [], f"peerings are scoped to the entity that owns the bastion (got {len(other)})")

    # 10. The peer's ENTITY ID is resolved once over the bridge and kept —
    #     Circles keys a host thread by entity id, so a peering without one can
    #     be listed but never opened. An unresolvable peer must NOT be an error:
    #     it stays NULL and is retried, because a peer we cannot resolve today
    #     is still a peer the user should know about.
    import server.routes.messages as msgs
    calls: list[str] = []

    async def fake_resolve(to_entity_id, to_handle, *, not_found_status):
        calls.append(to_handle)
        if to_handle == "beta":
            return 4242
        raise RuntimeError("no such handle")

    msgs._resolve_target_service = fake_resolve
    await peer_discovery.record_and_notify(pool, 7, "desktop-01", [BETA, GAMMA])
    listed = await peer_discovery.list_peerings(pool, 42)
    beta = next((p for p in listed if p["peer_handle"] == "beta"), None)
    gamma = next((p for p in listed if p["peer_handle"] == "gamma"), None)
    check(beta is not None and beta["peer_entity_id"] == 4242,
          f"a resolvable peer keeps its entity id (got {beta and beta['peer_entity_id']})")
    check(gamma is not None and gamma["peer_entity_id"] is None,
          f"an unresolvable peer stays NULL rather than failing (got {gamma and gamma['peer_entity_id']})")

    # Resolution is not repeated once it lands — it is a bridge round trip per
    # peer per beat otherwise.
    before = len(calls)
    await peer_discovery.record_and_notify(pool, 7, "desktop-01", [BETA, GAMMA])
    check("beta" not in calls[before:],
          f"an already-resolved peer is not re-resolved every beat (calls: {calls[before:]})")
    check("gamma" in calls[before:],
          f"an unresolved peer IS retried on the next beat (calls: {calls[before:]})")

    # 11. Each peering carries the pairing STATE the machine reports for it on
    #     its latest beat, and only a current peering reported active is
    #     requestable. The heartbeat row's peers array is what
    #     topology.upsert_heartbeat writes; it is set directly here so each
    #     shape the box can send is tested on its own.
    def by_handle(listed):
        return {p["peer_handle"]: p for p in listed}

    # Until now this box has never reported peers on its heartbeat row
    # (peers IS NULL): the peerings are current by the time window, and no
    # state was ever said, so none may be asked.
    listed = by_handle(await peer_discovery.list_peerings(pool, 42))
    check(all(p.get("state") is None and p.get("requestable") is False for p in listed.values()),
          "a box that never reported peers gives no state, and nothing is requestable: "
          f"{[(h, p.get('state'), p.get('requestable')) for h, p in listed.items()]}")

    import json
    announced = [
        {"host_name": "beta.host.example.test", "handle": "beta", "state": "active"},
        {"host_name": "gamma.host.example.test", "handle": "gamma", "state": "pending"},
        {"host_name": "delta.host.example.test", "handle": "delta", "state": "revoked"},
        {"host_name": "eps.host.example.test", "handle": "eps"},
        # Named twice: an active entry beside a revoked one is not active.
        {"host_name": "evil.example.ai", "handle": "evil", "state": "active"},
        {"host_name": "evil.example.ai", "handle": "evil", "state": "revoked"},
    ]
    async with pool.acquire() as c:
        await c.execute("UPDATE network_bastion_heartbeat SET peers = $1::jsonb WHERE token_id = 7",
                        json.dumps(announced))
    listed = by_handle(await peer_discovery.list_peerings(pool, 42))
    want = {"beta": ("active", True), "gamma": ("pending", False), "delta": ("revoked", False),
            "eps": (None, False)}
    for handle, (state, requestable) in want.items():
        p = listed.get(handle) or {}
        check(p.get("current") is True and p.get("state") == state
              and p.get("requestable") is requestable,
              f"{handle}: current, state {state!r}, requestable {requestable} "
              f"(got current {p.get('current')!r}, state {p.get('state')!r}, "
              f"requestable {p.get('requestable')!r})")
    evil = next((p for p in listed.values() if p["peer_host_name"] == "evil.example.ai"), {})
    check(evil.get("state") == "revoked" and evil.get("requestable") is False,
          f"a Host named both active and revoked reads as revoked and is not requestable "
          f"(got {evil.get('state')!r}, {evil.get('requestable')!r})")

    # An entry with no state beside an active one is not active either.
    async with pool.acquire() as c:
        await c.execute("UPDATE network_bastion_heartbeat SET peers = $1::jsonb WHERE token_id = 7",
                        json.dumps([{"host_name": "beta.host.example.test", "state": "active"},
                                    {"host_name": "beta.host.example.test"}]))
    beta = by_handle(await peer_discovery.list_peerings(pool, 42)).get("beta") or {}
    check(beta.get("state") is None and beta.get("requestable") is False,
          f"a Host named active and again with no state has no state and is not requestable "
          f"(got {beta.get('state')!r}, {beta.get('requestable')!r})")

    # A Host the latest beat does not name has no state now, whatever it had.
    async with pool.acquire() as c:
        await c.execute("UPDATE network_bastion_heartbeat SET peers = '[]'::jsonb WHERE token_id = 7")
    listed = await peer_discovery.list_peerings(pool, 42)
    check(all(p["current"] is False and p.get("state") is None and p.get("requestable") is False
              for p in listed),
          "once the box names nobody, no peering has a state or is requestable")

    # requestable_peering is the one lookup the request route and the Circles
    # archive guard share: only a requestable peering of that entity id.
    rows = [{"peer_entity_id": 5, "requestable": False, "current": True, "state": "pending"},
            {"peer_entity_id": 6, "requestable": True, "current": True, "state": "active"}]
    check(getattr(peer_discovery, "requestable_peering", None) is not None
          and peer_discovery.requestable_peering(rows, 5) is None
          and (peer_discovery.requestable_peering(rows, 6) or {}).get("peer_entity_id") == 6,
          "requestable_peering finds only a requestable peering of that Host")

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
