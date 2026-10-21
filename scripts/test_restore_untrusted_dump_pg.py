#!/usr/bin/env python3
"""Gate: a backup's database part runs nothing as the database superuser
(server/host_restore._load_database), on a real kernel schema.

Anyone can write a backup this Host can open (it is encrypted to the owner's
public key), and an unverified one is restored when the owner says so. So the
dump here is crafted, as such a backup's would be, with:

  I. a function falsely declared IMMUTABLE that runs a program (COPY ... TO
     PROGRAM), used in an index on a kernel table: building that index as a
     superuser runs the program during the restore;
  T. a trigger planted on a kernel table, whose function runs a program: it
     would fire with a superuser's rights the next time the kernel writes;
  E. an event trigger whose function runs a program: it would fire on the
     next DDL a superuser runs (the boot's migrations);
  X. an extension the kernel's schema does not have, and a default privilege;
  P. archive text pg_restore sends as it stands (the search path line every
     connection starts with), rewritten to run a program;
  W. in `world`: a table with rows, a trigger and its function that record who
     they run as and try a program, and an index on a function that tries one.

Each program writes a marker file into the server's socket folder, which this
machine sees. Then: no marker is ever written, none of the planted objects is
in the restored database, every kernel table's rows come back as they were,
the sequences' positions too, `world` comes back owned by host_world and its
trigger runs as host_world, the index whose function tries a program is left
out and named, and the role the rows load as holds nothing after.

Fresh clone of the KERNEL's schema on TEST_PG_DSN / PEER_TEST_DSN / TEST_DSN,
else a throwaway cseries-postgres through docker.
  python3 scripts/test_restore_untrusted_dump_pg.py
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

from kernel_schema import _psql, kernel_schema_db, throwaway_server  # noqa: E402

SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    print("SKIP-LOUD: no test server (TEST_PG_DSN/PEER_TEST_DSN/TEST_DSN) and docker unavailable — "
          "this gate needs a real database to prove what a restore runs.")
    sys.exit(2)

from server import host_backup as hb  # noqa: E402
from server import host_restore as hr  # noqa: E402

FAILED = 0
# Where the server writes (its socket folder, bind-mounted from this machine).
IN_SERVER = "/var/run/postgresql"
MARKERS = ("m_index", "m_trigger", "m_event", "m_text", "m_world")


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


def val(dsn, text):
    """sql(), or the error's words: a check reads them as its detail."""
    try:
        return sql(dsn, text)
    except RuntimeError as e:
        return f"ERROR {e}"


def program(marker):
    """A plpgsql statement that runs a program writing `marker`, only in a
    restore's database (never while the crafted source is being made)."""
    return (f"IF current_database() LIKE 'restore\\_%' THEN "
            f"EXECUTE 'COPY (SELECT 1) TO PROGRAM ''touch {IN_SERVER}/{marker}'''; END IF;")


def craft(src):
    """The crafted source database: the kernel's schema and rows, plus each
    planted object. Returns the extension planted."""
    # A row only the backup holds, so "the rows came back" needs the load.
    sql(src, "INSERT INTO public.schema_migrations(version, name) VALUES (88888, 'v88888_invented')")
    sql(src, f"""
        CREATE FUNCTION public.planted_vol(int) RETURNS int VOLATILE LANGUAGE plpgsql AS $f$
            BEGIN {program('m_index')} RETURN $1; END $f$;
        -- PostgreSQL refuses COPY in a function not VOLATILE, so the one the
        -- index names is IMMUTABLE and calls one that is.
        CREATE FUNCTION public.planted_imm(int) RETURNS int IMMUTABLE LANGUAGE plpgsql AS $f$
            BEGIN RETURN public.planted_vol($1); END $f$;
        CREATE INDEX planted_idx ON public.schema_migrations (public.planted_imm(version));
        CREATE FUNCTION public.planted_trg() RETURNS trigger LANGUAGE plpgsql AS $f$
            BEGIN {program('m_trigger')} RETURN NEW; END $f$;
        CREATE TRIGGER planted_t BEFORE INSERT ON public.schema_migrations
            FOR EACH ROW EXECUTE FUNCTION public.planted_trg();
        CREATE FUNCTION public.planted_evt() RETURNS event_trigger LANGUAGE plpgsql AS $f$
            BEGIN {program('m_event')} END $f$;
        CREATE EVENT TRIGGER planted_e ON ddl_command_end EXECUTE FUNCTION public.planted_evt();
        ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO PUBLIC;
    """)
    avail = sql(src, "SELECT string_agg(name, ' ') FROM pg_available_extensions "
                     "WHERE name IN ('file_fdw', 'dblink', 'pg_trgm', 'hstore', 'citext') "
                     "AND installed_version IS NULL").split()
    ext = avail[0] if avail else None
    if ext:
        sql(src, f'CREATE EXTENSION "{ext}"')
    hw = with_db(src, urlsplit(src).path.lstrip("/"), role="host_world")
    sql(hw, f"""
        CREATE TABLE world.notes (t text NOT NULL);
        COMMENT ON TABLE world.notes IS 'Notes the person keeps (invented).';
        INSERT INTO world.notes VALUES ('buy lentils'), ('call the plumber');
        CREATE TABLE world.seen (who text);
        COMMENT ON TABLE world.seen IS 'Who the note trigger ran as.';
        CREATE FUNCTION world.note_seen() RETURNS trigger LANGUAGE plpgsql AS $f$
            BEGIN
                INSERT INTO world.seen VALUES (current_user);
                BEGIN {program('m_world')}
                EXCEPTION WHEN insufficient_privilege THEN NULL; END;
                RETURN NEW;
            END $f$;
        COMMENT ON FUNCTION world.note_seen() IS 'Records who wrote a note.';
        CREATE TRIGGER note_seen AFTER INSERT ON world.notes FOR EACH ROW EXECUTE FUNCTION world.note_seen();
        CREATE FUNCTION world.prog_vol(int) RETURNS int VOLATILE LANGUAGE plpgsql AS $f$
            BEGIN {program('m_world')} RETURN $1; END $f$;
        COMMENT ON FUNCTION world.prog_vol(int) IS 'Tries a program (invented).';
        CREATE FUNCTION world.prog_imm(int) RETURNS int IMMUTABLE LANGUAGE plpgsql AS $f$
            BEGIN RETURN world.prog_vol($1); END $f$;
        COMMENT ON FUNCTION world.prog_imm(int) IS 'Tries a program (invented).';
        CREATE INDEX notes_len ON world.notes (world.prog_imm(length(t)));
    """)
    return ext


