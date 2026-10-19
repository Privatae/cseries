#!/usr/bin/env python3
"""Gate: the Host's own schema (`world`) and its role (`host_world`) against a
real kernel schema.

The schema is built by the boot's own section (scripts/kernel_schema.py), so
v233 runs exactly where entrypoint.sh runs it: after the migration loop.

  R. the role: LOGIN, no superuser, no RLS bypass, no CREATEDB/CREATEROLE/
     REPLICATION, CONNECTION LIMIT 3, member of no role; `world` is its own.
  W. it builds in `world`: create, comment, alter, insert, drop.
  X. the walls: no write to a kernel table, no create in `public`, no schema,
     no extension, no `SET ROLE entity`, no raising temp_file_limit, no fourth
     connection, no write to the AGE graph.
  L. the three lists in v233 (READABLE / SECRET / CLOSED): every table the
     kernel declares is on exactly one; host_world reads every READABLE table
     and no other; host_recall reads no SECRET table; a READABLE table with a
     secret-shaped column is named below with the reason it is safe.
  D. drift is pulled back by the next boot: a hand-granted membership, a
     superuser flag, an INSERT grant and an object owned by `entity` in `world`.
  S. no SECURITY DEFINER function anywhere host_world can call one.
  E. world_admin.empty_world() empties `world` as host_world and refuses others.
  B. the executor (brain/mastercode/world_db.py) on a host_world pool: SQL with
     a purpose lands; without one it rolls back naming the fix; params arrive
     as strings and are coerced to the statement's types; a script runs; a
     timeout cancels even after the SQL lifted its own; an error rolls back; a
     big result is capped and says so; describe shows purposes, columns and
     the call for a function; a write to a kernel table is refused.
  C. snapshots: one is taken before a change that can remove data, none when
     world is empty; restore brings a dropped table back with its rows and
     removes a table made after the snapshot; the undo snapshot undoes it; a
     bad id is refused; prune keeps the newest 20 plus one a day for 30 days.
  F. the database volume nearly full (pgdata_free in the DB's stats file): a
     write is refused as disk_full, a read works; a stale file is no guard.
  H. a whole-Host restore (server/host_restore._load_database) loads `world`
     in its own pass, connected as host_world: the table, its indexes, a view
     and a function come back owned by host_world, with rows and purposes; the
     kernel's tables come back as before; the Host can change what came back.
  Q. recall over what the Host built (mastercode/world_recall.py): by words a
     question about cooking tools finds `kitchen`; by meaning (a stand-in
     embedder) 'what should I make tonight' finds `meals`; purpose vectors are
     cached, re-embedded when a purpose changes and dropped with their table;
     recall's `world` store answers and its text has a section; the frame
     lists a small database whole and the best matches of a big one; aliases
     (memory_curate op='alias') make recall find a memory by new words, once,
     never for a memory the Host does not hold, and go with their memory.
  T. a function the Host writes is a tool: find_tools' catalog gains
     world:<name> on the operate door with the function's arguments as its
     schema, found by its purpose, not by a typed turn without the key
     `world:`; one capability row, no external_api; operate runs it through
     the gate, refuses a typed turn without the key, snapshots before a
     VOLATILE function and not a STABLE one, takes table-returning functions,
     refuses unknown and missing arguments; an unknown tool's refusal says how
     to build one.
  A. apps on the Host's own database (routes/world.py): use:world is
     declarable and not by community apps; an app's SQL passes the purpose
     check; describe, rows (paged; a view cannot change data by being read),
     history and restore; the memory routes' audience: an app reads only the
     memories tagged for it by id, the Memory app reads all, and the journal
     is for full-corpus callers only.

Fresh clone of the KERNEL's schema on TEST_PG_DSN / PEER_TEST_DSN / TEST_DSN,
else a throwaway cseries-postgres through docker.
  python3 scripts/test_host_world_pg.py
"""
import asyncio
import contextlib
import os
import pathlib
import re
import subprocess
import sys
from urllib.parse import urlsplit, urlunsplit

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "brain"))
sys.path.insert(0, str(ROOT))

import datetime as dt  # noqa: E402
import json  # noqa: E402
import tempfile  # noqa: E402

import asyncpg  # noqa: E402

from kernel_schema import _psql, kernel_schema_db, throwaway_server  # noqa: E402
from mastercode import world_db  # noqa: E402

SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    print("SKIP-LOUD: no test server (TEST_PG_DSN/PEER_TEST_DSN/TEST_DSN) and docker unavailable — "
          "this gate needs a real database to prove the host_world walls.")
    sys.exit(2)

V233 = ROOT / "migrations" / "v233_host_world.sql"
FAILED = 0

# A column on a READABLE table whose name looks like a secret, and why it is not
# one. The L section fails on any secret-shaped column of a READABLE table that
# is not here, so a new one is a decision someone writes down.
SECRET_SHAPED = re.compile(
    r"(^|_)(secret|password|passwd|private|privkey|seed|cookie|credentials?|bearer|hmac|"
    r"otp|totp|cipher|ciphertext|sealed|wrapped|dek|salt|key)(_|$)|(^|_)token$")
SAFE_COLUMNS = {
    ("application_signing_keys", "public_key"): "a public key",
    ("bastion_pairing_tokens", "secret_hash"): "SHA-256 of a 192-bit secret",
    ("integration_connections", "credentials_ref"):
        "an opaque cred_<hex> name the vault entry is filed under (integrations/core/credentials.py:177-184)",
    ("integration_oauth_clients", "credentials_ref"): "the same opaque vault name",
    ("entity_appearance", "image_bytes"): "an image",
    ("entity_appearance", "image_bytes_mobile"): "an image",
    ("generated_images", "bytes"): "an image",
    ("group_members", "verify_pubkey"): "a public key",
    ("group_votes", "signature"): "a signature, verifiable by anyone",
    ("mcp_client_tokens", "token_hash"): "SHA-256 of a 256-bit token",
    ("oversight_store_cursor", "sealed"): "a flag",
    ("turn_tool_results", "turn_token"): "a turn's id, not a credential",
    ("vault_identity_pubkey", "pubkey"): "a public key",
    # `key` joined the pattern later: each of these was read.
    ("context_backend_locations", "object_key"): "a storage path (v077:68)",
    ("context_backend_meta", "object_key"): "the S3 key or bastion path of a file (v076:19)",
    ("dcn_move_plans", "signing_key_id"): "which key signed, not the key",
    ("dcn_stores", "signing_key_id"): "which key signed, not the key",
    ("decisions", "chosen_key"): "the option the decision chose (v026:40)",
    ("entity_permissions", "permission_key"): "a permission's name",
    ("group_members", "key_pinned_at"): "a timestamp",
    ("group_votes", "key_id"): "which key signed the vote, not the key",
    ("integration_connections", "provider_key"): "the provider's id (integrations/core/connection.py:56)",
    ("mcp_connections", "key_name"): "the name of a Keys entry, not its value",
    ("mcp_connections", "signing_key_id"): "which key signed, not the key",
    ("nac_egress", "signing_key_id"): "which key signed, not the key",
    ("nac_mgmt_grants", "signing_key_id"): "which key signed, not the key",
    ("nac_policy", "signing_key_id"): "which key signed, not the key",
    ("oversight_findings", "key"): "a finding's (machine, kind, fingerprint) id (v156:69)",
    ("pending_words", "thread_key"): "a thread's id",
    ("research_pages", "thread_key"): "a thread's id",
    ("service_reach_grants", "signing_key_id"): "which key signed, not the key",
    ("ship_machine_keys", "encryption_key"): "a machine's public key, pinned (server/ships/seal.py:63-81)",
    ("ship_machine_keys", "machine_key"): "a machine's public key, pinned (server/ships/seal.py:63-81)",
}


def check(label, cond, detail=""):
    global FAILED
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  <- {detail}"))
    if not cond:
        FAILED += 1


def as_role(dsn: str, role: str) -> str:
    """The same database over the same socket, as `role` (pg_hba is trust)."""
    p = urlsplit(dsn)
    host = p.hostname or ""
    if p.port:
        host = f"{host}:{p.port}"
    return urlunsplit((p.scheme, f"{role}@{host}", p.path, p.query, p.fragment))


def v233_lists() -> dict[str, list[str]]:
    """READABLE / SECRET / CLOSED as v233 declares them (its array literals)."""
    text = V233.read_text()
    out = {}
    for name in ("readable", "secret", "closed"):
        m = re.search(rf"\b{name} text\[\] := ARRAY\[(.*?)\];", text, re.S)
        out[name] = re.findall(r"'([a-z0-9_]+)'", m.group(1)) if m else []
    return out


async def refused(conn, sql) -> str:
    """'' when `sql` ran, else the error's class name."""
    try:
        async with conn.transaction():
            await conn.execute(sql)
        return ""
    except asyncpg.PostgresError as e:
        return type(e).__name__


def run_v233(dsn: str) -> subprocess.CompletedProcess:
    return _psql(dsn, "-v", "ON_ERROR_STOP=1", "-f", str(V233))


