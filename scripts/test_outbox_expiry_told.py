#!/usr/bin/env python3
"""Gate: a Circles send given up after OUTBOX_TTL_DAYS is TOLD to the sender.

group_outbox and peer_app_outbox rows still queued after 7 days turn
'expired' (server/groups/lane.py tick, server/peer/app_lane.py tick). The TTL
is in line with mail-queue norms and stays; what was missing is the sender
hearing about it — the row changed status where nobody looks. Now each tick
that expires rows puts one notice in this Host's inbox naming what was given
up and to whom, and enqueue returns when a send will be given up on.

Stub pool and inbox; no DB, no peer. Run: APP_ROOT=<repo> python3 scripts/test_outbox_expiry_told.py
"""
import asyncio
import datetime as dt
import os
import sys
import types
import uuid
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "brain"))
sys.path.insert(0, str(ROOT))
sys.modules.setdefault("server.main", types.ModuleType("server.main"))
FAILS: list[str] = []


def check(label, cond, detail=""):
    print(f"[outbox-expiry] {'ok  ' if cond else 'FAIL'} {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


from server.peer import app_lane as AL  # noqa: E402
from server.groups import lane as GL  # noqa: E402
import server.bridge.memory_handlers as MH  # noqa: E402

told: list[dict] = []


async def _append(msg):
    told.append(msg)


MH.handle_inbox_append = _append
EXP = dt.datetime(2026, 10, 9, tzinfo=dt.timezone.utc)


class Pool:
    def __init__(self, expired):
        self.expired = expired

    async def fetch(self, sql, *a):
        if sql.lstrip().upper().startswith("UPDATE") and "expired" in sql:
            return list(self.expired) if "RETURNING" in sql else []
        return []

    async def execute(self, sql, *a):
        return "UPDATE 0"

    async def fetchrow(self, sql, *a):
        return {"id": 1, "status": "queued", "message_id": uuid.uuid4(), "attempts": 0,
                "expires_at": EXP}


async def main():
    rows = [{"id": 1, "what": "chess", "peer_entity_id": 9128, "op": "app.call",
             "message_id": uuid.uuid4()},
            {"id": 2, "what": "chess", "peer_entity_id": 9055, "op": "app.call",
             "message_id": uuid.uuid4()}]
    await AL.tick(Pool(rows))
    check("an app-lane tick that expires sends tells the sender once, naming them",
          len(told) == 1 and "Host 9128" in told[0]["body"] and "Host 9055" in told[0]["body"]
          and f"{AL.OUTBOX_TTL_DAYS} days" in told[0]["body"], repr(told)[:300])
    told.clear()
    await GL.tick(Pool([{"id": 3, "what": "grp-1", "peer_entity_id": 7, "op": "group.vote",
                         "message_id": uuid.uuid4()}]))
    check("a group-lane tick that expires sends tells the sender too",
          len(told) == 1 and "Host 7" in told[0]["body"], repr(told)[:300])
    told.clear()
    await AL.tick(Pool([]))
    check("a tick that expires nothing tells nobody", told == [])
    row = await AL.enqueue(Pool([]), "chess", 9128, "app.call", "move", {})
    check("enqueue says when the send will be given up on",
          row.get("expires_at") == EXP.isoformat(), repr(row))


asyncio.new_event_loop().run_until_complete(main())
if FAILS:
    print(f"[outbox-expiry] {len(FAILS)} FAILED")
    sys.exit(1)
print("[outbox-expiry] all checks passed")
