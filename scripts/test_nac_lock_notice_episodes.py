#!/usr/bin/env python3
"""Gate: every time the network gate locks, the person is told, even the same lock again.

The defect. NACController._tell_lock told each lock once, keyed by the broken rows, and
named the inbox notice by the same key. Two ways a lock went untold:
  - the lock lifts while its notice is still being written; the write then finishes and
    marks the lock told, so when the same lock comes back nothing is written;
  - a lock that lifts and comes back writes a notice with the id of the first one, and
    the inbox (ON CONFLICT (message_id) DO NOTHING) drops it as a copy.
Each episode of a lock now has its own notice. A restart in the same state still names
the first episode the way it always did, so the inbox keeps one copy of that.

Real engine and a real ed25519 key; the inbox is a stand-in keyed by message_id that
drops a repeated id, as the real one does. No pod, no DB.

Run: APP_ROOT=<repo> python3 scripts/test_nac_lock_notice_episodes.py
"""
import asyncio
import base64
import json
import logging
import os
import sys

ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [ROOT, os.path.join(ROOT, "brain")]
logging.disable(logging.CRITICAL)

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from server.network import nac_controller as nc  # noqa: E402

FAILS: list[str] = []


def check(label, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f" — {detail}" if detail != "" and not ok else ""))
    if not ok:
        FAILS.append(label)


KEY = Ed25519PrivateKey.generate()
PUB = KEY.public_key().public_bytes_raw()
TERMS = {"name": "desk", "priority": 1, "match": {}, "enforcement": "allow",
         "permits": [{"direction": "inbound", "lane": "mcp"}]}
GOOD = {"id": 3, "name": "desk", "priority": 1, "match": "{}", "profile_id": 3,
        "signed_terms": json.dumps(TERMS),
        "signature": base64.b64encode(KEY.sign(nc._canonical(TERMS))).decode()}
# The same row with its terms widened after signing: it fails its check and locks.
BAD = dict(GOOD, signed_terms=json.dumps(dict(TERMS, permits=[{"direction": "inbound", "lane": "any"}])))


class Pool:
    def __init__(self):
        self.rows = [GOOD]

    async def fetch(self, q, *a):
        return list(self.rows) if "FROM nac_policy" in q else []


class Inbox:
    """Stores a notice once per message_id; a repeated id is dropped, as the real inbox
    does (handle_inbox_append: ON CONFLICT (message_id) DO NOTHING)."""

    def __init__(self):
        self.stored: dict[str, dict] = {}
        self.calls = 0
        self.hold: asyncio.Event | None = None

    async def __call__(self, notice):
        self.calls += 1
        if self.hold is not None:
            await self.hold.wait()
        self.stored.setdefault(notice["message_id"], notice)
        return {"ok": True}


def engine(pool, inbox):
    return nc.NACController(pool=pool, verify_pubkey=PUB, notify=inbox)


async def main():
    # ── 1. a lock that lifts and comes back is told again, and the inbox keeps it ───────
    print("1. the same lock, lifted and back, is a new notice the inbox keeps")
    pool, inbox = Pool(), Inbox()
    nac = engine(pool, inbox)
    pool.rows = [BAD]
    for _ in range(3):
        await nac._load(force=True)
    check("locked and told once over three reloads", nac._tamper and len(inbox.stored) == 1,
          (nac._tamper, len(inbox.stored)))
    first_id = next(iter(inbox.stored))
    pool.rows = [GOOD]
    await nac._load(force=True)
    check("the restored rule lifts the lock", nac._tamper is False)
    pool.rows = [BAD]
    await nac._load(force=True)
    await nac._load(force=True)
    check("the lock's return is a second notice the inbox keeps (a new id)",
          len(inbox.stored) == 2, list(inbox.stored))

    # ── 2. the lock lifts while its notice is being written ─────────────────────────────
    print("2. the lock lifts while its notice is being written, then comes back")
    pool, inbox = Pool(), Inbox()
    nac = engine(pool, inbox)
    inbox.hold = asyncio.Event()
    pool.rows = [BAD]
    writing = asyncio.create_task(nac._load(force=True))
    for _ in range(50):
        if inbox.calls:
            break
        await asyncio.sleep(0)
    check("the first notice is being written", inbox.calls == 1, inbox.calls)
    pool.rows = [GOOD]
    await nac._load(force=True)               # the lock lifts meanwhile
    inbox.hold.set()
    await writing                             # ...and the first notice finishes
    check("the first notice was stored", len(inbox.stored) == 1, len(inbox.stored))
    pool.rows = [BAD]
    await nac._load(force=True)               # the same lock comes back
    check("the lock's return is told, not taken as already told",
          len(inbox.stored) == 2 and inbox.calls == 2, (inbox.calls, list(inbox.stored)))

    # ── 3. a restart in the same state is still the same notice ─────────────────────────
    print("3. a restart in the same state names the first notice again")
    inbox = Inbox()
    again = engine(pool, inbox)
    await again._load(force=True)
    check("a new process in the same lock writes the first episode's id (the inbox drops it)",
          list(inbox.stored) == [first_id], (list(inbox.stored), first_id))


asyncio.run(main())
print(f"{'PASS' if not FAILS else 'FAIL'}: every lock episode is told"
      + (f" — {len(FAILS)} failing: {FAILS}" if FAILS else ""))
sys.exit(1 if FAILS else 0)
