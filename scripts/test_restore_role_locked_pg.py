#!/usr/bin/env python3
"""Gate: the role a restore loads rows as (server/host_restore.LOAD_ROLE) is
locked again before anything else, after a restore killed in its load.

_load_rows gives host_restore a login, its INSERT grants, a replica setting in
the restore database, and takes them back after the load. A restore killed in
between leaves all of it. So:

  A. every restore starts by locking it (host_restore.stage ->
     lock_load_role): no login, no setting, a member of no role and with no
     member, nothing held in the restore's database;
  B. the database container's boot locks it too, in single-user mode before
     the server listens (entrypoint-postgres.sh lock_restore_role): the
     cseries-postgres image booted twice on one cluster, with this tree's
     entrypoint-postgres.sh, and the role left as a killed load leaves it in
     between.

Each starts from the state a killed load leaves (the role can log in and
write) and checks it can no longer log in after.

A runs on TEST_PG_DSN / PEER_TEST_DSN / TEST_DSN, else a throwaway
cseries-postgres through docker; B needs docker and the image
(PEER_TEST_PG_IMAGE, default cseries-postgres:smoke).
  python3 scripts/test_restore_role_locked_pg.py
"""
import asyncio
import contextlib
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.parse import parse_qsl, urlsplit, urlunsplit

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from kernel_schema import _psql, shared_dir_candidates, throwaway_server  # noqa: E402

SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
HAVE_DOCKER = subprocess.run(["docker", "info"], capture_output=True).returncode == 0
if not SERVER and not HAVE_DOCKER:
    print("SKIP-LOUD: no test server (TEST_PG_DSN/PEER_TEST_DSN/TEST_DSN) and docker unavailable — "
          "this gate needs a real database to prove the restore's role is locked.")
    sys.exit(2)

from server import host_backup as hb  # noqa: E402
from server import host_restore as hr  # noqa: E402

IMAGE = os.environ.get("PEER_TEST_PG_IMAGE", "cseries-postgres:smoke")
ROLE = hr.LOAD_ROLE
FAILED = 0


def check(label, cond, detail=""):
    global FAILED
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  <- {detail}"))
    if not cond:
        FAILED += 1


def with_db(dsn, db, role=None):
    p = urlsplit(dsn)
    netloc = p.netloc if role is None else f"{role}@{p.netloc.rsplit('@', 1)[-1]}"
    return urlunsplit((p.scheme, netloc, "/" + db, p.query, p.fragment))


def sql(dsn, text):
    r = _psql(dsn, "-v", "ON_ERROR_STOP=1", "-Atc", text)
    if r.returncode != 0:
        raise RuntimeError(f"{text[:120]}: {r.stderr[-400:]}")
    return r.stdout.strip()


def killed_load(server, db):
    """The role as a restore killed in the middle of its load leaves it."""
    sql(server, f"""DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}') THEN
            CREATE ROLE {ROLE} LOGIN NOINHERIT BYPASSRLS;
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rig_member') THEN
            CREATE ROLE rig_member NOLOGIN;
        END IF;
      END $$""")
    sql(server, f"ALTER ROLE {ROLE} LOGIN")
    sql(server, f'ALTER ROLE {ROLE} IN DATABASE "{db}" SET session_replication_role = replica')
    sql(server, f"GRANT pg_read_all_data TO {ROLE}")
    sql(server, f"GRANT {ROLE} TO rig_member")
    # The table and the schema it sits in are the gate's own: the grants a
    # killed load leaves are made on them, never on a kernel schema.
    d = with_db(server, db)
    sql(d, "CREATE SCHEMA IF NOT EXISTS rig_load")
    sql(d, "CREATE TABLE IF NOT EXISTS rig_load.rig_rows (x int)")
    sql(d, f"GRANT USAGE ON SCHEMA rig_load TO {ROLE}")
    sql(d, f"GRANT INSERT ON rig_load.rig_rows TO {ROLE}")


def state(server, db):
    """What the role can still do: can log in, member of, members, settings,
    INSERT on the rig table."""
    return sql(with_db(server, db), f"""SELECT concat_ws(' ', r.rolcanlogin,
            (SELECT count(*) FROM pg_auth_members WHERE member = r.oid),
            (SELECT count(*) FROM pg_auth_members WHERE roleid = r.oid),
            (SELECT count(*) FROM pg_db_role_setting WHERE setrole = r.oid),
            has_table_privilege(r.oid, 'rig_load.rig_rows', 'INSERT'))
          FROM pg_roles r WHERE r.rolname = '{ROLE}'""")