async def role_checks(dsn):
    su = await asyncpg.connect(dsn)
    hw = await asyncpg.connect(as_role(dsn, "host_world"))
    try:
        # ── R ──
        r = await su.fetchrow(
            "SELECT rolcanlogin, rolsuper, rolbypassrls, rolcreatedb, rolcreaterole, "
            "rolreplication, rolconnlimit FROM pg_roles WHERE rolname = 'host_world'")
        check("R host_world exists and logs in", r is not None and r["rolcanlogin"], str(r))
        check("R no superuser, no RLS bypass, no CREATEDB/CREATEROLE/REPLICATION",
              r is not None and not any((r["rolsuper"], r["rolbypassrls"], r["rolcreatedb"],
                                         r["rolcreaterole"], r["rolreplication"])), str(r))
        check("R CONNECTION LIMIT 3", r is not None and r["rolconnlimit"] == 3, str(r))
        n = await su.fetchval(
            "SELECT count(*) FROM pg_auth_members WHERE member = 'host_world'::regrole")
        check("R member of no role", n == 0, str(n))
        owner = await su.fetchval(
            "SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'world'")
        check("R `world` exists and host_world owns it", owner == "host_world", str(owner))
        sp = await hw.fetchval("SHOW search_path")
        check("R its search_path is world, public, pg_temp (temporary tables searched last)",
              sp.replace(" ", "") == "world,public,pg_temp", sp)

        # ── W ──
        built = await refused(hw, """
            CREATE TABLE world.kitchen (tool text PRIMARY KEY, uses int NOT NULL DEFAULT 0);
            COMMENT ON TABLE world.kitchen IS 'The cooking tools the person has.';
            ALTER TABLE world.kitchen ADD COLUMN note text;
            INSERT INTO world.kitchen (tool) VALUES ('air fryer'), ('rice cooker');
            CREATE VIEW world.kitchen_used AS SELECT tool FROM world.kitchen WHERE uses > 0;
            CREATE FUNCTION world.tool_count() RETURNS bigint LANGUAGE sql
                AS 'SELECT count(*) FROM world.kitchen';
        """)
        check("W it creates, comments, alters, fills and adds a view and a function in world",
              built == "", built)
        check("W and reads them back", await hw.fetchval("SELECT world.tool_count()") == 2)
        check("W it drops what it made",
              await refused(hw, "DROP VIEW world.kitchen_used") == "")
        check("W an unqualified CREATE lands in world (search_path)",
              await refused(hw, "CREATE TABLE meals (d date)") == ""
              and await su.fetchval("SELECT to_regclass('world.meals') IS NOT NULL"))

        # ── X ──
        check("X no write to a kernel table (INSERT entity_memory)",
              await refused(hw, "INSERT INTO public.entity_memory (entity_id, content, memory_type) "
                                "VALUES (1, 'x', 'preference')") == "InsufficientPrivilegeError")
        check("X no UPDATE of the person's network rules (nac_policy)",
              await refused(hw, "UPDATE public.nac_policy SET priority = priority")
              == "InsufficientPrivilegeError")
        check("X no DELETE from the tool log (tool_call_log)",
              await refused(hw, "DELETE FROM public.tool_call_log WHERE id > 0") == "InsufficientPrivilegeError")
        check("X no create in public",
              await refused(hw, "CREATE TABLE public.shadow (x int)") == "InsufficientPrivilegeError")
        check("X no function in public",
              await refused(hw, "CREATE FUNCTION public.f() RETURNS int LANGUAGE sql AS 'SELECT 1'")
              == "InsufficientPrivilegeError")
        check("X no new schema",
              await refused(hw, "CREATE SCHEMA elsewhere") == "InsufficientPrivilegeError")
        check("X no extension",
              await refused(hw, "CREATE EXTENSION IF NOT EXISTS hstore") != "")
        check("X no SET ROLE entity",
              await refused(hw, "SET ROLE entity") == "InsufficientPrivilegeError")
        check("X temp_file_limit cannot be raised",
              await refused(hw, "SET temp_file_limit = '100GB'") == "InsufficientPrivilegeError")
        graph = await su.fetch(
            "SELECT format('%I.%I', schemaname, tablename) AS t FROM pg_tables "
            "WHERE schemaname NOT IN ('pg_catalog', 'information_schema', 'public', 'world')")
        writable = [g["t"] for g in graph if await su.fetchval(
            "SELECT has_table_privilege('host_world', $1, 'INSERT') "
            "OR has_table_privilege('host_world', $1, 'UPDATE') "
            "OR has_table_privilege('host_world', $1, 'DELETE')", g["t"])]
        check("X no write to any table outside public and world (AGE graph included)",
              not writable, str(writable))
        extra = []
        try:
            extra = [await asyncpg.connect(as_role(dsn, "host_world")) for _ in range(2)]
            fourth = ""
            try:
                c4 = await asyncpg.connect(as_role(dsn, "host_world"))
                await c4.close()
            except asyncpg.PostgresError as e:
                fourth = type(e).__name__
            check("X a fourth host_world connection is refused",
                  fourth == "TooManyConnectionsError", fourth or "connected")
        finally:
            for c in extra:
                await c.close()

        # ── S ──
        definers = await su.fetch("""
            SELECT p.oid::regprocedure::text AS f FROM pg_proc p
              JOIN pg_namespace n ON n.oid = p.pronamespace
             WHERE p.prosecdef
               AND has_schema_privilege('host_world', n.oid, 'USAGE')
               AND has_function_privilege('host_world', p.oid, 'EXECUTE')""")
        check("S no SECURITY DEFINER function host_world can call, in any schema",
              not definers, str([d["f"] for d in definers]))

        # ── E ──
        await hw.execute("CREATE DOMAIN world.qty AS int CHECK (VALUE >= 0)")
        await hw.execute("CREATE TYPE world.mood AS ENUM ('ok', 'tired')")
        await hw.execute("CALL world_admin.empty_world()")
        left = await su.fetchval("""
            SELECT (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                     WHERE n.nspname = 'world')
                 + (SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                     WHERE n.nspname = 'world')
                 + (SELECT count(*) FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
                     WHERE n.nspname = 'world')""")
        check("E empty_world leaves nothing in world (tables, views, functions, types)", left == 0, str(left))
        check("E and world itself stays, still host_world's",
              await su.fetchval("SELECT nspowner::regrole::text FROM pg_namespace "
                                "WHERE nspname = 'world'") == "host_world")
        check("E empty_world refuses any role but host_world",
              await refused(su, "CALL world_admin.empty_world()") == "RaiseError")
        check("E host_world cannot change what a restore runs",
              await refused(hw, "CREATE OR REPLACE PROCEDURE world_admin.empty_world() "
                                "LANGUAGE sql AS 'SELECT 1'") == "InsufficientPrivilegeError")
    finally:
        await hw.close()
        await su.close()


async def list_checks(dsn):
    su = await asyncpg.connect(dsn)
    try:
        lists = v233_lists()
        declared = {r["t"] for r in await su.fetch(
            "SELECT c.relname AS t FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm')")}
        listed = lists["readable"] + lists["secret"] + lists["closed"]
        twice = sorted({t for t in listed if listed.count(t) > 1})
        check("L no table is on two lists", not twice, str(twice))
        unlisted = sorted(declared - set(listed))
        check("L every table the kernel declares is on a list (a new table is a decision)",
              not unlisted, f"add to READABLE, SECRET or CLOSED in v233: {unlisted}")
        stale = sorted(set(listed) - declared)
        check("L every listed table exists", not stale, str(stale))
        cannot = [t for t in lists["readable"] if t in declared and not await su.fetchval(
            "SELECT has_table_privilege('host_world', $1, 'SELECT')", f"public.{t}")]
        check("L host_world reads every READABLE table", not cannot, str(cannot))
        leaks = [t for t in lists["secret"] + lists["closed"] if t in declared and await su.fetchval(
            "SELECT has_table_privilege('host_world', $1, 'SELECT')", f"public.{t}")]
        check("L host_world reads no SECRET or CLOSED table", not leaks, str(leaks))
        recall = [t for t in lists["secret"] if t in declared and await su.fetchval(
            "SELECT has_table_privilege('host_recall', $1, 'SELECT')", f"public.{t}")]
        check("L host_recall reads no SECRET table", not recall, str(recall))
        cols = await su.fetch(
            "SELECT table_name AS t, column_name AS c, data_type AS ty FROM information_schema.columns "
            "WHERE table_schema = 'public'")
        shaped = sorted({(r["t"], r["c"]) for r in cols
                         if r["t"] in lists["readable"]
                         and (SECRET_SHAPED.search(r["c"]) or r["ty"] == "bytea")})
        unexplained = [tc for tc in shaped if tc not in SAFE_COLUMNS]
        check("L every secret-shaped column of a READABLE table is explained (SAFE_COLUMNS)",
              not unexplained, str(unexplained))
        gone = sorted(set(SAFE_COLUMNS) - set(shaped))
        check("L SAFE_COLUMNS names no column that is gone or no longer readable", not gone, str(gone))
        defaults = await su.fetchval(
            "SELECT count(*) FROM pg_default_acl d, aclexplode(d.defaclacl) a "
            "WHERE a.grantee = 'host_world'::regrole")
        check("L host_world holds no default privileges (closed by default)", defaults == 0, str(defaults))
    finally:
        await su.close()


