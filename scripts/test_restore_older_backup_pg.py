#!/usr/bin/env python3
"""Gate: a backup taken before a migration that dropped columns restores
(server/host_restore._load_database, settle), on real schemas.

The restore builds today's schema from this tree's files and loads only the
backup's rows into it. A backup from an older schema names, in its COPY,
columns a later migration dropped, and holds values a later check no longer
allows. The fixture is such a backup, made from this branch's own history:

  * the kernel's schema as it stood before v235 (scripts/kernel_schema.py
    `upto`, read from git: the schema at the fork, ledger at v228), which has
    entity_memory.dimension_vector (pgvector), dimension_scores, role,
    episode_id, extracted_at, graph_extracted_at and
    entity_registry.mastercode_version, all of which v235 drops, a memory-type
    check that allows 'fact' and 'skill_memory', which v235 retires, and
    tables (entity_episodes) v235 drops;
  * invented rows in it: a 'fact' memory with a personality vector, a taught
    'skill_memory' rule, a turn, an episode;
  * one column no migration drops (as when init-db.sql lost a column with no
    migration), with a value;
  * dumped the way server/host_backup.py dumps (pg_dump -Fc -Z0 --no-owner
    --no-privileges).

It is restored with the new code, then the boot's schema step runs over it as
the boot after the swap does (kernel_schema.complete: the backup's
schema_migrations rows came back, so v231 onwards run), then settle. Checks:
the restore loads; the columns it added are named with the database's own
type names; after the boot every row is where the newer schema keeps it (the
fact retired with its old type kept, the taught rule a preference rule, the
turn as it was), every dropped column is gone, the column no migration
dropped is dropped and named, the dropped table's rows are left out and
named, every check of today's schema is back and holds. And a column whose
type is not a built-in one (a domain the dump made, a table's row type) is
refused with the reason, before any of the backup's rows load.

Fresh clones on TEST_PG_DSN / PEER_TEST_DSN / TEST_DSN, else a throwaway
cseries-postgres through docker.
  python3 scripts/test_restore_older_backup_pg.py
"""
import contextlib
import os
import pathlib
import subprocess
import sys
import tempfile
import uuid
from urllib.parse import parse_qsl, urlsplit, urlunsplit

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from kernel_schema import _psql, complete, kernel_schema_db, throwaway_server  # noqa: E402

SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    print("SKIP-LOUD: no test server (TEST_PG_DSN/PEER_TEST_DSN/TEST_DSN) and docker unavailable — "
          "this gate needs a real database to restore an older backup into.")
    sys.exit(2)

from server import host_backup as hb  # noqa: E402
from server import host_restore as hr  # noqa: E402

# The first migration after the schema the backup is taken from.
OLDER_THAN = 235
FAILED = 0
# The restore databases this gate made, by the part of the name after restore_.
MADE = []


def check(label, cond, detail=""):
    global FAILED
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  <- {detail}"))
    if not cond:
        FAILED += 1


def with_db(dsn, db):
    p = urlsplit(dsn)
    return urlunsplit((p.scheme, p.netloc, "/" + db, p.query, p.fragment))


def sql(dsn, text):
    r = _psql(dsn, "-v", "ON_ERROR_STOP=1", "-Atc", text)
    if r.returncode != 0:
        raise RuntimeError(f"{text[:120]}: {r.stderr[-400:]}")
    return r.stdout.strip()


def val(dsn, text):
    try:
        return sql(dsn, text)
    except RuntimeError as e:
        return f"ERROR {e}"