def logs_in(server, db):
    return _psql(with_db(server, db, role=ROLE), "-Atc", "SELECT 1").stdout.strip() == "1"


def part_a(server):
    print("== A. every restore starts by locking the role")
    sock = dict(parse_qsl(urlsplit(server).query)).get("host") or urlsplit(server).hostname
    port = dict(parse_qsl(urlsplit(server).query)).get("port") or (str(urlsplit(server).port or "") or None)
    hb.PG_SOCKET, hb.PG_USER = sock, "entity"
    if port:
        os.environ["PGPORT"] = port
    scratch_db = f"restore_{uuid.uuid4().hex[:8]}"
    sql(server, f'CREATE DATABASE "{scratch_db}"')
    hr.RESTORE_DB = scratch_db
    real_read_plan = hr.read_plan
    try:
        killed_load(server, scratch_db)
        check("the role a killed load leaves can log in and write", logs_in(server, scratch_db)
              and state(server, scratch_db) == "t 1 1 1 t", state(server, scratch_db))
        # A restore already waiting: stage answers that, after the lock, and
        # touches nothing else (no pool is needed on that path).
        hr.read_plan = lambda: {"state": hr.STAGED_STATE}
        out = asyncio.run(hr.stage(None, 1, "20261020T000000Z"))
        check("the restore answers as it did (one is already waiting)",
              "already waiting" in (out.get("error") or ""), out)
        check("A it cannot log in, is a member of nothing, has no member, no setting, no grant",
              state(server, scratch_db) == "f 0 0 0 f" and not logs_in(server, scratch_db), state(server, scratch_db))
    finally:
        hr.read_plan = real_read_plan
        sql(server, f'DROP DATABASE IF EXISTS "{scratch_db}" WITH (FORCE)')
        sql(server, "DROP ROLE IF EXISTS rig_member")


def part_b():
    print("== B. the database container's boot locks the role before it serves")
    if not HAVE_DOCKER or subprocess.run(["docker", "image", "inspect", IMAGE],
                                         capture_output=True).returncode != 0:
        print(f"SKIP-LOUD: B needs docker and the image {IMAGE}")
        return
    base = next(p for p in shared_dir_candidates() if p.is_dir())
    work = pathlib.Path(tempfile.mkdtemp(prefix=".lockrole-", dir=base))
    sock = work / "s"
    sock.mkdir()
    sock.chmod(0o777)
    name = f"lockrole-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    dsn = f"postgresql://entity@/postgres?host={sock}&port=5432"
    # The tree's file, executable as the image's own is (git keeps it 0644).
    entry = work / "entrypoint-postgres.sh"
    shutil.copyfile(ROOT / "entrypoint-postgres.sh", entry)
    entry.chmod(0o755)
    try:
        r = subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "--network", "none",
                            "-v", f"{sock}:/var/run/postgresql",
                            "-v", f"{entry}:/app/entrypoint-postgres.sh:ro",
                            IMAGE], capture_output=True, text=True, timeout=120)
        check("the image starts with this tree's entrypoint", r.returncode == 0, r.stderr[-300:])

        def ready():
            for _ in range(180):
                if _psql(dsn, "-Atc", "SELECT 1").stdout.strip() == "1":
                    return True
                time.sleep(0.5)
            return False
        check("  and serves", ready())
        live = ", ".join(f"'{d}'" for d in hr.LIVE_DBS)
        db = sql(dsn, f"SELECT datname FROM pg_database WHERE datname IN ({live}) LIMIT 1") or "postgres"
        killed_load(dsn, db)
        check("the role a killed load leaves can log in and write", logs_in(dsn, db)
              and state(dsn, db) == "t 1 1 1 t", state(dsn, db))
        r = subprocess.run(["docker", "restart", "-t", "20", name], capture_output=True, text=True, timeout=180)
        check("the container boots again on the same cluster", r.returncode == 0 and ready(), r.stderr[-300:])
        st = state(dsn, db)
        check("B after the boot it cannot log in, is a member of nothing, has no member, no setting",
              st.split()[:4] == ["f", "0", "0", "0"] and not logs_in(dsn, db), st)
        logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True, timeout=60)
        check("  and the boot said nothing of failing to", "could not lock the restore role"
              not in logs.stdout + logs.stderr, (logs.stdout + logs.stderr)[-400:])
    finally:
        subprocess.run(["docker", "stop", "-t", "5", name], capture_output=True, timeout=120)
        shutil.rmtree(work, ignore_errors=True)


with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv:
    part_a(_srv)
part_b()
print("RESULT: ALL PASS" if FAILED == 0 else f"RESULT: {FAILED} FAILED")
sys.exit(1 if FAILED else 0)