def rewrite_search_path(dump: bytes) -> bytes:
    """P: the archive's search path line, which pg_restore sends as it stands at
    the start of every connection, replaced by a program run, at the same
    length so every offset in the archive still holds."""
    old = b"SELECT pg_catalog.set_config('search_path', '', false);\n"
    new = b"COPY(SELECT)TO PROGRAM 'touch /run/postgresql/m_text';"     # /var/run is /run
    assert old in dump and len(new) < len(old), (old in dump, len(new))
    return dump.replace(old, new + b" " * (len(old) - len(new) - 1) + b"\n", 1)


def counts(dsn):
    rows = sql(dsn, """SELECT string_agg(format('%s=%s', relname,
                         (xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM public.%I',
                          relname), false, true, '')))[1]::text), ',' ORDER BY relname)
                       FROM pg_class WHERE relnamespace = 'public'::regnamespace AND relkind = 'r'""")
    return dict(kv.split("=", 1) for kv in rows.split(",") if kv)


def restore(server, spool):
    """A fresh restore database, and _load_database of `spool` into it."""
    target = "restore_" + uuid.uuid4().hex[:8]
    sql(server, f'CREATE DATABASE "{target}"')
    hr.RESTORE_DB = target
    try:
        note, err = (hr._load_database(spool) or {}).get("note"), None
    except Exception as e:      # noqa: BLE001 — reported as the check's detail
        note, err = None, e
    return target, note, err