async def drift_checks(dsn):
    su = await asyncpg.connect(dsn)
    try:
        await su.execute("GRANT entity TO host_world")
        await su.execute("ALTER ROLE host_world SUPERUSER CONNECTION LIMIT 50")
        await su.execute("GRANT INSERT ON public.entity_memory TO host_world")
        await su.execute("GRANT SELECT ON public.kernel_secrets TO host_world")
        await su.execute("CREATE TABLE world.loaded_by_entity (x int)")
        await su.execute("CREATE FUNCTION world.f_by_entity() RETURNS int LANGUAGE sql AS 'SELECT 1'")
        r = run_v233(dsn)
        check("D the next boot's v233 runs clean", r.returncode == 0, r.stderr[-400:])
        row = await su.fetchrow(
            "SELECT rolsuper, rolconnlimit, (SELECT count(*) FROM pg_auth_members "
            "WHERE member = 'host_world'::regrole) AS m FROM pg_roles WHERE rolname = 'host_world'")
        check("D superuser, a raised limit and a membership are pulled back",
              not row["rolsuper"] and row["rolconnlimit"] == 3 and row["m"] == 0, str(dict(row)))
        check("D a hand-granted INSERT and a secret SELECT are gone",
              not await su.fetchval("SELECT has_table_privilege('host_world', 'public.entity_memory', 'INSERT')")
              and not await su.fetchval("SELECT has_table_privilege('host_world', 'public.kernel_secrets', 'SELECT')"))
        owners = await su.fetch(
            "SELECT relowner::regrole::text AS o FROM pg_class WHERE oid = 'world.loaded_by_entity'::regclass "
            "UNION ALL SELECT proowner::regrole::text FROM pg_proc WHERE oid = 'world.f_by_entity()'::regprocedure")
        check("D objects left in world by entity come back to host_world",
              [o["o"] for o in owners] == ["host_world", "host_world"], str([o["o"] for o in owners]))
        r2 = run_v233(dsn)
        check("D and a second run changes nothing and fails nothing", r2.returncode == 0, r2.stderr[-400:])
    finally:
        await su.close()


async def executor_checks(dsn, snapdir: pathlib.Path):
    hw_dsn = as_role(dsn, "host_world")
    pool = await asyncpg.create_pool(hw_dsn, min_size=0, max_size=2)
    su = await asyncpg.connect(dsn)
    os.environ["WORLD_SNAPSHOT_DIR"] = str(snapdir)
    run = lambda sql, params=None, t=None: world_db.run_sql(sql, params, t, pool=pool, dsn=hw_dsn)  # noqa: E731
    try:
        # ── B ──
        # The drift section left world.loaded_by_entity and world.f_by_entity()
        # without a purpose: older objects, which must not fail a read.
        r = await run("SELECT 1 AS one")
        check("B a read does not fail on an older object without a purpose, and names it",
              r["status"] == "ok" and "world.loaded_by_entity" in r.get("without_purpose", [])
              and "world.f_by_entity()" in r.get("without_purpose", []), str(r))
        r = await run("CREATE TABLE pantry (item text PRIMARY KEY, qty int NOT NULL DEFAULT 0, "
                      "bought date); COMMENT ON TABLE pantry IS 'What is in the person''s cupboard.'")
        check("B a table with its purpose lands (a two-statement script)", r["status"] == "ok", str(r))
        r = await run("CREATE TABLE nopurpose (x int)")
        check("B a table without a purpose rolls back and names the fix",
              r.get("reason") == "no_purpose" and "COMMENT ON TABLE world.nopurpose" in r["result"]
              and not await su.fetchval("SELECT to_regclass('world.nopurpose') IS NOT NULL"), str(r))
        r = await run("CREATE FUNCTION f_nopurpose() RETURNS int LANGUAGE sql AS 'SELECT 1'")
        check("B a function without a purpose rolls back too",
              r.get("reason") == "no_purpose" and "COMMENT ON FUNCTION world.f_nopurpose()" in r["result"], str(r))
        # A script that ends the call's transaction itself would commit what
        # comes before the purpose check, and run what follows outside the call.
        r = await run("BEGIN; CREATE TABLE wrapped (x int); COMMIT;")
        check("B a script that commits on its own is refused and leaves nothing behind",
              r.get("reason") == "transaction_control"
              and not await su.fetchval("SELECT to_regclass('world.wrapped') IS NOT NULL"), str(r))
        r = await run("CREATE FUNCTION f_atomic() RETURNS int LANGUAGE sql BEGIN ATOMIC SELECT 1; END; "
                      "COMMENT ON FUNCTION f_atomic() IS 'A standard-SQL body.'")
        check("B a standard-SQL function body (BEGIN ATOMIC ... END) is not taken for a COMMIT",
              r["status"] == "ok", str(r))
        r = await run("INSERT INTO pantry (item, qty, bought) VALUES ($1, $2, $3)",
                      ["rice", "3", "2026-10-16"])
        check("B params arrive as strings and are coerced to text/int/date",
              r["status"] == "ok" and r["command"] == "INSERT 0 1", str(r))
        r = await run("SELECT item, qty, bought FROM pantry WHERE qty > $1", ["1"])
        check("B a select returns its rows, JSON-ready",
              r["status"] == "ok" and r["rows"] == [{"item": "rice", "qty": 3, "bought": "2026-10-16"}], str(r))
        r = await run("SELECT 1 WHERE $1", ["maybe"])
        check("B a param that is not its type is refused by name",
              r.get("reason") == "bad_param" and "$1 is bool" in r["result"], str(r))
        r = await run("SELECT 1; SELECT 2", ["x"])
        check("B params with a multi-statement script are refused", r["status"] == "error", str(r))
        r = await run("SELECT pg_sleep(5)", None, 1)
        check("B a statement past its timeout is cancelled", r.get("reason") == "timeout", str(r))
        r = await run("SET statement_timeout = 0; SELECT pg_sleep(4)", None, 1)
        check("B and lifting the session's own timeout in the SQL does not lift the tool's",
              r.get("reason") == "timeout", str(r))
        r = await run("INSERT INTO pantry (item) VALUES ('x'); SELECT 1/0")
        check("B an error rolls the whole call back",
              r.get("reason") == "sql_error" and r.get("sqlstate") == "22012"
              and await su.fetchval("SELECT count(*) FROM world.pantry WHERE item = 'x'") == 0, str(r))
        r = await run(f"SELECT g FROM generate_series(1, {world_db.ROW_CAP + 10}) g")
        check("B a big result is capped and says so",
              r.get("truncated") and r["row_count"] == world_db.ROW_CAP, str({k: r.get(k) for k in ("truncated", "row_count")}))
        r = await run("INSERT INTO public.entity_memory (entity_id, content, memory_type) VALUES (1, 'x', 'preference')")
        check("B a write to a kernel table is refused through the tool",
              r.get("sqlstate") == "42501", str(r))
        await run("CREATE FUNCTION low_stock(n int) RETURNS SETOF text LANGUAGE sql "
                  "AS 'SELECT item FROM world.pantry WHERE qty < n'; "
                  "COMMENT ON FUNCTION low_stock(int) IS 'Items running low.'; "
                  "COMMENT ON COLUMN pantry.qty IS 'How many are left.'")
        d = await world_db.describe(pool=pool, dsn=hw_dsn)
        names = {o["name"]: o for o in d.get("objects", [])}
        check("B describe shows the table with its purpose and column purposes",
              names.get("world.pantry", {}).get("purpose") == "What is in the person's cupboard."
              and {"name": "qty", "type": "integer", "not_null": True, "purpose": "How many are left."}
              in names.get("world.pantry", {}).get("columns", []), str(names.get("world.pantry")))
        fn = names.get("world.low_stock(n integer)", {})
        check("B describe shows a function with its purpose and how to call it",
              fn.get("purpose") == "Items running low." and fn.get("call", "").startswith("operate(tool='world:low_stock'"),
              str(fn))
        check("B a function's call carries the intent operate refuses a call without",
              "intent='" in fn.get("call", ""), str(fn))
        one = await world_db.describe("pantry", pool=pool, dsn=hw_dsn)
        check("B describe of one name returns that object", one.get("count") == 1, str(one))
        check("B describe of an unknown name says so",
              (await world_db.describe("nothing_here", pool=pool, dsn=hw_dsn))["status"] == "error")

        # ── C ──
        before = len(list(snapdir.glob("*.sql")))
        r = await run("CREATE TABLE later (x int); COMMENT ON TABLE later IS 'made after';"
                      "UPDATE pantry SET qty = qty")
        check("C a change that can reshape data takes a snapshot first",
              r["status"] == "ok" and r.get("snapshot") and len(list(snapdir.glob("*.sql"))) == before + 1, str(r))
        snap = r["snapshot"]
        # `later` was made in the same call, so the snapshot predates it.
        r = await run("DROP TABLE pantry CASCADE")
        check("C a drop is preceded by its own snapshot", r["status"] == "ok" and r.get("snapshot"), str(r))
        h = world_db.history(snapdir)
        check("C history lists them, newest first",
              h["count"] >= 2 and h["snapshots"][0]["snapshot"] == r["snapshot"], str(h)[:300])
        res = world_db.restore(snap, hw_dsn, snapdir)
        check("C restore succeeds", res["status"] == "ok", str(res))
        check("C the dropped table is back with its rows",
              await su.fetchval("SELECT qty FROM world.pantry WHERE item = 'rice'") == 3)
        check("C a table made after the snapshot is gone",
              not await su.fetchval("SELECT to_regclass('world.later') IS NOT NULL"))
        check("C what comes back is owned by host_world and keeps its purpose",
              await su.fetchval("SELECT relowner::regrole::text FROM pg_class WHERE oid = 'world.pantry'::regclass")
              == "host_world"
              and await su.fetchval("SELECT obj_description('world.pantry'::regclass, 'pg_class')")
              == "What is in the person's cupboard.")
        undo = world_db.restore(res["undo_snapshot"], hw_dsn, snapdir)
        check("C the undo snapshot puts back the state before the restore",
              undo["status"] == "ok"
              and not await su.fetchval("SELECT to_regclass('world.pantry') IS NOT NULL")
              and await su.fetchval("SELECT to_regclass('world.later') IS NOT NULL"), str(undo))
        check("C a bad snapshot id is refused",
              world_db.restore("../../etc/passwd", hw_dsn, snapdir)["status"] == "error")
        # The trigger reads statement words, not text, and an unchanged world
        # is not snapshotted twice.
        await run("CREATE TABLE IF NOT EXISTS later (x int); COMMENT ON TABLE later IS 'made after'")
        n = len(list(snapdir.glob("*.sql")))
        r = await run("INSERT INTO later VALUES (1); SELECT 'call mom, then drop by the shop'")
        check("C words inside a string are not a reshaping statement",
              r["status"] == "ok" and not r.get("snapshot") and len(list(snapdir.glob("*.sql"))) == n, str(r))
        r = await run("CREATE OR REPLACE VIEW later_view AS SELECT x FROM later; "
                      "COMMENT ON VIEW later_view IS 'later, as a view'")
        check("C CREATE OR REPLACE takes a snapshot first", r["status"] == "ok" and r.get("snapshot"), str(r))
        a = world_db.take_snapshot("manual", hw_dsn, snapdir)
        b = world_db.take_snapshot("manual", hw_dsn, snapdir)
        check("C an unchanged world is not snapshotted twice",
              a["status"] == "ok" and b.get("unchanged") and b["snapshot"] == a["snapshot"], f"{a} {b}")
        check("C every snapshot carries psql's \\restrict guard",
              all(world_db._has_restrict(p) for p in snapdir.glob("*.sql")))
        bare = snapdir / "20261016T000000000000Z-manual.sql"
        bare.write_text("--\n-- a dump with no guard\n--\nSELECT 1;\n")
        r = world_db.restore(bare.stem, hw_dsn, snapdir)
        check("C a snapshot without the guard is never replayed",
              r["status"] == "error" and "17.6" in r["result"], str(r))
        bare.unlink()
        async with pool.acquire() as c:
            await c.execute("CALL world_admin.empty_world()")
        n = len(list(snapdir.glob("*.sql")))
        r = await run("UPDATE nothing SET x = 1")
        check("C no snapshot when world is empty",
              len(list(snapdir.glob("*.sql"))) == n and r["status"] == "error", str(r))
        # ── F: the database volume nearly full ──
        await run("CREATE TABLE notes (t text); COMMENT ON TABLE notes IS 'Scratch notes.'")
        with tempfile.NamedTemporaryFile("w", suffix=".stats", delete=False) as sf:
            stats = pathlib.Path(sf.name)
        import time as _time
        try:
            os.environ["WORLD_DB_STATS"] = str(stats)
            stats.write_text(f"ts {int(_time.time())}\npgdata_size {10 * 2**30}\npgdata_free {100 * 2**20}\n")
            r = await run("INSERT INTO notes VALUES ('x')")
            check("F with the volume nearly full, a write is refused as disk_full",
                  r.get("reason") == "disk_full" and "100 MiB free" in r["result"], str(r))
            r = await run("SELECT count(*) AS n FROM notes")
            check("F and a read still works", r["status"] == "ok" and r["rows"] == [{"n": 0}], str(r))
            r = await run("COMMIT; INSERT INTO notes VALUES ('past the guard')")
            check("F a script that ends the read-only transaction itself writes nothing",
                  await su.fetchval("SELECT count(*) FROM world.notes") == 0, str(r))
            stats.write_text(f"ts {int(_time.time()) - 600}\npgdata_size {10 * 2**30}\npgdata_free 1\n")
            r = await run("INSERT INTO notes VALUES ('y')")
            check("F a stale stats file is no guard (an older DB image, a dead writer)",
                  r["status"] == "ok", str(r))
            stats.write_text(f"ts {int(_time.time())}\npgdata_size {10 * 2**30}\npgdata_free {5 * 2**30}\n")
            r = await run("INSERT INTO notes VALUES ('z')")
            check("F with room, writes go through", r["status"] == "ok", str(r))
        finally:
            os.environ.pop("WORLD_DB_STATS", None)
            stats.unlink(missing_ok=True)
        with tempfile.TemporaryDirectory() as pd:
            p = pathlib.Path(pd)
            now = dt.datetime(2026, 10, 16, 12, tzinfo=dt.timezone.utc)
            for day in range(40):
                for k in range(3):
                    t = now - dt.timedelta(days=day, hours=k)
                    (p / f"{t.strftime('%Y%m%dT%H%M%S%fZ')}-before-change.sql").write_text("x")
            world_db.prune(p, now)
            left = sorted(x.stem for x in p.glob("*.sql"))
            days = {s[:8] for s in left}
            check("C prune keeps the newest 20 plus one a day for 30 days",
                  len(left) == 20 + (30 - 7) and len(days) == 30, f"{len(left)} kept over {len(days)} days")
    finally:
        await su.close()
        await pool.close()