def fill(src):
    """Invented rows in the older schema, in the columns and types it had."""
    eid = sql(src, "SELECT min(id) FROM entity_registry")
    vec = "[" + ",".join(["0.1"] * 10) + "]"
    sql(src, f"""
        INSERT INTO entity_episodes (entity_id, title, started_at)
             VALUES ({eid}, 'An invented evening', now());
        INSERT INTO entity_memory (entity_id, content, memory_type, status, metadata, dimension_vector,
                                   dimension_scores, role, extracted_at, graph_extracted_at)
             VALUES ({eid}, 'The invented kettle is blue.', 'fact', 'active', '{{}}', '{vec}',
                     '{{"warmth": 0.4}}', 'assistant', now(), now());
        INSERT INTO entity_memory (entity_id, content, memory_type, status, metadata)
             VALUES ({eid}, 'Invented rule: water the fern on Mondays.', 'skill_memory', 'active',
                     '{{"trigger": "remember this:"}}');
        INSERT INTO entity_memory (entity_id, content, memory_type, status, metadata)
             VALUES ({eid}, 'An invented turn about lentils.', 'turn', 'active', '{{}}');
        ALTER TABLE entity_memory ADD COLUMN invented_extra text;
        UPDATE entity_memory SET invented_extra = 'kept by no migration';
    """)
    return eid


