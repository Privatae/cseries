#!/usr/bin/env python3
"""Gate — a handle belongs to the FIRST entity that claimed it.

`contacts` is UNIQUE on `handle` alone (ux_contacts_handle,
migrations/v048_circles_contacts_policy.sql:28), and both upsert paths wrote
`peer_entity_id` into their ON CONFLICT branch. So a second entity arriving
under a handle this host already held RE-POINTED the existing contact at itself.

WHY THAT MATTERS, and it is not theoretical. `peer_entity_id` is what the People
send path resolves a person's HOST from — `find_human_contact`'s own docstring
says so: *"the People send path resolves the person's HOST from it
(peer_entity_id) — that is where the message is delivered and whose slip we
present."* Re-point the row and the messages follow.

REACHABILITY, which is what made this worth fixing rather than noting. The grant
path takes the handle straight off the SENDER'S PAYLOAD — for a host grant it is
`body_payload.get("from_handle")` (server/bridge/memory_handlers.py) — and an
`endpoint_grant` is admitted by `domain_policy._REQUEST_KINDS` with no
relationship precondition on the bridge leg. So the handle is not something the
receiver established; it is something the sender chose.

The rule applied is the one this codebase already uses for a value that must not
change under a stable identity: `roster.pin_key`'s first-key-wins, and
`hub_peers.arm_hub_pdp` treating a changed key as an alarm rather than an update.

BOTH CALLERS ARE COVERED HERE, deliberately. This branch is the SECOND caller of
`accept_request_service`; the first is the shipped Circles Accept button, and a
fix that covered only the new one would leave the busier path wrong — the
mistake `inbox_deliver`'s own comment records ("fixing only the new path would
have left the older, busier one wrong").

  1. The SQL guard text is present on EVERY ON CONFLICT branch that writes
     peer_entity_id. Asserted on the source, because the guard is the real
     enforcement and the Python check is only the error message.
  2. `handle_claimed_by` reports the OTHER entity, and says nothing for a free
     handle or for the entity that already holds it.
  3. `accept_request_service` refuses with 409 and names the holder, BEFORE
     writing anything.
  4. `upsert_contact_from_grant` refuses silently and writes nothing — it is
     best-effort by contract and must never fail a delivery.
  5. The ALLOW direction: the legitimate holder re-accepting still works, and a
     handle with no entity bound to it can still be bound. A guard that refuses
     everyone would pass every deny-test and break first contact.
  6. A row that comes back None from the guarded RETURNING is turned into the
     same 409, not an AttributeError.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys

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


class ContactsPool:
    """One contacts table in a dict, answering on the SQL the production code
    actually writes — and APPLYING THE GUARD the way Postgres does, so a guard
    removed from the source stops being honoured here rather than passing."""

    def __init__(self, rows=None):
        # handle -> dict
        self.rows = {r["handle"]: dict(r) for r in (rows or [])}
        self.execs = []
        self._next_id = 100

    def _guarded(self, sql: str) -> bool:
        return "contacts.peer_entity_id IS NULL" in " ".join(sql.split())

    def _upsert(self, sql, handle, peer, **fields):
        existing = self.rows.get(handle)
        if existing is not None:
            held = existing.get("peer_entity_id")
            if self._guarded(sql) and held is not None and peer is not None \
                    and int(held) != int(peer):
                return None            # Postgres: DO UPDATE ... WHERE false
            existing.update({k: v for k, v in fields.items() if v is not None})
            if peer is not None:
                existing["peer_entity_id"] = int(peer)
            return existing
        self._next_id += 1
        row = {"id": self._next_id, "handle": handle,
               "peer_entity_id": None if peer is None else int(peer),
               "display_name": fields.get("display_name"),
               "kind": fields.get("kind", "host"),
               "status": fields.get("status", "active"),
               "notes": None, "last_contact_at": None, "created_at": None}
        self.rows[handle] = row
        return row

    async def fetchval(self, sql, *a):
        self.execs.append((sql, a))
        s = " ".join(sql.split())
        if "SELECT peer_entity_id FROM contacts WHERE handle" in s:
            r = self.rows.get(a[0])
            return None if r is None else r.get("peer_entity_id")
        return None

    async def fetchrow(self, sql, *a):
        self.execs.append((sql, a))
        s = " ".join(sql.split())
        if "INSERT INTO contacts" in s:
            return self._upsert(sql, a[0], a[3], display_name=a[1], kind=a[2],
                                status="pending")
        return None

    async def execute(self, sql, *a):
        self.execs.append((sql, a))
        s = " ".join(sql.split())
        if "INSERT INTO contacts" in s:
            self._upsert(sql, a[0], a[3], display_name=a[1], kind=a[2],
                         status="active")
            return "INSERT 0 1"
        return "INSERT 0 1"


async def main() -> int:
    from server.routes import contacts as C

    src = open(os.path.join(_ROOT, "server", "routes", "contacts.py")).read()

    # ── 1. the guard is on every branch that writes peer_entity_id ─────────
    print("\n1. the SQL guard, which is the real enforcement")
    branches = re.findall(r"ON CONFLICT \(handle\) DO UPDATE(.*?)(?:\"\"\"|RETURNING)",
                          src, re.S)
    writes_peer = [b for b in branches if "peer_entity_id" in b]
    check(len(writes_peer) >= 2,
          "both upsert paths write peer_entity_id in their ON CONFLICT branch",
          len(writes_peer))
    check("_HANDLE_CLAIM_GUARD" in src, "the guard constant exists")
    guarded = src.count("_HANDLE_CLAIM_GUARD")
    check(guarded >= 3,
          "and it is APPLIED at both call sites as well as declared once",
          f"{guarded} references")
    check("contacts.peer_entity_id IS NULL" in src
          and "contacts.peer_entity_id = EXCLUDED.peer_entity_id" in src,
          "the guard admits an unbound handle and the SAME entity, and nothing else")

    # ── 2. handle_claimed_by ──────────────────────────────────────────────
    print("\n2. who already holds the handle")
    pool = ContactsPool([{"handle": "alice", "peer_entity_id": 101}])
    check(await C.handle_claimed_by(pool, "alice", 999) == 101,
          "a handle held by ANOTHER entity reports that entity")
    check(await C.handle_claimed_by(pool, "alice", 101) is None,
          "a handle held by the SAME entity is not a conflict")
    check(await C.handle_claimed_by(pool, "nobody", 999) is None,
          "a free handle is not a conflict")
    check(await C.handle_claimed_by(pool, "alice", None) is None,
          "no entity id means nothing to conflict with")

    # ── 3. accept_request_service refuses, and writes NOTHING first ───────
    print("\n3. the accept path")
    pool = ContactsPool([{"handle": "alice", "peer_entity_id": 101}])
    raised = None
    try:
        await C.accept_request_service(pool, handle="alice", from_entity_id=999,
                                       kind="host")
    except C.ContactsError as e:
        raised = e
    check(raised is not None and raised.status == 409,
          "accepting under a handle another entity holds is refused with 409",
          raised.detail if raised else "ACCEPTED")
    check(raised is not None and "101" in raised.detail,
          "and the refusal NAMES the holder rather than refusing anonymously",
          raised.detail if raised else "")
    check(pool.rows["alice"]["peer_entity_id"] == 101,
          "the existing contact is untouched — not re-pointed",
          pool.rows["alice"]["peer_entity_id"])
    wrote = [s for s, _a in pool.execs
             if "INSERT INTO contacts" in " ".join(s.split())]
    check(not wrote, "and nothing was written at all", wrote)

    # ── 4. the grant path refuses SILENTLY and writes nothing ─────────────
    print("\n4. the grant path (the sender-chosen handle)")
    pool = ContactsPool([{"handle": "alice", "peer_entity_id": 101}])
    out = await C.upsert_contact_from_grant(
        pool, handle="alice", kind="host", display_name="Mallory",
        peer_entity_id=999)
    check(out is None, "it returns None rather than raising — it is best-effort "
                       "by contract and must never fail a delivery", out)
    check(pool.rows["alice"]["peer_entity_id"] == 101,
          "the contact still names the first entity",
          pool.rows["alice"]["peer_entity_id"])
    check(pool.rows["alice"].get("display_name") != "Mallory",
          "and the sender's display name did not land either")
    wrote = [s for s, _a in pool.execs
             if "INSERT INTO contacts" in " ".join(s.split())]
    check(not wrote, "nothing was written", wrote)

    # ── 5. THE ALLOW DIRECTION ────────────────────────────────────────────
    # A guard that refuses everyone passes every deny-test above and breaks
    # first contact. Both legitimate shapes must still work.
    print("\n5. the allow direction")
    pool = ContactsPool([{"handle": "alice", "peer_entity_id": 101}])
    await C.upsert_contact_from_grant(pool, handle="alice", kind="host",
                                      display_name="Alice", peer_entity_id=101)
    check(pool.rows["alice"]["peer_entity_id"] == 101,
          "the entity that ALREADY holds the handle is still admitted")
    wrote = [s for s, _a in pool.execs
             if "INSERT INTO contacts" in " ".join(s.split())]
    check(wrote, "and its upsert actually ran")

    pool = ContactsPool([{"handle": "bob", "peer_entity_id": None}])
    await C.upsert_contact_from_grant(pool, handle="bob", kind="host",
                                      display_name="Bob", peer_entity_id=202)
    check(pool.rows["bob"]["peer_entity_id"] == 202,
          "a handle with NO entity bound to it can still be bound — a contact "
          "the user created by hand is not a claim by anybody",
          pool.rows["bob"]["peer_entity_id"])

    pool = ContactsPool()
    await C.upsert_contact_from_grant(pool, handle="carol", kind="host",
                                      display_name="Carol", peer_entity_id=303)
    check(pool.rows.get("carol", {}).get("peer_entity_id") == 303,
          "and a brand-new handle is created normally")

    # ── 6. a raced refusal is a 409, not an AttributeError ────────────────
    # The check in Python is check-then-write; the SQL guard is what actually
    # holds. When the guard fires, RETURNING gives no row — and the first cut
    # would have dereferenced None.
    print("\n6. the raced refusal")

    class RacingPool(ContactsPool):
        """Reports the handle free, then refuses the write — the window between
        the check and the INSERT."""

        def __init__(self):
            super().__init__([{"handle": "alice", "peer_entity_id": 101}])
            self._lied = False

        async def fetchval(self, sql, *a):
            s = " ".join(sql.split())
            if "SELECT peer_entity_id FROM contacts WHERE handle" in s and not self._lied:
                self._lied = True
                return None            # "free" — the race
            return await super().fetchval(sql, *a)

    raced = None
    try:
        await C.accept_request_service(RacingPool(), handle="alice",
                                       from_entity_id=999, kind="host")
    except C.ContactsError as e:
        raced = e
    except Exception as e:
        raced = e
    check(isinstance(raced, C.ContactsError) and raced.status == 409,
          "a refusal that only the SQL guard catches is still a clean 409",
          f"{type(raced).__name__}: {raced}")

    print(f"\n{CHECKS} checks, {len(FAILS)} failure(s)")
    for f in FAILS:
        print("  FAILED: " + f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