def _socket_and_port(dsn: str) -> tuple[str, str | None]:
    from urllib.parse import parse_qsl
    p = urlsplit(dsn)
    q = dict(parse_qsl(p.query))
    return q.get("host") or p.hostname or "", q.get("port") or (str(p.port) if p.port else None)


def _with_db_name(dsn: str, db: str) -> str:
    p = urlsplit(dsn)
    return urlunsplit((p.scheme, p.netloc, "/" + db, p.query, p.fragment))


async def restore_checks(dsn):
    """H. A whole-Host restore (server/host_restore._load_database) brings the
    Host's own schema back loaded as host_world, with its rows and purposes,
    and the kernel's tables as before."""
    from server import host_backup as hb
    from server import host_restore as hr
    hw = await asyncpg.connect(as_role(dsn, "host_world"))
    su = await asyncpg.connect(dsn)
    target = "restore_" + os.urandom(4).hex()
    socket, port = _socket_and_port(dsn)
    saved = (hr.RESTORE_DB, hb.PG_SOCKET, hb.PG_USER, os.environ.get("PGPORT"))
    spool = None
    try:
        await hw.execute("CALL world_admin.empty_world()")     # start H from an empty world
        await hw.execute("""
            CREATE TABLE world.recipes (name text PRIMARY KEY, minutes int NOT NULL);
            COMMENT ON TABLE world.recipes IS 'Recipes the person likes.';
            CREATE INDEX recipes_minutes ON world.recipes (minutes);
            INSERT INTO world.recipes VALUES ('lentil soup', 40), ('fried rice', 15);
            CREATE VIEW world.quick AS SELECT name FROM world.recipes WHERE minutes <= 20;
            COMMENT ON VIEW world.quick IS 'Recipes done in 20 minutes.';
            CREATE FUNCTION world.recipe_count() RETURNS bigint LANGUAGE sql
                AS 'SELECT count(*) FROM world.recipes';
            COMMENT ON FUNCTION world.recipe_count() IS 'How many recipes are kept.';
        """)
        with tempfile.NamedTemporaryFile(suffix=".dump", delete=False) as df:
            spool = pathlib.Path(df.name)
        # The platform's own backup flags (backup-entities.sh: pg_dump -Fc -Z0
        # --no-owner --no-privileges), as entity.
        r = subprocess.run(["pg_dump", "--dbname", dsn, "-Fc", "-Z0", "--no-owner",
                            "--no-privileges", "-f", str(spool)], capture_output=True, text=True)
        check("H the backup dumps", r.returncode == 0, r.stderr[-300:])
        await su.execute(f'CREATE DATABASE "{target}"')
        hr.RESTORE_DB, hb.PG_SOCKET, hb.PG_USER = target, socket, "entity"
        if port:
            os.environ["PGPORT"] = port
        try:
            note = hr._load_database(spool)
            loaded = True
        except Exception as e:      # noqa: BLE001 — reported as the check's detail
            loaded, note = False, repr(e)
        check("H the two-pass restore loads, nothing left out", loaded and note is None, str(note))
        rd = await asyncpg.connect(_with_db_name(dsn, target))
        try:
            owners = await rd.fetch("""
                SELECT c.relname AS n, c.relowner::regrole::text AS o FROM pg_class c
                  JOIN pg_namespace s ON s.oid = c.relnamespace
                 WHERE s.nspname = 'world' AND c.relkind IN ('r', 'v', 'i')
                UNION ALL
                SELECT p.proname, p.proowner::regrole::text FROM pg_proc p
                  JOIN pg_namespace s ON s.oid = p.pronamespace WHERE s.nspname = 'world'""")
            got = {o["n"]: o["o"] for o in owners}
            check("H the table, its indexes, the view and the function came back, each loaded as host_world",
                  got == {"recipes": "host_world", "recipes_pkey": "host_world",
                          "recipes_minutes": "host_world", "quick": "host_world",
                          "recipe_count": "host_world"}, str(got))
            check("H with their rows", await rd.fetchval("SELECT count(*) FROM world.recipes") == 2)
            check("H and their purposes",
                  await rd.fetchval("SELECT obj_description('world.recipes'::regclass, 'pg_class')")
                  == "Recipes the person likes.")
            check("H the kernel's own tables came back as before",
                  await rd.fetchval("SELECT to_regclass('public.entity_memory') IS NOT NULL"))
            check("H world is host_world's in the restored database",
                  await rd.fetchval("SELECT nspowner::regrole::text FROM pg_namespace "
                                    "WHERE nspname = 'world'") == "host_world")
        finally:
            await rd.close()
        rhw = await asyncpg.connect(as_role(_with_db_name(dsn, target), "host_world"))
        try:
            check("H and the Host can change what came back",
                  await refused(rhw, "INSERT INTO world.recipes VALUES ('omelette', 10)") == "")
        finally:
            await rhw.close()
    finally:
        hr.RESTORE_DB, hb.PG_SOCKET, hb.PG_USER = saved[0], saved[1], saved[2]
        if saved[3] is None:
            os.environ.pop("PGPORT", None)
        else:
            os.environ["PGPORT"] = saved[3]
        await su.execute(f'DROP DATABASE IF EXISTS "{target}" WITH (FORCE)')
        await su.close()
        await hw.close()
        if spool is not None:
            spool.unlink(missing_ok=True)


