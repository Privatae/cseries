#!/usr/bin/env python3
"""asyncpg binds `$n::timestamptz` / `$n::date` / `$n::interval` only from a
datetime / date / timedelta — a str raises DataError at execute time, which no
fake pool can see. Found when a live test run of a peer group showed
the owner refusing every vote with "could not store the vote": store_vote bound
the signed terms' ISO string. (The interval twins were fixed the same way.)

Pinned, through the real functions with a pool that REJECTS a str where
Postgres would (the check asyncpg makes, not the one a fake usually skips):
  1. store_vote binds signed_at as a datetime, and refuses an unparseable one;
  2. accept_vote refuses an unparseable signed_at as FINAL, before any store,
     even on the unpinned-key path that skips verify_vote's own time check;
  3. the calendar day aggregate binds a date, and refuses a malformed one.

Measured on PG 18 as well: the fixed store_vote stores a vote
(seq 1790122257889877) and the aggregate answers for a given day.

Container-free: APP_ROOT=<repo> python3 scripts/test_typed_binds.py
"""
import asyncio
import datetime as _dt
import os
import re
import sys

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain")]

FAILS = []


def check(cond, msg, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + msg + (f"\n        {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(msg)


_TYPED = re.compile(r"\$(\d+)::(timestamptz|timestamp|date|time|interval)\b")
_WANT = {"timestamptz": _dt.datetime, "timestamp": _dt.datetime,
         "date": _dt.date, "time": _dt.time, "interval": _dt.timedelta}


class StrictPool:
    """Raises, like asyncpg does, when a typed placeholder gets the wrong type."""
    def __init__(self, row=None):
        self.calls, self.row = [], row

    def _check(self, sql, args):
        for n, kind in _TYPED.findall(sql):
            v = args[int(n) - 1]
            if v is not None and not isinstance(v, _WANT[kind]):
                raise TypeError(f"DataError: ${n}::{kind} got {type(v).__name__}")
        self.calls.append((" ".join(sql.split()), args))

    async def execute(self, sql, *a):
        self._check(sql, a)
        return "INSERT 0 1"

    async def fetchrow(self, sql, *a):
        self._check(sql, a)
        return self.row

    async def fetchval(self, sql, *a):
        self._check(sql, a)
        return None

    async def fetch(self, sql, *a):
        self._check(sql, a)
        return []


async def main() -> int:
    from server.groups import roster as R
    from server.groups import lane as L
    from server.bridge import memory_handlers as M
    from server import brain_service

    GID = "grp-163-" + "0" * 32
    vote = {"terms": {"v": 1, "kind": "group_vote", "group": GID, "ballot": "b1", "voter": 164,
                      "choice": "yes", "seq": 1790122257889877, "nonce": "ab" * 16,
                      "signed_at": "2026-09-23T00:10:55.749841+00:00"},
            "signature": "eA==", "key_id": "entity_sign"}

    print("1. store_vote")
    pool = StrictPool()
    try:
        await R.store_vote(pool, GID, "b1", vote, verified=True, verify_error=None)
        args = pool.calls[-1][1]
        check(isinstance(args[6], _dt.datetime) and args[6].tzinfo is not None,
              "signed_at is bound as an aware datetime", args[6])
    except Exception as e:
        check(False, "signed_at is bound as an aware datetime", f"{type(e).__name__}: {e}")
    bad = {**vote, "terms": {**vote["terms"], "signed_at": "yesterday"}}
    try:
        await R.store_vote(StrictPool(), GID, "b1", bad, verified=True, verify_error=None)
        check(False, "an unparseable signed_at is refused, not bound")
    except ValueError:
        check(True, "an unparseable signed_at is refused, not bound")

    print("2. accept_vote refuses it as final, before storing (unpinned-key path)")
    ballot = {"group_id": GID, "ballot_id": "b1", "choices": ["yes", "no"],
              "closes_at": _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=1),
              "closed_at": None}

    async def get_ballot(pool, g, b):
        return ballot
    real_get = R.get_ballot
    R.get_ballot = get_ballot
    stored = []
    real_store = R.store_vote

    async def rec_store(*a, **k):
        stored.append(a)
    R.store_vote = rec_store
    try:
        out = await L.accept_vote(StrictPool(row={"verify_pubkey": None}), GID, "b1", bad)
        check(out.get("ok") is False and "signed_at" in (out.get("error") or "")
              and not out.get("retryable") and not stored,
              "refused with a final error, nothing stored", out)
        out = await L.accept_vote(StrictPool(row={"verify_pubkey": None}), GID, "b1", vote)
        check(out.get("ok") is True and stored, "a well-formed unpinned vote is still stored", out)
    finally:
        R.get_ballot, R.store_vote = real_get, real_store

    print("3. the calendar day aggregate")
    pool = StrictPool(row={"prs": 0, "artifacts": 0, "apps_built": 0, "apps_installed": 0,
                           "research": 0, "perms": 0, "cost_usd": 0.0, "t_in": 0, "t_out": 0,
                           "total_events": 0})
    brain_service.get_pool = lambda: pool
    brain_service._entity_id = 9003
    r = await M.handle_calendar_aggregate_day({"date": "2026-09-22"})
    bound = [a for s, a in pool.calls if "$2::date" in s]
    check(r.get("ok") and bound and bound[0][1] == _dt.date(2026, 9, 22),
          "a given day binds a date", (r, bound[:1]))
    r = await M.handle_calendar_aggregate_day({"date": "not-a-date"})
    check(r.get("ok") is False and "YYYY-MM-DD" in (r.get("error") or ""),
          "a malformed day is refused with a reason", r)

    print("4. the roster sync with a REMOVED member (the owner's snapshot is JSON)")
    import server.groups.roster as R2
    members = [{"member_entity_id": 163, "role": "creator", "verify_pubkey": None,
                "display_name": "", "removed_at": None},
               {"member_entity_id": 165, "role": "member", "verify_pubkey": None,
                "display_name": "", "removed_at": "2026-09-20T10:00:00+00:00"},
               {"member_entity_id": 164, "role": "member", "verify_pubkey": None,
                "display_name": "", "removed_at": None}]
    pool = StrictPool(row=None)
    try:
        out = await R2.apply_roster_snapshot(pool, group_id=GID, owner_entity_id=9163, name="g",
                                             description="", roster_epoch=2, members=members)
        ins = [a for q, a in pool.calls if q.startswith("INSERT INTO group_members")]
        check(out.get("applied") and len(ins) == 3 and isinstance(ins[1][5], _dt.datetime),
              "every member applies, removed_at bound as a datetime", out)
    except Exception as e:
        check(False, "every member applies, removed_at bound as a datetime", f"{type(e).__name__}: {e}")

    print("5. a reach-out channel with a quiet window")
    from server.reachout import service as RS
    pool = StrictPool(row={"id": 1, "name": "n", "kind": "log", "cooldown_s": 120,
                           "created_by": "host", "created_at": _dt.datetime.now(_dt.timezone.utc)})
    try:
        await RS.add_channel(pool, 3, name="n", kind="log", action={"type": "log"},
                             quiet_from="22:00", quiet_to="07:00", quiet_tz="UTC")
        ins = [a for q, a in pool.calls if "INSERT INTO reachout_channels" in q]
        check(ins and ins[0][5] == _dt.time(22, 0) and ins[0][6] == _dt.time(7, 0),
              "quiet hours bind as times", ins[:1])
    except Exception as e:
        check(False, "quiet hours bind as times", f"{type(e).__name__}: {e}")
    for bad, why in ((("25:00", "07:00"), "a time like"), (("22:00", None), "needs both"),
                     (("22:00+05:00", "07:00"), "without an offset")):
        try:
            await RS.add_channel(StrictPool(), 3, name="n", kind="log", action={"type": "log"},
                                 quiet_from=bad[0], quiet_to=bad[1])
            check(False, f"quiet {bad} is refused")
        except RS.ReachoutError as e:
            check(why in str(e), f"quiet {bad} is refused with a reason", e)

    print("6. a malformed thread id is a 400, not a 500")
    import types as _t
    sys.modules.setdefault("server.main", _t.ModuleType("server.main"))
    from server.routes import messages as MS
    try:
        await MS.thread_service(StrictPool(), 3, "not-a-uuid")
        check(False, "refused as a MessageError(400)")
    except MS.MessageError as e:
        check(e.status == 400, "refused as a MessageError(400)", e.status)

    print("7. the vote refusal is FINAL at the sender (through peer_server's wrap + classify)")
    from server.peer.app_lane import classify
    absurd = {**vote, "terms": {**vote["terms"], "signed_at": "0001-01-01T00:00:00+14:00"}}
    R.get_ballot = get_ballot
    try:
        out = await L.accept_vote(StrictPool(row={"verify_pubkey": None}), GID, "b1", absurd)
    finally:
        R.get_ballot = real_get
    wrapped = {"ok": False, "receiver": True, "retryable": bool(out.get("retryable")),
               "error": out.get("error") or "receiver refused", "data": out}
    status, _ = classify(wrapped, None)
    check(out.get("ok") is False and status == "refused",
          "a signed_at Postgres cannot hold is refused for good, not retried", (out, status))

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): " + "; ".join(FAILS))
        return 1
    print("ok  typed placeholders get typed values")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
