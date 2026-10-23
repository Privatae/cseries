#!/usr/bin/env python3
"""record_other_bastions against a REAL Postgres — the alert lifecycle.

Why a real database: every decision this function makes is expressed in SQL —
the upsert, the `NOT (uid = ANY(...))` gone-marking, and the CASE that clears
notified_kind when a finding comes back. A stub pool would assert control flow
against a fiction, which is why the peer gates are infra-only for the same
reason.

The only stubs are the two OUTBOUND surfaces: `_inbox` (pairing_grants.py:544,
returns bool — True only on a confirmed write) and `_host_message` (:522,
returns None). Both are recorded so the test can assert what the user is told.
`ensure_tables` (:140) creates the tables this drives.

Covers: first raise, silence on a repeat, re-raise on escalation, gone-marking,
RE-RAISE AFTER REINSTALL (a bastion removed and installed again was
permanently silent), the daily reminder, the member-host branch, the local-mode
branch, "did not look", and a refused inbox write.
"""
import asyncio
import datetime as dt
import os
import sys

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain"), os.path.join(_ROOT, "scripts")]

import asyncpg  # noqa: E402

DSN = os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not DSN:
    print(f"SKIP — no test database. Start one:  bash {os.path.join(_ROOT, 'scripts/peer_test_db.sh')}")
    sys.exit(2)

from server.network import pairing_grants as pg  # noqa: E402

EID, TOKEN, MACHINE = 9003, 7, "box-a"
FAILS = []
INBOX: list = []
HOSTMSG: list = []


def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (("\n        " + str(extra)) if extra else ""))
    if not cond:
        FAILS.append(label)


def finding(kind, uid=1000, user="eve", pairings=0):
    return {"kind": kind, "user": user, "uid": uid, "pairings": pairings,
            "state_dir": f"/home/{user}/.config/cseries-bastion", "binary_path": "",
            "last_change": "2026-09-12T05:00:00Z"}