_FOOD = {"cook", "cooked", "make", "eat", "ate", "dinner", "tonight", "meal", "meals", "food", "recipe"}
_TOOLS = {"tool", "tools", "fryer", "cooker", "kitchen", "pan"}


async def _fake_embed(text: str):
    """A deterministic stand-in for the Host's embedder: food words, tool
    words, everything else. Tests the plumbing, not the quality."""
    words = re.findall(r"[a-z]+", (text or "").lower())
    v = [sum(w in _FOOD for w in words), sum(w in _TOOLS for w in words),
         sum(w not in _FOOD and w not in _TOOLS for w in words) * 0.2]
    return v if any(v) else [0.0, 0.0, 1.0]


async def recall_checks(dsn):
    """Q. Recall over the Host's own database, the frame line, and aliases."""
    from mastercode import memory_retrieval, world_recall
    from mastercode.runtime import tools as host_tools
    from server import brain_service
    hw_dsn = as_role(dsn, "host_world")
    wpool = await asyncpg.create_pool(hw_dsn, min_size=0, max_size=2)
    bpool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    saved = (brain_service._world_pool, brain_service._pool, brain_service._entity_id)
    brain_service._world_pool, brain_service._pool, brain_service._entity_id = wpool, bpool, 1
    os.environ["WORLD_DATABASE_URL"] = hw_dsn
    try:
        async with wpool.acquire() as c:
            await c.execute("CALL world_admin.empty_world()")
        r = await world_db.run_sql(
            "CREATE TABLE kitchen (tool text PRIMARY KEY); "
            "COMMENT ON TABLE kitchen IS 'The cooking tools the person has.'; "
            "CREATE TABLE meals (day date, dish text, note text); "
            "COMMENT ON TABLE meals IS 'What the person ate, and how it went.'; "
            "COMMENT ON COLUMN meals.dish IS 'The dish eaten'; "
            "INSERT INTO kitchen VALUES ('air fryer'), ('rice cooker')", pool=wpool, dsn=hw_dsn)
        check("Q the Host builds two tables to recall", r["status"] == "ok", str(r))
        cat = await world_db.catalog(pool=wpool, dsn=hw_dsn)
        check("Q the catalog lists both, with purposes and columns",
              {e["name"] for e in cat} == {"world.kitchen", "world.meals"}
              and next(e for e in cat if e["short"] == "meals")["columns"] == ["day", "dish", "note"], str(cat))
        scored, ran = await world_recall.rank(bpool, 1, cat, "which cooking tools do I have", semantic=False)
        check("Q by words: a question about cooking tools finds the kitchen first",
              scored and scored[0][1]["short"] == "kitchen" and not ran, str([(s, e["short"]) for s, e in scored]))
        scored, ran = await world_recall.rank(bpool, 1, cat, "what should I make tonight",
                                              embed_lane=(_fake_embed, "fake-1"),
                                              refresh_in_background=False)
        check("Q the first search by meaning embeds the purposes it lacks",
              await bpool.fetchval("SELECT count(*) FROM world_catalog_vectors WHERE model = 'fake-1'") == 2)
        scored, ran = await world_recall.rank(bpool, 1, cat, "what should I make tonight",
                                              embed_lane=(_fake_embed, "fake-1"),
                                              refresh_in_background=False)
        check("Q by meaning: 'what should I make tonight' finds the meals table, which shares no word with it",
              ran and scored and scored[0][1]["short"] == "meals", str([(round(s, 3), e["short"]) for s, e in scored]))
        await world_db.run_sql("COMMENT ON TABLE kitchen IS 'Pans and appliances the person cooks with.'",
                               pool=wpool, dsn=hw_dsn)
        await world_db.run_sql("DROP TABLE meals", pool=wpool, dsn=hw_dsn)
        cat2 = await world_db.catalog(pool=wpool, dsn=hw_dsn)
        await world_recall.rank(bpool, 1, cat2, "pans", embed_lane=(_fake_embed, "fake-1"),
                                refresh_in_background=False)
        rows = await bpool.fetch("SELECT object_name, text_hash FROM world_catalog_vectors")
        check("Q a changed purpose is re-embedded and a dropped table's vector goes",
              [r["object_name"] for r in rows] == ["world.kitchen"]
              and rows[0]["text_hash"] == world_recall._hash("fake-1", world_recall.text_of(
                  next(e for e in cat2 if e["short"] == "kitchen"))), str([dict(r) for r in rows]))
        out = await memory_retrieval.unified_search(bpool, 1, "cooking appliances", stores=["world"])
        hits = (out.get("world") or {}).get("results") or []
        check("Q recall's world store answers, and says how to open a hit",
              hits and hits[0]["name"] == "world.kitchen" and "data(op='describe'" not in hits[0]["open"]
              and "read(tool='data', args={'op': 'describe', 'name': 'kitchen'})" in hits[0]["open"], str(out))
        # A test run: a Host told only "data(op='describe')" sent read(data, op=select),
        # then operate(data, op='select', table=...), and gave up. A hit on a table names the
        # call that reads its rows, through the door `sql` takes.
        check("Q a table's hit names the call that reads its rows, on operate",
              hits and "operate(tool='data', args={'op': 'sql', 'sql': 'SELECT * FROM world.kitchen'}"
              in hits[0]["open"], str(hits[:1]))
        # The named calls are run as named: the read through the read door's own
        # check (the ops it admits come from the registry schema, as a session's
        # do), the rows call with the `intent` the operate lane refuses a call
        # without (runtime/session.py, operate branch), and both executed.
        from mastercode.runtime.tool_access import read_safe_ops as _rso
        from mastercode.runtime.tool_call_ledger import known_tool_schema
        from mastercode.runtime.tool_doors import read_refusal
        _data_ops = {"data": _rso("data", known_tool_schema("data"))}

        async def _opens(open_text):
            """Every call an `open` text names, run as a Host would send it."""
            import ast
            got = []
            for part in open_text.split("; its rows: "):
                try:
                    call = ast.parse(part.strip(), mode="eval").body
                    kw = {k.arg: ast.literal_eval(k.value) for k in call.keywords}
                except (SyntaxError, ValueError):
                    got.append(("unparsable", part))
                    continue
                if call.func.id == "read":
                    refused = read_refusal(kw, None, read_safe_tools=set(), read_safe_ops=_data_ops)
                    ran = await world_db.describe(kw["args"].get("name"), pool=wpool, dsn=hw_dsn)
                    got.append(("read", refused, ran.get("status")))
                else:
                    ran = await world_db.run_sql(kw["args"]["sql"], pool=wpool, dsn=hw_dsn)
                    got.append(("operate", bool(str(kw.get("intent") or "").strip()), ran.get("status"),
                                len(ran.get("rows") or [])))
            return got

        got = await _opens(hits[0]["open"]) if hits else []
        check("Q a hit's calls run as named: the read door admits the read, the rows call carries "
              "operate's required intent, and both run",
              got == [("read", None, "ok"), ("operate", True, "ok", 2)], str(got))
        await world_db.run_sql("CREATE TABLE \"Bob's Pantry\" (jar text); "
                               "COMMENT ON TABLE \"Bob's Pantry\" IS 'Jars on the shelf.'; "
                               "INSERT INTO \"Bob's Pantry\" VALUES ('honey')", pool=wpool, dsn=hw_dsn)
        odd = next((e for e in await world_db.catalog(pool=wpool, dsn=hw_dsn)
                    if e["short"] == "Bob's Pantry"), None)
        got = await _opens(world_recall.open_call(odd)) if odd else []
        check("Q a name that needs quoting (case, space, a quote) still opens as named",
              got == [("read", None, "ok"), ("operate", True, "ok", 1)],
              f"{got} {world_recall.open_call(odd) if odd else None}")
        await world_db.run_sql("DROP TABLE \"Bob's Pantry\"", pool=wpool, dsn=hw_dsn)
        text = memory_retrieval.format_unified_text(out)
        check("Q recall's text has a section for the Host's own database",
              "## Your own database" in text and "world.kitchen" in text, text[:400])
        line = await world_recall.frame_line(bpool, 1, "anything at all")
        check("Q the frame lists a small database whole, every turn",
              line and "kitchen" in line and "Pans and appliances" in line, str(line))
        check("Q the frame names the read and the rows call as the doors take them",
              line and "read(tool='data', args={'op': 'describe'" in line
              and "operate(tool='data', args={'op': 'sql'" in line and "data(op=" not in line, str(line))
        check("Q the frame's rows call carries the intent operate refuses a call without",
              line and "intent='I read the rows of <name>.')" in line, str(line))
        await world_db.run_sql("; ".join(
            f"CREATE TABLE t{i} (x int); COMMENT ON TABLE t{i} IS 'Scratch table number {i} for a long list "
            f"of things that will not fit on one line of the frame.'" for i in range(12)), pool=wpool, dsn=hw_dsn)
        line = await world_recall.frame_line(bpool, 1, "what cooking appliances do I have")
        check("Q past the frame's room, the frame names the best matches and the count",
              line and "kitchen" in line and "13 objects" in line and "t11" not in line, str(line))
        # ── the Host's own SQL errors are its outcome, not a platform fault ──
        # In a test run this statement, a syntax error in Postgres, was filed
        # as fault 'platform' and failed the run's clean-errors check.
        from mastercode.runtime import tool_call_ledger as _tcl
        bad = await world_db.run_sql(
            "CREATE TABLE IF NOT EXISTS meal_preferences (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "appliances TEXT NOT NULL); COMMENT ON TABLE meal_preferences IS 'x'",
            pool=wpool, dsn=hw_dsn)
        check("S a syntax error in the Host's own SQL is the model's outcome, and says to fix and resend",
              bad.get("reason") == "sql_error" and str(bad.get("sqlstate") or "").startswith("42")
              and _tcl.fault_class(_tcl.ERROR, bad) == "model"
              and "send it again" in bad.get("result", "") and bad.get("rolled_back") is True, str(bad))
        dup = await world_db.run_sql("CREATE TABLE kitchen (x int); COMMENT ON TABLE kitchen IS 'x'",
                                     pool=wpool, dsn=hw_dsn)
        check("S a name already taken (42P07) is the model's outcome too",
              _tcl.fault_class(_tcl.ERROR, dup) == "model", str(dup))

        plat = world_db._sql_error(asyncpg.exceptions.DiskFullError("could not extend file"))
        check("S a server-side error (53100, disk) stays a platform fault",
              _tcl.fault_class(_tcl.ERROR, plat) == "platform" and _tcl.INPUT_REFUSED not in plat, str(plat))
        r = await world_db.run_sql("SELECT 1", pool=wpool, dsn=hw_dsn)
        check("Q (the frame never fails a turn: an unreadable catalog is no line)",
              await world_recall.frame_line(bpool, 1, "x", catalog=[]) is None)
        line = await world_recall.frame_line(bpool, 1, "x", catalog=[
            {"name": "world.x", "short": "a\n\nnew line", "kind": "table", "rows_estimate": 0,
             "purpose": "one\nline", "columns": [], "column_purposes": []}])
        check("Q a name or purpose with line breaks stays on the frame's one line",
              line and "\n" not in line and "a new line: one line" in line, str(line))

        # ── aliases ──
        mid = await bpool.fetchval(
            "INSERT INTO entity_memory (entity_id, content, memory_type) "
            "VALUES (1, 'Alex likes the lentil soup from the corner shop', 'preference') RETURNING id")
        where, params = "entity_id = $1 AND status = 'active' AND t_valid_until IS NULL", [1]
        hits, ran, why = await memory_retrieval._lexical_arm(bpool, 1, "yellow dal", where_sql=where,
                                                             params=params, candidates=10)
        check("Q before an alias, words the memory does not hold miss it",
              ran and mid not in [h["id"] for h in hits], f"{why} {[h['id'] for h in hits]}")
        res = await host_tools._exec_memory_alias({"memory_id": mid, "content": "yellow dal"})
        check("Q memory_curate op='alias' adds the words", res.get("status") == "ok" and res["added"], str(res))
        hits, ran, why = await memory_retrieval._lexical_arm(bpool, 1, "yellow dal", where_sql=where,
                                                             params=params, candidates=10)
        check("Q and recall finds the memory by them", mid in [h["id"] for h in hits], f"{why} {hits}")
        check("Q the memory's own text is untouched",
              await bpool.fetchval("SELECT content FROM entity_memory WHERE id = $1", mid)
              == "Alex likes the lentil soup from the corner shop")
        again = await host_tools._exec_memory_alias({"memory_id": mid, "content": "yellow dal"})
        check("Q the same alias twice is kept once", again.get("status") == "ok" and not again["added"], str(again))
        bad = await host_tools._exec_memory_alias({"memory_id": 999999, "content": "x"})
        check("Q an alias for a memory this Host does not hold is refused",
              bad.get("status") != "ok" and "not a live memory" in str(bad), str(bad))
        await bpool.execute("DELETE FROM entity_memory WHERE id = $1", mid)
        check("Q an alias goes with its memory",
              await bpool.fetchval("SELECT count(*) FROM memory_recall_aliases WHERE memory_id = $1", mid) == 0)
    finally:
        brain_service._world_pool, brain_service._pool, brain_service._entity_id = saved
        os.environ.pop("WORLD_DATABASE_URL", None)
        await wpool.close()
        await bpool.close()