def main(server, src):
    sock = dict(parse_qsl(urlsplit(src).query)).get("host") or urlsplit(src).hostname
    port = dict(parse_qsl(urlsplit(src).query)).get("port") or (str(urlsplit(src).port or "") or None)
    here = pathlib.Path(sock)
    hr.RESTORE_DB, hb.PG_SOCKET, hb.PG_USER = None, sock, "entity"
    if port:
        os.environ["PGPORT"] = port
    ext = craft(src)
    check("the crafted source holds an extension the kernel's schema does not have", ext, "none available")
    want_rows = counts(src)
    want_seq = sql(src, "SELECT last_value FROM public.entity_registry_id_seq")
    want_reg = sql(src, "SELECT row_to_json(e)::text FROM public.entity_registry e ORDER BY id LIMIT 1")
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="untrusted-dump-"))
    spool, text_spool = tmp / "crafted.dump", tmp / "crafted-text.dump"
    try:
        # No --no-privileges: the default privilege goes in the archive too.
        r = subprocess.run(["pg_dump", "--dbname", src, "-Fc", "-Z0", "--no-owner", "-f", str(spool)],
                           capture_output=True, text=True)
        check("the crafted backup dumps", r.returncode == 0, r.stderr[-300:])
        text_spool.write_bytes(rewrite_search_path(spool.read_bytes()))

        print("== objects planted in the dump")
        target, note, err = restore(server, spool)
        rd = with_db(server, target)
        check("the restore loads", err is None, repr(err))
        check("I the program in the planted index's function did not run during the restore",
              not (here / "m_index").exists())
        planted = val(rd, """SELECT concat_ws(',',
            (SELECT string_agg(proname, ' ') FROM pg_proc WHERE proname LIKE 'planted%'),
            (SELECT string_agg(relname, ' ') FROM pg_class WHERE relname = 'planted_idx'),
            (SELECT string_agg(tgname, ' ') FROM pg_trigger WHERE tgname = 'planted_t'),
            (SELECT string_agg(evtname, ' ') FROM pg_event_trigger),
            (SELECT string_agg(a::text, ' ') FROM pg_default_acl, unnest(defaclacl) a
              WHERE a::text LIKE '=%'))""")
        check("I T E X no planted function, index, trigger, event trigger or default privilege to all is there",
              planted == "", planted)
        exts = val(rd, "SELECT string_agg(extname, ' ' ORDER BY extname) FROM pg_extension")
        check("X the planted extension is not there; the kernel's own are",
              ext not in exts.split() and "vector" in exts.split(), exts)
        val(rd, "INSERT INTO public.schema_migrations(version, name) VALUES (99999, 'invented')")
        val(rd, "CREATE TABLE public.after_restore (x int); DROP TABLE public.after_restore")
        check("T E a superuser's write and DDL afterwards run no program",
              not (here / "m_trigger").exists() and not (here / "m_event").exists())
        val(rd, "DELETE FROM public.schema_migrations WHERE version = 99999")
        got_rows = counts(rd)
        diff = {k: (want_rows.get(k), got_rows.get(k)) for k in set(want_rows) | set(got_rows)
                if want_rows.get(k) != got_rows.get(k)}
        check("every kernel table's rows came back, as many as the backup holds", not diff, diff)
        check("  a row only the backup held is there",
              val(rd, "SELECT name FROM public.schema_migrations WHERE version = 88888") == "v88888_invented")
        check("  the Host's own row is as it was",
              val(rd, "SELECT row_to_json(e)::text FROM public.entity_registry e ORDER BY id LIMIT 1")
              == want_reg)
        check("  and the sequences stand where they stood",
              val(rd, "SELECT last_value FROM public.entity_registry_id_seq") == want_seq)
        check("W the Host's own table came back, with its rows, owned by host_world",
              val(rd, "SELECT relowner::regrole::text || ' ' || "
                      "(SELECT count(*) FROM world.notes) FROM pg_class WHERE oid = 'world.notes'::regclass")
              == "host_world 2")
        check("W its trigger function is host_world's",
              val(rd, "SELECT proowner::regrole::text FROM pg_proc WHERE proname = 'note_seen'") == "host_world")
        check("W the index whose function tries a program is left out and named, nothing ran",
              val(rd, "SELECT count(*) FROM pg_class WHERE relname = 'notes_len'") == "0"
              and isinstance(note, str) and "notes_len" in note and not (here / "m_world").exists(), note)
        val(with_db(server, target, role="host_world"), "INSERT INTO world.notes VALUES ('water the basil')")
        check("W a write on the Host's lane fires its trigger as host_world, and no program runs",
              val(rd, "SELECT string_agg(who, ' ') FROM world.seen") == "host_world"
              and not (here / "m_world").exists())
        role = val(rd, f"""SELECT concat_ws(' ', rolsuper, rolcanlogin,
                (SELECT count(*) FROM pg_auth_members WHERE member = r.oid),
                (SELECT count(*) FROM pg_db_role_setting WHERE setrole = r.oid),
                (SELECT count(*) FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace
                    AND c.relkind IN ('r', 'S')
                    AND (has_table_privilege(r.oid, c.oid, 'INSERT') OR has_table_privilege(r.oid, c.oid, 'UPDATE'))),
                (SELECT count(*) FROM pg_proc p WHERE p.prosecdef AND has_function_privilege(r.oid, p.oid, 'EXECUTE')))
              FROM pg_roles r WHERE rolname = '{hr.LOAD_ROLE}'""")
        check("the role the rows loaded as: no superuser, cannot log in, no role, no setting, "
              "no grant left, no SECURITY DEFINER function it can call", role == "f f 0 0 0 0", role)
        sql(server, f'DROP DATABASE IF EXISTS "{target}" WITH (FORCE)')

        print("== archive text pg_restore sends as it stands")
        target, note, err = restore(server, text_spool)
        check("P the rewritten search path line runs no program", not (here / "m_text").exists(),
              "the program ran")
        check("P and the restore is refused", isinstance(err, hr.RestoreError), repr(err) + " " + str(note))
        sql(server, f'DROP DATABASE IF EXISTS "{target}" WITH (FORCE)')
    finally:
        for m in MARKERS:
            with contextlib.suppress(FileNotFoundError):
                (here / m).unlink()
        for f in (spool, text_spool):
            with contextlib.suppress(FileNotFoundError):
                f.unlink()
        with contextlib.suppress(OSError):
            tmp.rmdir()
    return 1 if FAILED else 0


with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, \
        kernel_schema_db(_srv, entity_id=1) as _db:
    rc = main(_srv, _db)
print("RESULT: ALL PASS" if FAILED == 0 else f"RESULT: {FAILED} FAILED")
sys.exit(rc)