def dump(src, path):
    r = subprocess.run(["pg_dump", "--dbname", src, "-Fc", "-Z0", "--no-owner", "--no-privileges",
                        "-f", str(path)], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("pg_dump: " + r.stderr[-300:])


def restore(server, spool):
    target = "restore_" + uuid.uuid4().hex[:8]
    sql(server, f'CREATE DATABASE "{target}"')
    MADE.append(target[len("restore_"):])
    hr.RESTORE_DB = target
    try:
        got, err = hr._load_database(spool), None
    except Exception as e:      # noqa: BLE001 — reported as the check's detail
        got, err = None, e
    return target, got, err


def checks_of(dsn):
    return set(sql(dsn, "SELECT string_agg(t.relname || '.' || c.conname || ':' || c.convalidated, ' ') "
                        "FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
                        "WHERE c.contype = 'c' AND t.relnamespace = 'public'::regnamespace").split())


def main(server, old, today):
    sock = dict(parse_qsl(urlsplit(old).query)).get("host") or urlsplit(old).hostname
    port = dict(parse_qsl(urlsplit(old).query)).get("port") or (str(urlsplit(old).port or "") or None)
    hb.PG_SOCKET, hb.PG_USER = sock, "entity"
    if port:
        os.environ["PGPORT"] = port
    check("the older schema has the columns v235 drops",
          val(old, "SELECT count(*) FROM pg_attribute WHERE attrelid = 'entity_memory'::regclass "
                   "AND attname IN ('dimension_vector', 'role', 'episode_id') AND NOT attisdropped") == "3")
    check("  and its ledger ends before v235",
          int(val(old, "SELECT max(version) FROM schema_migrations") or 0) < OLDER_THAN)
    fill(old)
    want_checks = checks_of(today)
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="older-backup-"))
    spool = tmp / "older.dump"
    try:
        dump(old, spool)
        print("== a backup from before v235")
        target, got, err = restore(server, spool)
        rd = with_db(server, target)
        check("the restore loads", err is None, repr(err))
        got = got if isinstance(got, dict) else {}
        added = {f"{t}.{c}": typ for t, c, typ in got.get("columns") or []}
        check("the columns the backup's COPY names and this version lacks were added for the load",
              {"entity_memory.dimension_vector", "entity_memory.role", "entity_memory.episode_id",
               "entity_registry.mastercode_version", "entity_memory.invented_extra"} <= set(added), added)
        check("  each with the database's own name of the dump's type",
              added.get("entity_memory.dimension_vector") == "public.vector(10)"
              and added.get("entity_memory.role") == "character varying(100)", added)
        check("the retired types' rows came in (their check set aside for the load)",
              val(rd, "SELECT count(*) FROM entity_memory WHERE memory_type IN ('fact', 'skill_memory')") == "2")
        check("  and the check they do not meet yet waits for the boot",
              any(n == "entity_memory_memory_type_check" for _, n, _ in got.get("checks") or []),
              got.get("checks"))
        check("the dropped table's rows are left out and named",
              "entity_episodes" in (got.get("note") or ""), got.get("note"))
        check("the role the rows loaded as cannot log in after",
              val(rd, f"SELECT rolcanlogin FROM pg_roles WHERE rolname = '{hr.LOAD_ROLE}'") == "f")

        print("== the boot after the swap: the schema step, then settle")
        if err is None:
            ran = complete(rd)
            check("the boot ran the migrations the backup had not had", ran >= 1, ran)
            try:
                note, serr = hr.settle(target, got.get("columns"), got.get("checks")), None
            except Exception as e:      # noqa: BLE001
                note, serr = None, e
            check("settle runs", serr is None, repr(serr))
            gone = val(rd, "SELECT string_agg(attname, ' ' ORDER BY attname) FROM pg_attribute "
                           "WHERE attrelid IN ('entity_memory'::regclass, 'entity_registry'::regclass) "
                           "AND attname IN ('dimension_vector', 'dimension_scores', 'role', 'episode_id', "
                           "'extracted_at', 'graph_extracted_at', 'mastercode_version', 'invented_extra') "
                           "AND NOT attisdropped")
            check("every column added for the load is gone after the boot", gone == "", gone)
            check("  the one no migration dropped is named in the note",
                  "entity_memory.invented_extra" in (note or "")
                  and "entity_memory.dimension_vector" not in (note or ""), note)
            check("the fact is where v235 puts a retired type: retired, its old type kept",
                  val(rd, "SELECT memory_type || ' ' || (metadata->>'prior_memory_type') || ' ' || status "
                          "FROM entity_memory WHERE content = 'The invented kettle is blue.'")
                  == "retired fact archived")
            check("the taught rule is a preference rule",
                  val(rd, "SELECT memory_type || ' ' || (metadata->>'rule_kind') FROM entity_memory "
                          "WHERE content LIKE 'Invented rule:%'") == "preference taught")
            check("the turn is as it was",
                  val(rd, "SELECT memory_type || ' ' || status FROM entity_memory "
                          "WHERE content = 'An invented turn about lentils.'") == "turn active")
            reg = "SELECT string_agg(id || ' ' || display_name, ',' ORDER BY id) FROM entity_registry"
            check("the Host's registry rows are there", val(rd, reg) == val(old, reg) != "",
                  (val(rd, reg), val(old, reg)))
            have = checks_of(rd)
            check("every check of today's schema is back, and holds over the rows",
                  have == want_checks, sorted(want_checks ^ have))
            check("the ledger is today's", val(rd, "SELECT max(version) FROM schema_migrations")
                  == val(today, "SELECT max(version) FROM schema_migrations"))
            check("settle again changes nothing", hr.settle(target, got.get("columns"), got.get("checks")) is None)

        print("== a column whose type is not a built-in one")
        for label, ddl in (
                ("a domain the dump made", "CREATE DOMAIN public.invented_dom AS text CHECK (VALUE <> '');"
                                           "ALTER TABLE entity_memory ADD COLUMN invented_dom_col public.invented_dom"),
                ("a table's row type", "ALTER TABLE entity_memory ADD COLUMN invented_row public.schema_migrations")):
            with kernel_schema_db(server, entity_id=1, upto=OLDER_THAN) as bad:
                sql(bad, ddl)
                path = tmp / "bad.dump"
                dump(bad, path)
                t2, g2, e2 = restore(server, path)
                check(f"{label}: refused, with the reason", isinstance(e2, hr.RestoreError)
                      and "cannot add for the load" in str(e2), repr(e2) + " " + str(g2))
                check(f"  {label}: no row of the backup loaded (the ledger is the schema step's own)",
                      val(with_db(server, t2), "SELECT max(version) FROM schema_migrations")
                      == val(today, "SELECT max(version) FROM schema_migrations"))
                path.unlink()
    finally:
        for made in MADE:
            sql(server, f'DROP DATABASE IF EXISTS "restore_{made}" WITH (FORCE)')
        with contextlib.suppress(FileNotFoundError):
            spool.unlink()
        with contextlib.suppress(OSError):
            tmp.rmdir()
    return 1 if FAILED else 0


with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, \
        kernel_schema_db(_srv, entity_id=1, upto=OLDER_THAN) as _old, \
        kernel_schema_db(_srv, entity_id=1) as _today:
    rc = main(_srv, _old, _today)
print("RESULT: ALL PASS" if FAILED == 0 else f"RESULT: {FAILED} FAILED")
sys.exit(rc)