async def tool_checks(dsn, snapdir: pathlib.Path):
    """T. A function the Host writes is a tool."""
    from mastercode.runtime import direct_ops
    from mastercode.runtime.meta_tool_caps import WORLD_TOOL_CAPS, world_tool_caps
    from mastercode.runtime.tool_doors import DoorCatalog
    from server import brain_service
    hw_dsn = as_role(dsn, "host_world")
    wpool = await asyncpg.create_pool(hw_dsn, min_size=0, max_size=2)
    bpool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    saved = (brain_service._world_pool, brain_service._pool, brain_service._entity_id)
    brain_service._world_pool, brain_service._pool, brain_service._entity_id = wpool, bpool, 1
    os.environ["WORLD_DATABASE_URL"] = hw_dsn
    os.environ["WORLD_SNAPSHOT_DIR"] = str(snapdir)
    try:
        async with wpool.acquire() as c:
            await c.execute("CALL world_admin.empty_world()")
        r = await world_db.run_sql("""
            CREATE TABLE stock (item text PRIMARY KEY, qty int NOT NULL);
            COMMENT ON TABLE stock IS 'What is in the cupboard.';
            INSERT INTO stock VALUES ('rice', 1), ('beans', 6);
            CREATE FUNCTION low_stock(below integer) RETURNS SETOF text LANGUAGE sql STABLE
                AS 'SELECT item FROM world.stock WHERE qty < below ORDER BY item';
            COMMENT ON FUNCTION low_stock(integer) IS 'Items running low in the cupboard.';
            CREATE FUNCTION restock(item_name text, amount integer DEFAULT 1) RETURNS integer
                LANGUAGE sql AS 'UPDATE world.stock SET qty = qty + amount WHERE item = item_name RETURNING qty';
            COMMENT ON FUNCTION restock(text, integer) IS 'Add to an item''s count.'
        """, pool=wpool, dsn=hw_dsn)
        check("T the Host writes two functions with purposes", r["status"] == "ok", str(r))
        cat = await world_db.catalog(pool=wpool, dsn=hw_dsn)
        dc = DoorCatalog([], read_safe_tools=(), read_safe_ops={})
        added = dc.add_world(cat)
        check("T find_tools' catalog gains world:<name> entries on the operate door",
              added == 2 and dc.entries["world:low_stock"].door == "operate"
              and {"world:low_stock", "world:restock"} <= dc.searchable, str(list(dc.entries)))
        props = dc.entries["world:restock"].schema["parameters"]["properties"]
        check("T an entry's schema is the function's arguments, by name",
              set(props) == {"item_name", "amount"}, str(props))
        found = json.dumps(dc.find({"query": "which items are running low"}))
        check("T find_tools finds the function by its purpose", "world:low_stock" in found, found[:300])
        typed = DoorCatalog([], read_safe_tools=(), read_safe_ops={},
                            turn_type={"keys": [], "status": "active"})
        typed.add_world(cat)
        check("T a typed turn without the key `world:` cannot find them",
              not ({"world:low_stock", "world:restock"} & typed.searchable))
        check("T one capability row for every world:<name>, without external_api",
              world_tool_caps("world:low_stock") is WORLD_TOOL_CAPS
              and not WORLD_TOOL_CAPS["required_bits"] & 32
              and world_tool_caps("world:bad name") is None and world_tool_caps("mcp:x/y") is None)
        r = await direct_ops.run_world_tool("world:low_stock", {"below": "5"}, entity_id=1, pool=bpool)
        check("T operate runs it through the capability gate and returns its rows",
              r.get("status") == "ok" and r["rows"] == [{"low_stock": "rice"}], str(r))
        r = await direct_ops.run_world_tool("world:low_stock", {"below": "5"}, entity_id=1, pool=bpool,
                                            turn_type={"keys": [], "status": "active"})
        check("T a typed turn without the key `world:` is refused at the gate",
              r.get("status") != "ok" and "world:" in json.dumps(r), str(r))
        n = len(list(snapdir.glob("*.sql")))
        r = await direct_ops.run_world_tool("world:restock", {"item_name": "rice"}, entity_id=1, pool=bpool)
        check("T a function that can change data runs after a snapshot; defaults apply",
              r.get("status") == "ok" and r["rows"] == [{"restock": 2}] and r.get("snapshot")
              and len(list(snapdir.glob("*.sql"))) == n + 1, str(r))
        r = await world_db.call_function("low_stock", {"below": "5"}, pool=wpool, dsn=hw_dsn)
        check("T a STABLE function takes no snapshot", r.get("status") == "ok" and not r.get("snapshot"), str(r))
        r = await world_db.call_function("restock", {"item": "rice"}, pool=wpool, dsn=hw_dsn)
        check("T an argument it does not take is refused by name",
              r.get("reason") == "bad_args" and "item_name" in r["result"], str(r))
        r = await world_db.call_function("restock", {}, pool=wpool, dsn=hw_dsn)
        check("T a missing required argument is refused", r.get("reason") == "bad_args", str(r))
        r = await world_db.call_function("nothing_here", {}, pool=wpool, dsn=hw_dsn)
        check("T a function that does not exist says how to make one",
              r.get("reason") == "no_function" and "operate(tool='data', args={op:'sql'" in r["result"], str(r))
        await world_db.run_sql(
            "CREATE FUNCTION stock_below(min_qty integer) RETURNS TABLE (item text, qty integer) "
            "LANGUAGE sql STABLE AS 'SELECT item, qty FROM world.stock WHERE qty < min_qty ORDER BY item'; "
            "COMMENT ON FUNCTION stock_below(integer) IS 'Items under a count, with their counts.'",
            pool=wpool, dsn=hw_dsn)
        r = await world_db.call_function("stock_below", {"min_qty": "10"}, pool=wpool, dsn=hw_dsn)
        check("T a function returning a table takes its input by name and returns its columns",
              r.get("status") == "ok" and r["rows"] == [{"item": "beans", "qty": 6}, {"item": "rice", "qty": 2}],
              str(r))
        refusal = dc.unknown_name_refusal("meal_planner", intent="plan my meals")
        check("T an unknown tool's refusal says how to build it",
              "build it" in refusal["result"] and "world:<name>" in refusal["result"], refusal["result"])
        check("T a world:-shaped name goes on to the gate, which answers for it",
              dc.operate_refusal("world:not_built_yet", {}) is None)
        n = len(list(snapdir.glob("*.sql")))
        r = await world_db.run_sql("SELECT restock('beans', 2)", pool=wpool, dsn=hw_dsn)
        check("T SQL that names one of the Host's data-changing functions takes a snapshot",
              r["status"] == "ok" and r.get("snapshot") and len(list(snapdir.glob("*.sql"))) == n + 1, str(r))
        n = len(list(snapdir.glob("*.sql")))
        r = await world_db.run_sql("SELECT * FROM low_stock(5)", pool=wpool, dsn=hw_dsn)
        check("T ...and SQL that names only a STABLE function does not",
              r["status"] == "ok" and not r.get("snapshot") and len(list(snapdir.glob("*.sql"))) == n, str(r))
        # ── S: the Host's own SQL errors are its outcome, on the path operate takes ──
        # session.py: `operate` -> direct_ops.run_direct_op -> tools._execute_meta_tool
        # ("data", behind the capability gate) -> world_db.run_op; then _json_safe,
        # the ledger's classify and the error lane's fault_class over that dict.
        from mastercode.runtime import tool_call_ledger as _tcl
        from mastercode.runtime.tools import _json_safe

        def lane_fault(res):
            res = _json_safe(res)
            reason = _tcl.classify(res)
            return _tcl.fault_class(reason, res) if reason == _tcl.ERROR else f"reason {reason}"

        op_args = {"op": "sql", "sql": "CREATE TABLE meal_prefs (id INTEGER PRIMARY KEY AUTOINCREMENT); "
                                       "COMMENT ON TABLE meal_prefs IS 'x'"}
        r = await direct_ops.run_direct_op("data", op_args, entity_id=1, pool=bpool)
        check("S through operate's backend, a syntax error in the Host's SQL is filed 'model'",
              r.get("reason") == "sql_error" and r.get("sqlstate") == "42601"
              and lane_fault(r) == "model", str(r))
        r = await world_db.run_sql(
            "CREATE FUNCTION take(item_name text) RETURNS integer LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'there is no %', item_name; END $$; "
            "COMMENT ON FUNCTION take(text) IS 'Take one of an item out of the cupboard.'",
            pool=wpool, dsn=hw_dsn)
        check("S (the Host writes a function that refuses with RAISE)", r["status"] == "ok", str(r))
        r = await direct_ops.run_world_tool("world:take", {"item_name": "salt"}, entity_id=1, pool=bpool)
        check("S a RAISE in the Host's own function (P0001), called as world:<name>, is filed "
              "'model' and names the function to fix, not a statement",
              r.get("sqlstate") == "P0001" and lane_fault(r) == "model"
              and "world.take" in r["result"] and "call it again" in r["result"]
              and "fix the statement" not in r["result"], str(r))
        r = await world_db.run_sql("CREATE TABLE meals.prefs (x int); COMMENT ON TABLE meals.prefs IS 'x'",
                                   pool=wpool, dsn=hw_dsn)
        check("S a schema the Host named that is not there (3F000) is filed 'model'",
              r.get("sqlstate") == "3F000" and lane_fault(r) == "model", str(r))
        r = await world_db.run_sql("CREATE VIEW running_low AS SELECT item FROM stock WHERE qty < 3; "
                                   "COMMENT ON VIEW running_low IS 'Items nearly gone.'",
                                   pool=wpool, dsn=hw_dsn)
        check("S (the Host makes a view on stock)", r["status"] == "ok", str(r))
        r = await world_db.run_sql("DROP TABLE stock", pool=wpool, dsn=hw_dsn)
        check("S a DROP that a view still depends on (2BP01) is filed 'model', and the table stays",
              r.get("sqlstate") == "2BP01" and lane_fault(r) == "model"
              and await bpool.fetchval("SELECT to_regclass('world.stock') IS NOT NULL"), str(r))
        for exc, state in ((asyncpg.exceptions.DeadlockDetectedError, "40P01"),
                           (asyncpg.exceptions.SerializationError, "40001"),
                           (asyncpg.exceptions.InFailedSQLTransactionError, "25P02"),
                           (asyncpg.exceptions.InvalidCachedStatementError, "0A000"),
                           (asyncpg.exceptions.LockNotAvailableError, "55P03")):
            res = world_db._sql_error(exc("x"))
            check(f"S {state} ({exc.__name__}) is not the statement's, and stays a platform fault",
                  res.get("sqlstate") == state and lane_fault(res) == "platform", str(res))
        # Every refusal of what the Host sent is the Host's outcome too, the way
        # every other tool's refused input is (tool_call_ledger.refused). A table
        # made without its COMMENT (no_purpose) is the likely one: it was filed
        # 'platform' and would fail a clean-errors check the same way.
        refusals = {
            "no sql": await world_db.run_sql(None, pool=wpool, dsn=hw_dsn),
            "params not a list": await world_db.run_sql("SELECT 1", "x", pool=wpool, dsn=hw_dsn),
            "transaction control": await world_db.run_sql("SELECT 1; COMMIT", pool=wpool, dsn=hw_dsn),
            "params with several statements": await world_db.run_sql(
                "SELECT $1; SELECT 2", ["1"], pool=wpool, dsn=hw_dsn),
            "wrong parameter count": await world_db.run_sql("SELECT $1::int", [], pool=wpool, dsn=hw_dsn),
            "no purpose": await world_db.run_sql("CREATE TABLE nopurpose (x int)", pool=wpool, dsn=hw_dsn),
            "bad param": await world_db.run_sql("SELECT $1::int", ["not a number"], pool=wpool, dsn=hw_dsn),
            "unknown name": await world_db.describe("no_such_thing", pool=wpool, dsn=hw_dsn),
            "rows of an unknown table": await world_db.rows("no_such_thing", pool=wpool, dsn=hw_dsn),
            "rows limit not a number": await world_db.rows("kitchen", "x", pool=wpool, dsn=hw_dsn),
            "unknown op": await world_db.run_op({"op": "select"}, pool=wpool, dsn=hw_dsn),
            "unknown function": await world_db.call_function("no_such_fn", {}, pool=wpool, dsn=hw_dsn),
            "bad snapshot id": world_db.restore("not an id", hw_dsn),
        }
        for what, res in refusals.items():
            check(f"S a refused input ({what}) is the Host's outcome",
                  res.get("status") == "error" and lane_fault(res) == "model", str(res)[:300])
        check("S nothing the refusals touched was made", not await bpool.fetchval(
            "SELECT to_regclass('world.nopurpose') IS NOT NULL"))
        check("S the database being unavailable stays a platform fault",
              lane_fault(world_db._unavailable()) == "platform", str(world_db._unavailable()))

        async def answer(coro):
            """The tool's answer, or what escaped it (a raise reaches the
            session's broad handler unmarked: filed 'platform')."""
            try:
                return await coro
            except Exception as e:  # noqa: BLE001
                return {"raised": f"{type(e).__name__}: {e}"}

        # asyncpg's own DataError (22000) when it cannot ENCODE an argument: the
        # value is right for the type and _coerce passed it on as a string.
        r = await answer(world_db.run_sql("SELECT $1::interval", ["1 day"], pool=wpool, dsn=hw_dsn))
        check("S an argument asyncpg itself cannot encode (an interval passed on as a string) "
              "is not the Host's statement: platform, and no 'fix the statement'",
              r.get("sqlstate") == "22000" and "invalid input for query argument" in r.get("result", "")
              and lane_fault(r) == "platform" and "fix the statement" not in r.get("result", ""), str(r))
        r = await answer(world_db.run_sql("SELECT 1/0", pool=wpool, dsn=hw_dsn))
        check("S ...while the server's own data error (22012) is still the Host's",
              r.get("sqlstate") == "22012" and lane_fault(r) == "model", str(r))
        # An array argument of a world:<name> function (its type comes from
        # pg_type.typname, `_int4`, so _coerce's array branch runs).
        r = await world_db.run_sql(
            "CREATE FUNCTION total_of(xs int[]) RETURNS int LANGUAGE sql STABLE "
            "AS 'SELECT sum(x)::int FROM unnest(xs) x'; "
            "COMMENT ON FUNCTION total_of(int[]) IS 'The sum of some counts.'", pool=wpool, dsn=hw_dsn)
        r = await answer(world_db.call_function("total_of", {"xs": "[1,2,3]"}, pool=wpool, dsn=hw_dsn))
        check("S (a JSON list for an array argument works)",
              r.get("status") == "ok" and r.get("rows") == [{"total_of": 6}], str(r))
        for what, value in (("not JSON", "1,2,3"), ("JSON that is not a list", "5")):
            r = await answer(world_db.call_function("total_of", {"xs": value}, pool=wpool, dsn=hw_dsn))
            check(f"S an array argument that is {what} is refused as bad_param, the Host's outcome",
                  r.get("reason") == "bad_param" and lane_fault(r) == "model", str(r))
        with tempfile.TemporaryDirectory() as loopdir:
            sid = "20261017T000000000000Z-before-change"
            check("S a well-formed snapshot id that is not there is the Host's outcome",
                  lane_fault(world_db.restore(sid, hw_dsn, pathlib.Path(loopdir))) == "model")
            os.symlink(f"{sid}.sql", os.path.join(loopdir, f"{sid}.sql"))   # stat: ELOOP
            r = world_db.restore(sid, hw_dsn, pathlib.Path(loopdir))
            check("S a snapshot the folder cannot read (stat fails, not 'not there') is a platform fault",
                  r.get("status") == "error" and lane_fault(r) == "platform"
                  and "No snapshot" not in r.get("result", ""), str(r))
        r = await world_db.run_sql("CALL world_admin.empty_world()", pool=wpool, dsn=hw_dsn)
        check("T emptying world through SQL takes a snapshot first",
              r["status"] == "ok" and r.get("snapshot")
              and not await bpool.fetchval("SELECT to_regclass('world.stock') IS NOT NULL"), str(r))
    finally:
        brain_service._world_pool, brain_service._pool, brain_service._entity_id = saved
        os.environ.pop("WORLD_DATABASE_URL", None)
        await wpool.close()
        await bpool.close()


