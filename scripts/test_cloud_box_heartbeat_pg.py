#!/usr/bin/env python3
"""The cloud box heartbeat's backup.paused (vm/cloud/overlay/usr/local/sbin/
cseries-cloud-heartbeat backup_paused) on the REAL schema.

The box asks the Host's database, through psql, whether the newest finished
backup run took no backup for want of a recovery key (no_recipient) or of a
place it may go (no_destination). The runs here are written by
server/host_backup.py's own recorders where it has them (a run not attempted)
and by its own statements otherwise (a run under way, then finished, with the
states it writes: ok, partial, failed), and a restore's row the way
server/host_restore.py writes it. Then the heartbeat's own query and its own
parse are run against them, through psql as the box runs them:

  1. no run yet: not paused;
  2. the newest finished run is no_recipient: paused; no_destination: paused;
  3. a backup finishes ok, partial or failed after it: not paused;
  4. a run still under way (no finished_at) changes nothing;
  5. a restore's row is not a backup run: it changes nothing either way;
  6. a database answer that is neither of the query's own is null.

Needs a test server (TEST_PG_DSN / PEER_TEST_DSN / TEST_DSN), else a throwaway
cseries-postgres through docker.

    python3 scripts/test_cloud_box_heartbeat_pg.py
"""
import asyncio
import contextlib
import datetime as dt
import importlib.util
import json
import os
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "brain"))
sys.path.insert(0, str(REPO / "scripts"))
OVERLAY = REPO / "vm/cloud/overlay"
sys.path.insert(0, str(OVERLAY / "usr/local/lib/cseries-cloud"))

import asyncpg  # noqa: E402

from kernel_schema import kernel_schema_db, throwaway_server  # noqa: E402

SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    print("SKIP-LOUD: no test server (TEST_PG_DSN/PEER_TEST_DSN/TEST_DSN) and docker unavailable — "
          "this gate needs a real database to prove the heartbeat's backup query.")
    sys.exit(2)

loader = SourceFileLoader("cloud_heartbeat", str(OVERLAY / "usr/local/sbin/cseries-cloud-heartbeat"))
spec = importlib.util.spec_from_loader("cloud_heartbeat", loader)
hb = importlib.util.module_from_spec(spec)
loader.exec_module(hb)

from server import host_backup  # noqa: E402

FAILED = 0


def check(label, cond, detail=""):
    global FAILED
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  <- {str(detail)[:400]}"))
    if not cond:
        FAILED += 1


def paused_on(dsn):
    """backup_paused() with the box's command run against `dsn`: the same SQL,
    through psql with the same -Atqc, and the heartbeat's own parse."""
    def quiet(cmd, timeout=20):
        assert cmd[:3] == ["podman", "exec", "cseries-postgres"] and cmd[-2] == "-Atqc", cmd
        r = subprocess.run(["psql", dsn, "-X", "-Atqc", cmd[-1]], capture_output=True, text=True, timeout=60)
        return r.stdout.strip() if r.returncode == 0 else None
    saved = hb._quiet
    hb._quiet = quiet
    try:
        return hb.backup_paused()
    finally:
        hb._quiet = saved


async def backup(pool, state, minutes_ago):
    """One backup run the way host_backup._run writes it: a 'running' row, then
    its finish with the state it ended in."""
    rid = await pool.fetchval(
        "INSERT INTO host_backup_runs (reason, state, stored, recipients, started_at) "
        "VALUES ('schedule', 'running', 'local', '[]'::jsonb, now() - ($1::text || ' minutes')::interval) "
        "RETURNING id", str(minutes_ago))
    if state != "running":
        await pool.execute(
            "UPDATE host_backup_runs SET finished_at = now() - ($3::text || ' minutes')::interval, state = $2 "
            "WHERE id = $1", rid, state, str(max(minutes_ago - 1, 0)))
    return rid


async def run(dsn):
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        check("1 no run yet: not paused", paused_on(dsn) is False, paused_on(dsn))

        await backup(pool, "ok", 300)
        check("  a backup that finished ok: not paused", paused_on(dsn) is False)

        rec = await host_backup._record_no_recipient(pool, "schedule")
        check("2 the newest finished run is no_recipient (host_backup._record_no_recipient): paused",
              rec["state"] == "no_recipient" and paused_on(dsn) is True, (rec, paused_on(dsn)))

        await backup(pool, "running", 0)
        check("4 a run under way after it changes nothing", paused_on(dsn) is True)
        await pool.execute("DELETE FROM host_backup_runs WHERE state = 'running'")

        await backup(pool, "failed", 0)
        check("3 a backup that failed after it: not paused (it was attempted)", paused_on(dsn) is False)

        rec = await host_backup._record_no_destination(pool, "schedule")
        check("2 the newest finished run is no_destination (host_backup._record_no_destination): paused",
              rec["state"] == "no_destination" and paused_on(dsn) is True, (rec, paused_on(dsn)))

        # A restore's own row, as host_restore writes it on its finish: not a
        # backup, so the pause stands.
        await pool.execute(
            "INSERT INTO host_backup_runs (started_at, finished_at, reason, state, stamp, parts, error) "
            "VALUES ($1::timestamptz, now(), 'restore', $2, $3, $4::jsonb, $5)",
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=2), "ok", "20260102T030405Z",
            json.dumps({}), None)
        check("5 a restore's row after it changes nothing (still paused)", paused_on(dsn) is True)

        await backup(pool, "partial", 0)
        check("3 a backup that saved part of the Host after it: not paused", paused_on(dsn) is False)
        await backup(pool, "ok", 0)
        check("3 a backup that finished ok after it: not paused", paused_on(dsn) is False)

        await pool.execute("DELETE FROM host_backup_runs WHERE reason <> 'restore'")
        await pool.execute("UPDATE host_backup_runs SET state = 'failed'")
        check("5 a restore's row alone is not a run: not paused", paused_on(dsn) is False)

        check("  the states the query names are host_backup's NOT_ATTEMPTED",
              set(host_backup.NOT_ATTEMPTED) == {"no_recipient", "no_destination"}
              and all(f"'{s}'" in hb.BACKUP_PAUSED_SQL for s in host_backup.NOT_ATTEMPTED),
              (host_backup.NOT_ATTEMPTED, hb.BACKUP_PAUSED_SQL))
    finally:
        await pool.close()

    # 6. Only the query's own answers are reported.
    for said, want in [("t", True), ("f", False), ("", None), (None, None), ("true", None),
                       ("t\nf", None), ("x" * 1000, None)]:
        hb._quiet = lambda cmd, timeout=20, said=said: said
        got = hb.backup_paused()
        check(f"6 a database answer of {str(said)[:12]!r} is {want!r}", got is want, got)


with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, \
        kernel_schema_db(_srv) as _db:
    asyncio.run(run(_db))
print("RESULT: ALL PASS" if FAILED == 0 else f"RESULT: {FAILED} FAILED")
sys.exit(1 if FAILED else 0)