async def main(dsn):
    # The KERNEL's network_bastion_other, on a fresh clone of the schema the
    # migrations build (scripts/kernel_schema.py) — never a copy of it.
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3)

    async def fake_inbox(subject, body, payload):
        INBOX.append({"subject": subject, "body": body, "payload": payload})
        return True

    async def fake_host_message(content, action):
        HOSTMSG.append({"content": content, "action": action})

    pg._inbox = fake_inbox
    pg._host_message = fake_host_message

    async def run(items, *, is_anchor=True, mode="networked"):
        INBOX.clear()
        HOSTMSG.clear()
        return await pg.record_other_bastions(
            pool, EID, TOKEN, MACHINE, items, is_anchor=is_anchor, mode=mode)

    print("\n── 1. a finding is raised once ──")
    r = await run([finding("state")])
    check("raised exactly once", r["raised"] == 1 and r["escalated"] == 0, r)
    check("the inbox names the machine", bool(INBOX) and MACHINE in INBOX[0]["subject"], INBOX)
    check("the host is told", bool(HOSTMSG) and HOSTMSG[0]["action"] == "other_bastion", HOSTMSG)
    row = await pool.fetchrow("SELECT kind, notified_kind, gone_at FROM network_bastion_other")
    check("notified_kind records how far along it was", row["notified_kind"] == "state", dict(row))

    print("\n── 2. the SAME finding on the next beat says nothing ──")
    r = await run([finding("state")])
    check("no second alert", r["raised"] == 0 and r["escalated"] == 0 and not INBOX, (r, INBOX))

    print("\n── 3. it gets further along — that IS news ──")
    r = await run([finding("paired", pairings=2)])
    check("re-raised as an escalation", r["escalated"] == 1, r)
    check("the body says it is serving Hosts",
          bool(INBOX) and "serving 2 Host" in INBOX[0]["body"],
          INBOX[0]["body"][:160] if INBOX else None)
    row = await pool.fetchrow("SELECT notified_kind FROM network_bastion_other")
    check("notified_kind moved up the ladder", row["notified_kind"] == "paired", dict(row))

    print("\n── 4. it goes away — recorded, never deleted ──")
    r = await run([])
    check("nothing raised for an empty list", r["raised"] == 0 and not INBOX, (r, INBOX))
    row = await pool.fetchrow("SELECT gone_at, notified_kind FROM network_bastion_other")
    check("the row is marked gone, not removed",
          row is not None and row["gone_at"] is not None, dict(row) if row else None)

    print("\n── 5. FABLE B1: installed AGAIN — the user must hear ──")
    r = await run([finding("state")])
    check("a reinstall is raised again", r["raised"] == 1, r)
    check("and the user actually gets a message", bool(INBOX), INBOX)
    row = await pool.fetchrow("SELECT gone_at, notified_kind FROM network_bastion_other")
    check("gone_at cleared and the ladder reset to the new kind",
          row["gone_at"] is None and row["notified_kind"] == "state", dict(row))

    print("\n── 6. the daily reminder, while one is actually serving ──")
    await run([finding("paired", pairings=1)])          # escalate to paired
    r = await run([finding("paired", pairings=1)])      # same again, immediately
    check("no reminder before the window", r["raised"] == 0 and not INBOX, (r, INBOX))
    await pool.execute("UPDATE network_bastion_other SET raised_at = $1, notified_at = $1",
                       dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2))
    r = await run([finding("paired", pairings=1)])
    check("reminded once the window passes", r["raised"] == 1, r)
    check("and it reads as a standing finding",
          bool(INBOX) and "Still there" in INBOX[0]["body"],
          INBOX[0]["body"][:120] if INBOX else None)

    print("\n── 7. a host that does NOT own the machine: told once, never nagged ──")
    await pool.execute("DELETE FROM network_bastion_other")
    r = await run([finding("paired", pairings=1)], is_anchor=False, mode="networked")
    check("told once", r["raised"] == 1, r)
    check("and told the anchor decides",
          bool(INBOX) and "decides what happens" in INBOX[0]["body"],
          INBOX[0]["body"][:200] if INBOX else None)
    await pool.execute("UPDATE network_bastion_other SET raised_at = $1, notified_at = $1",
                       dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2))
    r = await run([finding("paired", pairings=1)], is_anchor=False, mode="networked")
    check("no daily reminder for a member host", r["raised"] == 0 and not INBOX, (r, INBOX))

    print("\n── 8. FABLE B3: a LOCAL box's single host owns the machine ──")
    await pool.execute("UPDATE network_bastion_other SET raised_at = $1, notified_at = $1",
                       dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2))
    r = await run([finding("paired", pairings=1)], is_anchor=False, mode="local")
    check("a local box IS reminded", r["raised"] == 1, r)
    check("and is told to settle it, not that somebody else will",
          bool(INBOX) and "yours to settle" in INBOX[0]["body"],
          INBOX[0]["body"][:200] if INBOX else None)

    print("\n── 9. 'did not look' touches nothing ──")
    before = await pool.fetchval("SELECT count(*) FROM network_bastion_other")
    gone_before = await pool.fetchval(
        "SELECT count(*) FROM network_bastion_other WHERE gone_at IS NOT NULL")
    r = await run(None)
    after = await pool.fetchval("SELECT count(*) FROM network_bastion_other")
    gone_after = await pool.fetchval(
        "SELECT count(*) FROM network_bastion_other WHERE gone_at IS NOT NULL")
    check("None is a no-op",
          r == {"raised": 0, "escalated": 0, "cleared": 0} and before == after and not INBOX,
          (r, before, after))
    check("and it does NOT mark anything gone — absent is not 'clean'",
          gone_after == gone_before, (gone_before, gone_after))

    print("\n── 10. a refused inbox write leaves no mark, and retries ──")
    await pool.execute("DELETE FROM network_bastion_other")

    async def refusing_inbox(subject, body, payload):
        INBOX.append({"subject": subject})
        return False

    pg._inbox = refusing_inbox
    r = await run([finding("state")])
    check("nothing counted as raised", r["raised"] == 0, r)
    row = await pool.fetchrow("SELECT notified_kind, notified_at FROM network_bastion_other")
    check("the row is NOT marked notified — so the next beat tries again",
          row["notified_kind"] is None and row["notified_at"] is None, dict(row))
    check("no host message was written either", not HOSTMSG, HOSTMSG)
    pg._inbox = fake_inbox
    r = await run([finding("state")])
    check("the retry does raise", r["raised"] == 1, r)

    await pool.close()
    print(f"\nFAILURES: {len(FAILS)}")
    return 1 if FAILS else 0


from kernel_schema import kernel_schema_db  # noqa: E402
with kernel_schema_db(DSN, entity_id=EID) as _db:
    sys.exit(asyncio.run(main(_db)))