class _FakeToken:
    def __init__(self, app_id, permissions):
        self.app_id, self.permissions = app_id, permissions


class _FakeRequest:
    """What _memory_audience reads: request.state.app_token."""
    def __init__(self, token):
        self.state = type("S", (), {"app_token": token})()


async def app_checks(dsn, snapdir: pathlib.Path):
    """A. Apps on the Host's own database (routes/world.py) and the memory
    routes' audience (routes/brain.py)."""
    from fastapi import HTTPException
    from server import brain_service
    from server.applications.build_contract import COMMUNITY_ALLOWED_SCOPES
    from server.applications.scopes import PERMISSIONS
    from server.routes import brain as brain_routes
    from server.routes import world as world_routes
    hw_dsn = as_role(dsn, "host_world")
    wpool = await asyncpg.create_pool(hw_dsn, min_size=0, max_size=2)
    bpool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    saved = (brain_service._world_pool, brain_service._pool, brain_service._entity_id)
    brain_service._world_pool, brain_service._pool, brain_service._entity_id = wpool, bpool, 1
    os.environ["WORLD_DATABASE_URL"] = hw_dsn
    os.environ["WORLD_SNAPSHOT_DIR"] = str(snapdir)
    try:
        check("A use:world is a scope an app can declare", "use:world" in PERMISSIONS)
        check("A a community app cannot declare it", "use:world" not in COMMUNITY_ALLOWED_SCOPES)
        async with wpool.acquire() as c:
            await c.execute("CALL world_admin.empty_world()")
        r = await world_routes.world_sql(world_routes.WorldSql(
            sql="CREATE TABLE habits (name text PRIMARY KEY, per_week int); "
                "COMMENT ON TABLE habits IS 'Habits the person is building.'; "
                "INSERT INTO habits VALUES ('walk', 5), ('cook', 3)"))
        check("A an app's SQL runs through the same executor (purpose check included)", r["status"] == "ok", str(r))
        r = await world_routes.world_sql(world_routes.WorldSql(sql="CREATE TABLE unexplained (x int)"))
        check("A ...and an app's object without a purpose is refused too", r.get("reason") == "no_purpose", str(r))
        d = await world_routes.world_describe(None)
        check("A describe lists what an app's Host keeps",
              [o["name"] for o in d["objects"]] == ["world.habits"], str(d))
        rows = await world_routes.world_rows("habits", 1, 0)
        check("A rows pages a table", rows["status"] == "ok" and len(rows["rows"]) == 1 and rows["more"], str(rows))
        await world_db.run_sql(
            "CREATE FUNCTION bump() RETURNS int LANGUAGE sql AS 'UPDATE world.habits SET per_week = per_week + 1 RETURNING 1'; "
            "COMMENT ON FUNCTION bump() IS 'Adds one to every habit.'; "
            "CREATE VIEW habits_bumped AS SELECT name, world.bump() AS b FROM world.habits; "
            "COMMENT ON VIEW habits_bumped IS 'A view that would change data if read.'",
            pool=wpool, dsn=hw_dsn)
        before = await bpool.fetchval("SELECT sum(per_week) FROM world.habits")
        rows = await world_routes.world_rows("habits_bumped", 10, 0)
        after = await bpool.fetchval("SELECT sum(per_week) FROM world.habits")
        check("A looking at a view cannot change data (rows read in a READ ONLY transaction)",
              rows["status"] == "error" and before == after, f"{rows} {before}->{after}")
        r = await world_routes.world_sql(world_routes.WorldSql(sql="DELETE FROM habits WHERE name = 'cook'"))
        snap = r.get("snapshot")
        h = await world_routes.world_history()
        check("A history lists the snapshot the delete took", snap and snap in [s["snapshot"] for s in h["snapshots"]],
              str(h)[:300])
        r = await world_routes.world_restore(world_routes.WorldRestore(snapshot=snap))
        check("A the Data tab's Restore brings it back",
              r["status"] == "ok" and await bpool.fetchval("SELECT count(*) FROM world.habits") == 2, str(r))

        # ── the memory routes' audience ──
        untagged = await bpool.fetchval(
            "INSERT INTO entity_memory (entity_id, content, memory_type) "
            "VALUES (1, 'untagged note', 'preference') RETURNING id")
        tagged = await bpool.fetchval(
            "INSERT INTO entity_memory (entity_id, content, memory_type, metadata) "
            "VALUES (1, 'tagged note', 'preference', $1::jsonb) RETURNING id",
            json.dumps({"share_scope": ["app:notes"]}))
        app = _FakeRequest(_FakeToken("notes", ["read:brain_memory"]))
        native = _FakeRequest(_FakeToken("oracle", ["read:brain_memory"]))
        got_untagged = await brain_routes.get_memory_detail(untagged, app)
        got_tagged = await brain_routes.get_memory_detail(tagged, app)
        got_native = await brain_routes.get_memory_detail(untagged, native)
        check("A an app no longer reads an untagged memory by id",
              not (isinstance(got_untagged, dict) and got_untagged.get("content") == "untagged note"),
              str(got_untagged)[:200])
        check("A it still reads a memory tagged for it",
              isinstance(got_tagged, dict) and got_tagged.get("content") == "tagged note", str(got_tagged)[:200])
        check("A the Memory app (native) reads any memory by id",
              isinstance(got_native, dict) and got_native.get("content") == "untagged note", str(got_native)[:200])
    finally:
        brain_service._world_pool, brain_service._pool, brain_service._entity_id = saved
        os.environ.pop("WORLD_DATABASE_URL", None)
        await wpool.close()
        await bpool.close()


async def main(dsn):
    await role_checks(dsn)
    await list_checks(dsn)
    await drift_checks(dsn)
    with tempfile.TemporaryDirectory() as snapdir:
        await executor_checks(dsn, pathlib.Path(snapdir))
    await restore_checks(dsn)
    await recall_checks(dsn)
    with tempfile.TemporaryDirectory() as snapdir:
        await tool_checks(dsn, pathlib.Path(snapdir))
    with tempfile.TemporaryDirectory() as snapdir:
        await app_checks(dsn, pathlib.Path(snapdir))
    return 1 if FAILED else 0


with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, \
        kernel_schema_db(_srv, entity_id=1) as _db:
    rc = asyncio.run(main(_db))
print("RESULT: ALL PASS" if FAILED == 0 else f"RESULT: {FAILED} FAILED")
sys.exit(rc)
