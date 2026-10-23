#!/usr/bin/env python3
"""The inbox LIST names a decision row's decision_id — and only that from the payload.

Circles opens a `decision:<id>` link by finding that decision's message in the
list (circles-web/src/cards/cards-app.tsx: the older ask whose decision_id it
is, opened in full). The list carries no payload, so without this field every
such link opened nothing. Drives the real handler against a pool that records its SQL.

Run: APP_ROOT=<repo> python3 scripts/test_inbox_list_decision_id.py
"""
import asyncio
import os
import sys
from datetime import datetime, timezone

ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [ROOT, os.path.join(ROOT, "brain")]

from server import brain_service  # noqa: E402
from server.bridge import memory_handlers as mh  # noqa: E402

T = datetime(2026, 9, 23, tzinfo=timezone.utc)


def row(kind, decision_id):
    return {"message_id": "m-" + kind, "thread_id": "t", "from_address": "a", "to_address": "b",
            "subject": "s", "body_format": "text/plain", "message_kind": kind, "sent_at": T,
            "delivered_at": None, "read_at": None, "archived_at": None, "from_entity_id": 9003,
            "to_entity_id": 9003, "from_user_email": None, "decision_id": decision_id}


class Pool:
    sql = ""

    async def fetch(self, sql, *p):
        Pool.sql = sql
        return [row("decision", "12"), row("system_notification", None), row("decision", "x")]


def main() -> int:
    brain_service._pool = Pool()
    real = mh._entity_id
    mh._entity_id = lambda: 9003
    try:
        out = asyncio.run(mh.handle_inbox_list({}))
    finally:
        mh._entity_id = real
    msgs = out.get("messages") or out.get("data", {}).get("messages") or []
    fails = []
    if "payload->>'decision_id'" not in Pool.sql:
        fails.append("the list query does not read payload->>'decision_id'")
    if "payload," in Pool.sql.replace(" ", "") .replace("payload->>", "X"):
        fails.append("the list must not carry the whole payload")
    got = [m.get("decision_id") for m in msgs]
    if got != [12, None, None]:
        fails.append(f"decision_id per row: want [12, None, None], got {got}")
    for f in fails:
        print("  FAIL", f)
    print("all checks passed" if not fails else f"FAILED: {len(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
