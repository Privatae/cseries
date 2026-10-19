#!/usr/bin/env python3
"""A Host's pod row for infra.codebase is a copy of the platform's (2026-10-04).

server/platform_grant_sync.py asks the bridge for this Host's own central level
(the platform.grant bridge op) and writes
it into the pod's entity_domain_access, or deletes the row when the platform
says there is none. Only a definite answer changes anything.

Runs against a fresh clone of the kernel's own schema (scripts/kernel_schema.py
— entrypoint.sh's procedure, every migration), with the bridge stubbed. That
also proves, end to end, that a NEW pod no longer seeds the row for itself.

Needs a database it creates: TEST_DSN / PEER_TEST_DSN, else a docker throwaway.
Exit 0 = pass; 1 = FAIL; 2 = skipped by hand (a gate lane fails instead).
"""
import asyncio
import contextlib
import datetime
import os
import subprocess
import sys
import types

APP_ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_ROOT)
sys.path.insert(0, os.path.join(APP_ROOT, "scripts"))

FAILS: list[str] = []


def check(name, cond, extra=""):
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"[grant-sync] FAIL {name} {extra}")


class _Bridge:
    def __init__(self, envelope, connected=True):
        self.envelope = envelope
        self._connected = connected
        self.calls = []

    async def request_full(self, op, params=None):
        self.calls.append((op, params))
        return self.envelope


async def main(dsn: str) -> int:
    import asyncpg
    from server import platform_grant_sync as pgs

    eid = 9128
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)

    async def row():
        return await pool.fetchrow(
            "SELECT access_level, granted_by FROM entity_domain_access "
            "WHERE entity_id = $1 AND domain_id = 'infra.codebase'", eid)

    try:
        check("a new pod's own seed no longer grants it infra.codebase", await row() is None)
        own = await pool.fetchval(
            "SELECT access_level FROM entity_domain_access WHERE entity_id = $1 AND domain_id = 'core.cognitive'", eid)
        check("...while its own cognition grant is still seeded", own == "execute", own)

        b = _Bridge({"ok": True, "data": {"level": "admin"}})
        out = await pgs.sync_once(pool, b, eid)
        r = await row()
        check("the platform says admin: the pod row is written admin, marked as the platform's",
              r is not None and r["access_level"] == "admin" and r["granted_by"] == "platform", (out, r))
        check("...asking only for its own grant", b.calls == [("platform.grant", {})], b.calls)

        await pgs.sync_once(pool, _Bridge({"ok": True, "data": {"level": "read"}}), eid)
        r = await row()
        check("a changed level follows the platform", r is not None and r["access_level"] == "read", r)

        for label, env, connected in (
                ("an old bridge that does not know the op", {"ok": False, "error": "Unknown operation: platform.grant"}, True),
                ("a bridge outage on the read", {"ok": False, "error": "the platform grant could not be read"}, True),
                ("an answer with no level in it", {"ok": True, "data": {}}, True),
                ("a disconnected bridge", {"ok": True, "data": {"level": None}}, False)):
            out = await pgs.sync_once(pool, _Bridge(env, connected), eid)
            r = await row()
            check(f"{label} changes nothing", r is not None and r["access_level"] == "read"
                  and out.startswith("unchanged"), (out, r))

        out = await pgs.sync_once(pool, _Bridge({"ok": True, "data": {"level": None}}), eid)
        check("the platform says none: the pod row is deleted", await row() is None, out)

        await pool.execute(
            "INSERT INTO entity_domain_access (entity_id, domain_id, access_level, granted_by, grant_reason) "
            "VALUES ($1, 'infra.codebase', 'admin', 'migration:v019', 'self')", eid)
        await pgs.sync_once(pool, _Bridge({"ok": True, "data": {"level": None}}), eid)
        check("an old self-seeded admin row is removed once the platform says none",
              await row() is None)

        # An equal level leaves the row exactly as it was: who granted it and
        # when. A changed level is stamped as the platform's, with the time.
        old = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        await pool.execute(
            "INSERT INTO entity_domain_access (entity_id, domain_id, access_level, granted_by, "
            "grant_reason, updated_at) VALUES ($1, 'infra.codebase', 'admin', 'system', 'seeded', $2)",
            eid, old)

        async def stamped():
            return await pool.fetchrow(
                "SELECT access_level, granted_by, updated_at FROM entity_domain_access "
                "WHERE entity_id = $1 AND domain_id = 'infra.codebase'", eid)

        await pgs.sync_once(pool, _Bridge({"ok": True, "data": {"level": "admin"}}), eid)
        r = await stamped()
        check("an equal level leaves the row as it was (granted_by, updated_at)",
              r["granted_by"] == "system" and r["updated_at"] == old, r)
        await pgs.sync_once(pool, _Bridge({"ok": True, "data": {"level": "read"}}), eid)
        r = await stamped()
        check("a changed level is the platform's and records when it changed",
              r["access_level"] == "read" and r["granted_by"] == "platform" and r["updated_at"] > old, r)

        # A pod that boots before its bridge answers looks again soon (a local
        # check, nothing sent) instead of a full interval later; once the bridge
        # has answered, the loop is back on the interval.
        delays: list[float] = []
        late = _Bridge({"ok": True, "data": {"level": "admin"}}, connected=False)

        async def _sleep(s):
            delays.append(s)
            if len(delays) == 2:
                late._connected = True       # the bridge comes up during the retry wait
            if len(delays) == 3:
                raise asyncio.CancelledError

        real_asyncio = pgs.asyncio
        pgs.asyncio = types.SimpleNamespace(sleep=_sleep, CancelledError=asyncio.CancelledError)
        try:
            await pgs.run_loop(pool, late, eid)
        finally:
            pgs.asyncio = real_asyncio
        check("not connected yet: the next look comes after the startup delay, not the interval",
              delays == [pgs.STARTUP_DELAY_SECONDS, pgs.STARTUP_DELAY_SECONDS, pgs.INTERVAL_SECONDS], delays)
        r = await row()
        check("...and the pass after it mirrored the platform's level",
              r is not None and r["access_level"] == "admin", r)
    finally:
        await pool.close()

    if FAILS:
        print(f"[grant-sync] {len(FAILS)} FAIL")
        return 1
    print("[grant-sync] ALL OK")
    return 0


def _no_database() -> int:
    lane = os.environ.get("GATE_LANE")
    if lane:
        print(f"FAIL — the {lane} lane gave no test server (TEST_DSN/PEER_TEST_DSN) and docker is "
              "unavailable; this gate never writes to a database it did not create.")
        return 1
    print("SKIP — no test server (TEST_DSN/PEER_TEST_DSN) and docker unavailable.")
    return 2


if __name__ == "__main__":
    from kernel_schema import kernel_schema_db, throwaway_server

    SERVER = os.environ.get("TEST_DSN") or os.environ.get("PEER_TEST_DSN")
    if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        sys.exit(_no_database())
    with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, \
            kernel_schema_db(_srv, entity_id=9128) as _db:
        sys.exit(asyncio.run(main(_db)))
