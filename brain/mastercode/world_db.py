"""The Host's own schema: the one place the kernel runs SQL against `world`.

The `data` tool and
the app routes call this module and nothing else. Every statement runs on the
`host_world` lane (server/brain_service.get_world_pool, migrations/v233): a
view or function the Host wrote runs with the rights of whoever reads it, so
reading `world` as `entity` would run the Host's code as the superuser.
scripts/test_host_world_static.py fails on kernel SQL naming `world.` anywhere
else.

What the code requires of the Host is one thing: every table, view and function
in `world` carries a COMMENT saying what it is for. A transaction that leaves
one without is rolled back with the statement that fixes it. That line is what
recall finds months later.

Snapshots: before a call that could remove or reshape data, `pg_dump -n world`
runs as host_world. A restore empties `world` and loads the snapshot in ONE
psql transaction, so it lands whole or not at all. The statements that empty
`world` are declared in v233 (world_admin.empty_world), so this module carries
no DDL text of its own (scripts/test_no_runtime_ddl.py).
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import decimal
import json
import logging
import os
import re
import stat as _stat
import subprocess
import uuid
from pathlib import Path

from mastercode.runtime.tool_call_ledger import INPUT_REFUSED

# SQLSTATE classes whose error is the statement the Host wrote, not the database:
# 21 cardinality, 22 data, 23 a constraint, 2B a DROP or REVOKE something still
# depends on, 3F a schema name that is not there, 42 syntax, an unknown name or a
# wall the role holds, P0 a RAISE in a PL/pgSQL function or DO block the Host
# wrote (the one kernel RAISE host_world can reach, world_admin.empty_world,
# fires only for another role). Marked INPUT_REFUSED, so the error lane files
# them as the model's outcome (tool_call_ledger.fault_class), the way every
# other refused input is. In a test run a Host wrote SQLite (INTEGER PRIMARY
# KEY AUTOINCREMENT), got a syntax error, and the run's clean-errors check
# failed on a "platform" error row. Every other class stays a
# platform fault: disk, connection, resources and internal errors; 25 and 40
# (transaction state, deadlock, serialization), which come from how and when
# statements run, not from what one says; 55 (lock_timeout on host_world); and
# 0A, which asyncpg also raises from its own statement cache
# (InvalidCachedStatementError).
_HOST_SQL_CLASSES = ("21", "22", "23", "2B", "3F", "42", "P0")

_RESEND_SQL = "This database is PostgreSQL: fix the statement and send it again."


def _sql_error(e: Exception, resend: str = _RESEND_SQL, **extra) -> dict:
    """The answer for a PostgresError from SQL the Host sent or a function it
    wrote. `resend` says what to fix: the statement (data op='sql') or the
    function or its arguments (a world:<name> call)."""
    state = getattr(e, "sqlstate", None)
    out = {"status": "error", "reason": "sql_error", "sqlstate": state,
           "result": f"{type(e).__name__}: {e}", **extra}
    # asyncpg raises DataError (22000) from the CLIENT when it cannot encode an
    # argument, raised from the codec's own error (protocol/prepared_stmt.pyx,
    # "invalid input for query argument"). _coerce hands a type it does not
    # convert (interval, timetz, ranges ...) through as a string, so a value
    # that is right for the column fails there: not the Host's statement.
    client_side = e.__cause__ is not None and str(e).startswith("invalid input for query argument")
    if isinstance(state, str) and state[:2] in _HOST_SQL_CLASSES and not client_side:
        out[INPUT_REFUSED] = True
        out["result"] += ". Nothing was changed. " + resend
    return out

log = logging.getLogger("cseries.mastercode.world_db")

DEFAULT_TIMEOUT_S = 60
MAX_TIMEOUT_S = 900
# Rows handed back from one statement. A bigger result is still computed; the
# Host gets the first ROW_CAP and is told how to page (LIMIT/OFFSET). Protects
# the kernel's memory, not the Host's data: nothing caps what `world` holds.
ROW_CAP = 5000
PURPOSE_SHOWN = 200          # characters of a purpose line rendered in describe
KEEP_RECENT = 20             # snapshots kept regardless of age
KEEP_DAYS = 30               # plus the newest of each of the last 30 days
SNAPSHOT_TIMEOUT_S = 300

# What makes a call take a snapshot first. Read from the statements'
# own words (_statement_words: comments, quoted strings and bodies skipped), so
# "call mom" in a row's text is not a CALL, and a function body that deletes is
# not run by creating it. A false match only costs a snapshot (and an identical
# one is not kept twice, take_snapshot).
_RESHAPING_WORDS = frozenset({"DROP", "ALTER", "TRUNCATE", "DELETE", "UPDATE", "CALL", "MERGE"})
# A function of the Host's that can change data (VOLATILE) reshapes too when the
# SQL names it: `SELECT restock('rice')` removes nothing by keyword alone.
_VOLATILE_WORLD_FUNCTIONS = """
SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname = 'world' AND p.prokind IN ('f', 'p') AND p.provolatile = 'v'
"""


def _reshapes(sql: str, volatile_names=()) -> bool:
    """Whether this SQL can remove or reshape what is in `world`: a reshaping
    statement word, an anonymous DO block (it runs its body), CREATE OR REPLACE
    (it overwrites a body), or a VOLATILE world function named in it."""
    names = {str(n).upper() for n in volatile_names}
    for words in _statement_words(sql):
        if words[0] == "DO" or any(w in _RESHAPING_WORDS for w in words):
            return True
        if any(a == "OR" and b == "REPLACE" for a, b in zip(words, words[1:])):
            return True
        if names and any(w in names for w in words):
            return True
    return False


# Each call is ONE transaction, opened by the executor. A COMMIT or ROLLBACK in
# the Host's own script would end it early: in a script of several statements,
# whatever follows runs in a fresh transaction that commits on its own. That
# would skip the purpose check, the near-full read-only guard and the rollback
# on error. So a statement that ends the transaction is refused before anything
# runs. BEGIN and SAVEPOINT are harmless inside the call and are let through.
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_DOLLAR_TAG = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")


def _statement_words(sql: str) -> list[list[str]]:
    """The upper-cased words of each top-level statement, with comments, quoted
    strings, quoted names and dollar-quoted bodies skipped. Good enough to find
    the first words of each statement; never used to decide what is safe."""
    out, cur, i, n = [], [], 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
        elif ch == "/" and sql.startswith("/*", i):
            depth, i = 1, i + 2
            while i < n and depth:
                if sql.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif sql.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
        elif ch == "'":
            backslash = i > 0 and sql[i - 1] in "eE" and (i < 2 or not sql[i - 2].isalnum())
            i += 1
            while i < n:
                if backslash and sql[i] == "\\":
                    i += 2
                elif sql[i] == "'" and sql.startswith("''", i):
                    i += 2
                elif sql[i] == "'":
                    i += 1
                    break
                else:
                    i += 1
        elif ch == '"':
            j = i + 1
            while True:
                j = sql.find('"', j)
                if j < 0 or not sql.startswith('""', j):
                    break
                j += 2
            i = n if j < 0 else j + 1
        elif ch == "$" and (m := _DOLLAR_TAG.match(sql, i)) and not (i > 0 and (sql[i - 1].isalnum() or sql[i - 1] == "_")):
            end = sql.find(m.group(0), m.end())
            i = n if end < 0 else end + len(m.group(0))
        elif ch == ";":
            out.append(cur)
            cur, i = [], i + 1
        elif (m := _WORD_RE.match(sql, i)) and not (i > 0 and (sql[i - 1].isalnum() or sql[i - 1] in "_$")):
            cur.append(m.group(0).upper())
            i = m.end()
        else:
            i += 1
    out.append(cur)
    return [w for w in out if w]


def _ends_transaction(sql: str) -> str | None:
    """The first statement that would end the call's transaction, or None.
    A SQL-standard function body (BEGIN ATOMIC ... END) has its own END."""
    atomic = 0
    for words in _statement_words(sql):
        if atomic and words[0] == "END":
            atomic -= 1
            continue
        if any(a == "BEGIN" and b == "ATOMIC" for a, b in zip(words, words[1:])):
            atomic += 1
        first, second = words[0], (words[1] if len(words) > 1 else "")
        if first in ("COMMIT", "END", "ABORT"):
            return first
        if first == "ROLLBACK" and second != "TO":
            return first
        if first == "PREPARE" and second == "TRANSACTION":
            return "PREPARE TRANSACTION"
    return None


_SNAPSHOT_ID = re.compile(r"^\d{8}T\d{6}\d{6}Z-[a-z-]{1,24}$")


# The database volume's room, published by the cseries-postgres container into
# the socket folder both containers mount (entrypoint-postgres.sh,
# write_container_stats: pgdata_size, pgdata_free, ts). The kernel cannot
# measure that volume itself: it mounts only the apps claim. Nearly full means
# under 10% or under 256 MiB free; then `world` turns read-only, so one runaway
# insert cannot stop the Host writing its memory. A missing or stale file (an
# older DB image, a dead writer) means no guard, never a refusal.
LOW_ROOM_FRACTION = 0.10
LOW_ROOM_FLOOR = 256 * 1024 * 1024
DB_STATS_MAX_AGE_S = 30


def _db_room():
    """(free, size) in bytes from a fresh stats file, else None."""
    path = Path(os.environ.get("WORLD_DB_STATS")
                or Path(os.environ.get("PG_SOCKET", "/var/run/postgresql")) / ".container-stats")
    try:
        vals = dict(line.split(" ", 1) for line in path.read_text().splitlines() if " " in line)
        ts, free, size = int(vals["ts"]), int(vals["pgdata_free"]), int(vals["pgdata_size"])
    except (OSError, KeyError, ValueError):
        return None
    import time
    if time.time() - ts > DB_STATS_MAX_AGE_S or size <= 0:
        return None
    return free, size


def _nearly_full(room) -> bool:
    if room is None:
        return False
    free, size = room
    return free < max(LOW_ROOM_FRACTION * size, LOW_ROOM_FLOOR)


def _snapshot_dir() -> Path:
    root = Path(os.environ.get("CSERIES_STATE_ROOT", "/var/lib/cseries"))
    return Path(os.environ.get("WORLD_SNAPSHOT_DIR") or (root / "world-snapshots"))


def _lane(pool=None, dsn=None):
    """(pool, dsn) of the host_world lane. Never the brain pool."""
    if pool is not None and dsn is not None:
        return pool, dsn
    from server import brain_service
    return (pool if pool is not None else brain_service.get_world_pool(),
            dsn if dsn is not None else brain_service.world_dsn())


def _unavailable() -> dict:
    return {"status": "error", "reason": "unavailable",
            "result": "Your database lane (host_world) is not up on this Host. It comes up with "
                      "the role migrations/v233 asserts at boot; nothing ran."}


def _jsonable(v):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(v))} bytes>"
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    return str(v)


class _BadParam(Exception):
    pass


def _coerce(value, pgtype: str, n: int):
    """A JSON value (the tool passes strings) as the Python type asyncpg encodes
    for parameter $n, whose type Postgres inferred from the statement."""
    if value is None:
        return None
    t = pgtype[1:] if pgtype.startswith("_") else pgtype
    if pgtype.startswith("_"):                       # an array parameter
        try:
            items = value if isinstance(value, list) else json.loads(value)
        except (TypeError, ValueError) as e:         # JSONDecodeError is a ValueError
            raise _BadParam(f"${n} is an array ({pgtype}) here; pass a list or a JSON list, "
                            f"not {value!r} ({e})") from None
        if not isinstance(items, list):
            raise _BadParam(f"${n} is an array ({pgtype}) here; pass a list or a JSON list, "
                            f"not {value!r}")
        return [_coerce(x, t, n) for x in items]
    try:
        if t in ("int2", "int4", "int8", "oid"):
            return int(value)
        if t in ("float4", "float8"):
            return float(value)
        if t == "numeric":
            return decimal.Decimal(str(value))
        if t == "bool":
            if isinstance(value, bool):
                return value
            s = str(value).strip().lower()
            if s in ("true", "t", "yes", "y", "1", "on"):
                return True
            if s in ("false", "f", "no", "n", "0", "off"):
                return False
            raise ValueError(s)
        if t == "date":
            return _dt.date.fromisoformat(str(value))
        if t == "timestamp":
            return _dt.datetime.fromisoformat(str(value)).replace(tzinfo=None)
        if t == "timestamptz":
            d = _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=_dt.timezone.utc)
        if t == "time":
            return _dt.time.fromisoformat(str(value))
        if t in ("json", "jsonb"):
            return value if isinstance(value, str) else json.dumps(value)
        if t == "uuid":
            return uuid.UUID(str(value))
        if t == "bytea":
            return str(value).encode()
    except (ValueError, TypeError, decimal.InvalidOperation, json.JSONDecodeError) as e:
        raise _BadParam(f"${n} is {pgtype} here and {value!r} is not one ({e})") from None
    return str(value)


def _clamp_timeout(timeout_s) -> float:
    try:
        t = float(timeout_s) if timeout_s is not None else DEFAULT_TIMEOUT_S
    except (TypeError, ValueError):
        t = DEFAULT_TIMEOUT_S
    return max(1.0, min(t, float(MAX_TIMEOUT_S)))


# Everything in `world` that must say what it is for, and does not.
_UNEXPLAINED = """
SELECT kind, name FROM (
    SELECT CASE c.relkind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW'
                          ELSE 'TABLE' END AS kind,
           format('world.%I', c.relname) AS name,
           obj_description(c.oid, 'pg_class') AS purpose
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'world' AND c.relkind IN ('r', 'p', 'v', 'm')
    UNION ALL
    SELECT CASE p.prokind WHEN 'p' THEN 'PROCEDURE' ELSE 'FUNCTION' END,
           format('world.%I(%s)', p.proname, pg_get_function_identity_arguments(p.oid)),
           obj_description(p.oid, 'pg_proc')
      FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'world' AND p.prokind IN ('f', 'p')
) o
WHERE purpose IS NULL OR btrim(purpose) = ''
ORDER BY name
"""


class _Unexplained(Exception):
    def __init__(self, rows):
        super().__init__("unexplained objects in world")
        self.rows = rows


async def _objects_in_world(conn) -> int:
    return await conn.fetchval(
        "SELECT (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "        WHERE n.nspname = 'world') "
        "     + (SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "        WHERE n.nspname = 'world')")


async def run_sql(sql, params=None, timeout_s=None, *, pool=None, dsn=None) -> dict:
    """data(op='sql'): run the Host's SQL as host_world, in one transaction.

    One statement returns its rows (up to ROW_CAP). A script of several
    statements, with no params, returns the last command's tag. Before commit,
    anything in `world` without a purpose line rolls the whole call back."""
    if not isinstance(sql, str) or not sql.strip():
        return {"status": "error", INPUT_REFUSED: True, "result": "sql is required: the statement(s) to run in your database"}
    if params is None:
        params = []
    if not isinstance(params, list):
        return {"status": "error", INPUT_REFUSED: True, "result": "params is a list of values for $1, $2, ..."}
    ender = _ends_transaction(sql)
    if ender:
        return {"status": "error", INPUT_REFUSED: True, "reason": "transaction_control",
                "result": f"Nothing ran: each call is already one transaction, so {ender} is not "
                          "needed and would end it early. Leave out COMMIT and ROLLBACK; an error "
                          "rolls the whole call back on its own."}
    pool, dsn = _lane(pool, dsn)
    if pool is None:
        return _unavailable()
    timeout = _clamp_timeout(timeout_s)
    room = _db_room()
    full = _nearly_full(room)

    snapshot = None
    if not full:
        async with pool.acquire() as c:
            has_objects = await _objects_in_world(c)
            reshapes = has_objects and _reshapes(
                sql, [r["proname"] for r in await c.fetch(_VOLATILE_WORLD_FUNCTIONS)])
        if has_objects and reshapes:
            snap = await asyncio.to_thread(take_snapshot, "before-change", dsn)
            if snap.get("status") != "ok":
                return {"status": "error", "reason": "snapshot_failed",
                        "result": "Nothing ran: the snapshot that comes before a change that can "
                                  "remove or reshape data failed. " + snap.get("result", "")}
            snapshot = snap["snapshot"]

    import asyncpg
    rows, truncated, tag, columns, still = [], False, None, [], []
    try:
        async with pool.acquire() as conn:
            async with conn.transaction(readonly=full):
                await conn.execute("SET LOCAL search_path = world, public, pg_temp")
                # What already lacked a purpose before this call is not this
                # call's to answer for: a read must not fail on an old object.
                already = {r["name"] for r in await conn.fetch(_UNEXPLAINED)}
                stmt = None
                try:
                    async with conn.transaction():          # a savepoint: a failed Parse
                        stmt = await conn.prepare(sql, timeout=timeout)   # must not doom the call
                except asyncpg.PostgresSyntaxError as e:
                    if "multiple commands" not in str(e):
                        raise
                    if params:
                        return {"status": "error", INPUT_REFUSED: True,
                                "result": "params work with one statement; this is several. Run "
                                          "them as one script without params, or one at a time."}
                if stmt is not None:
                    types = [p.name for p in stmt.get_parameters()]
                    if len(params) != len(types):
                        return {"status": "error", INPUT_REFUSED: True,
                                "result": f"this statement takes {len(types)} parameter(s) and "
                                          f"params has {len(params)}"}
                    params = [_coerce(v, ty, i + 1) for i, (v, ty) in enumerate(zip(params, types))]
                if stmt is None:
                    tag = await conn.execute(sql, timeout=timeout)
                elif stmt.get_attributes():
                    columns = [a.name for a in stmt.get_attributes()]
                    cur = await stmt.cursor(*params, timeout=timeout)
                    got = await cur.fetch(ROW_CAP + 1, timeout=timeout)
                    truncated = len(got) > ROW_CAP
                    rows = [{k: _jsonable(v) for k, v in r.items()} for r in got[:ROW_CAP]]
                    tag = stmt.get_statusmsg() or None
                else:
                    await stmt.fetch(*params, timeout=timeout)
                    tag = stmt.get_statusmsg()
                # The Host's own SQL may have moved search_path in this
                # transaction; the check reads the real catalog, not a
                # temporary table named like it.
                await conn.execute("SET LOCAL search_path = world, public, pg_temp")
                after = await conn.fetch(_UNEXPLAINED)
                missing = [r for r in after if r["name"] not in already]
                if missing:
                    raise _Unexplained(missing)
                still = [r["name"] for r in after]
    except _Unexplained as u:
        fixes = [f"COMMENT ON {r['kind']} {r['name']} IS '<what it is for>';" for r in u.rows]
        return {"status": "error", INPUT_REFUSED: True, "reason": "no_purpose", "rolled_back": True,
                "result": "Rolled back: every table, view and function in your database says what "
                          "it is for, so recall can find it later. Add the purpose in the same call:\n"
                          + "\n".join(fixes),
                "unexplained": [r["name"] for r in u.rows], "snapshot": snapshot}
    except _BadParam as e:
        return {"status": "error", INPUT_REFUSED: True, "reason": "bad_param", "rolled_back": True,
                "result": str(e), "snapshot": snapshot}
    except asyncio.TimeoutError:
        return {"status": "error", "reason": "timeout", "rolled_back": True,
                "result": f"Cancelled after {timeout:g}s and rolled back. Pass timeout_s (up to "
                          f"{MAX_TIMEOUT_S}) for a long job.", "snapshot": snapshot}
    except asyncpg.PostgresError as e:
        if full and getattr(e, "sqlstate", None) == "25006":
            free, size = room
            return {"status": "error", "reason": "disk_full", "rolled_back": True,
                    "result": f"Your database's disk is nearly full ({free // 2**20} MiB free of "
                              f"{size // 2**20} MiB), so your own schema is read-only until there "
                              "is room. Reads work. Tell the person."}
        return _sql_error(e, rolled_back=True, snapshot=snapshot)
    out = {"status": "ok", "command": tag}
    if columns:
        out.update(columns=columns, rows=rows, row_count=len(rows))
        if truncated:
            out["truncated"] = True
            out["note"] = (f"First {ROW_CAP} rows. Page with LIMIT and OFFSET, or count with "
                           "SELECT count(*).")
    if snapshot:
        out["snapshot"] = snapshot
    if still:
        out["without_purpose"] = still
        out["purpose_note"] = ("These were made before they needed a purpose. Add one when you "
                               "next touch them, with COMMENT ON, so recall can find them.")
    return out


_DESCRIBE_RELATIONS = """
SELECT c.oid, c.relname AS name,
       CASE c.relkind WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' ELSE 'table' END AS kind,
       obj_description(c.oid, 'pg_class') AS purpose,
       GREATEST(c.reltuples, 0)::bigint AS rows_estimate,
       pg_total_relation_size(c.oid) AS bytes
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'world' AND c.relkind IN ('r', 'p', 'v', 'm')
   AND ($1::text IS NULL OR c.relname = $1)
 ORDER BY c.relname
"""
_DESCRIBE_COLUMNS = """
SELECT a.attname AS name, format_type(a.atttypid, a.atttypmod) AS type,
       a.attnotnull AS not_null, col_description(a.attrelid, a.attnum) AS purpose
  FROM pg_attribute a
 WHERE a.attrelid = $1 AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum
"""
_DESCRIBE_FUNCTIONS = """
SELECT p.proname AS name, pg_get_function_identity_arguments(p.oid) AS args,
       pg_get_function_result(p.oid) AS returns,
       CASE p.prokind WHEN 'p' THEN 'procedure' ELSE 'function' END AS kind,
       obj_description(p.oid, 'pg_proc') AS purpose
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname = 'world' AND p.prokind IN ('f', 'p')
   AND ($1::text IS NULL OR p.proname = $1)
 ORDER BY p.proname
"""


def _purpose(text, full: bool) -> str:
    t = (text or "").strip()
    return t if full or len(t) <= PURPOSE_SHOWN else t[:PURPOSE_SHOWN - 1] + "…"


async def describe(name=None, *, pool=None, dsn=None) -> dict:
    """data(op='describe'): everything in `world`, or one object in full."""
    pool, dsn = _lane(pool, dsn)
    if pool is None:
        return _unavailable()
    name = (str(name).strip() or None) if name is not None else None
    if name and name.startswith("world."):
        name = name[len("world."):]
    full = name is not None
    async with pool.acquire() as conn:
        rels = await conn.fetch(_DESCRIBE_RELATIONS, name)
        funcs = await conn.fetch(_DESCRIBE_FUNCTIONS, name)
        objects = []
        for r in rels:
            entry = {"name": f"world.{r['name']}", "kind": r["kind"],
                     "purpose": _purpose(r["purpose"], full),
                     "rows_estimate": r["rows_estimate"], "bytes": r["bytes"]}
            cols = await conn.fetch(_DESCRIBE_COLUMNS, r["oid"])
            entry["columns"] = [
                {k: v for k, v in (("name", c["name"]), ("type", c["type"]),
                                   ("not_null", c["not_null"] or None),
                                   ("purpose", (c["purpose"] or "").strip() or None)) if v is not None}
                for c in cols]
            objects.append(entry)
        for f in funcs:
            objects.append({"name": f"world.{f['name']}({f['args']})", "kind": f["kind"],
                            "returns": f["returns"], "purpose": _purpose(f["purpose"], full),
                            "call": f"operate(tool='world:{f['name']}', args={{...}}, intent='...')"
                                    if f["kind"] == "function" else None})
    if name and not objects:
        return {"status": "error", INPUT_REFUSED: True, "result": f"Nothing named {name!r} in your database. "
                                             "read(tool='data', args={'op': 'describe'}) lists everything."}
    return {"status": "ok", "objects": objects, "count": len(objects),
            "note": None if objects else "Your database is empty. Create what the request needs "
                                         "with operate(tool='data', args={op:'sql', ...}, intent='...'), each object with a COMMENT."}


async def catalog(*, pool=None, dsn=None):
    """Everything in `world`, compact, for recall and the frame to rank
    (mastercode/world_recall.py). Read on the host_world lane like every
    other read of `world`. None when the lane is not up; [] when empty."""
    pool, dsn = _lane(pool, dsn)
    if pool is None:
        return None
    out = []
    async with pool.acquire() as conn:
        for r in await conn.fetch(_DESCRIBE_RELATIONS, None):
            cols = await conn.fetch(_DESCRIBE_COLUMNS, r["oid"])
            out.append({"name": f"world.{r['name']}", "short": r["name"], "kind": r["kind"],
                        "purpose": (r["purpose"] or "").strip(),
                        "rows_estimate": r["rows_estimate"],
                        "columns": [c["name"] for c in cols],
                        "column_purposes": [(c["purpose"] or "").strip() for c in cols
                                            if (c["purpose"] or "").strip()]})
        for f in await conn.fetch(_DESCRIBE_FUNCTIONS, None):
            out.append({"name": f"world.{f['name']}({f['args']})", "short": f["name"],
                        "kind": f["kind"], "purpose": (f["purpose"] or "").strip(),
                        "args": f["args"] or "",
                        "columns": [], "column_purposes": [],
                        "call": f"operate(tool='world:{f['name']}', args={{...}}, intent='...')"
                                if f["kind"] == "function" else None})
    return out


ROWS_MAX = 500


async def rows(name, limit=100, offset=0, *, pool=None, dsn=None) -> dict:
    """One table's or view's rows, for the person's Data tab: a READ
    ONLY transaction on the host_world lane, so a view that calls a function
    cannot change anything by being looked at."""
    pool, dsn = _lane(pool, dsn)
    if pool is None:
        return _unavailable()
    short = str(name or "").strip()
    if short.startswith("world."):
        short = short[len("world."):]
    try:
        limit = max(1, min(int(limit), ROWS_MAX))
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        return {"status": "error", INPUT_REFUSED: True, "result": "limit and offset are numbers"}
    import asyncpg
    try:
        async with pool.acquire() as conn:
            async with conn.transaction(readonly=True):
                kind = await conn.fetchval(
                    "SELECT c.relkind::text FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'world' AND c.relname = $1 AND c.relkind IN ('r', 'p', 'v', 'm')",
                    short)
                if kind is None:
                    return {"status": "error", INPUT_REFUSED: True, "result": f"No table or view named {short!r} in world."}
                ident = '"' + short.replace('"', '""') + '"'
                got = await conn.fetch(f"SELECT * FROM world.{ident} LIMIT $1 OFFSET $2",
                                       limit + 1, offset, timeout=DEFAULT_TIMEOUT_S)
    except asyncio.TimeoutError:
        return {"status": "error", "result": f"Reading {short} took longer than {DEFAULT_TIMEOUT_S}s."}
    except asyncpg.PostgresError as e:
        return {"status": "error", "result": f"{type(e).__name__}: {e}"}
    out_rows = [{k: _jsonable(v) for k, v in r.items()} for r in got[:limit]]
    return {"status": "ok", "name": f"world.{short}", "rows": out_rows, "offset": offset,
            "limit": limit, "more": len(got) > limit,
            "columns": list(got[0].keys()) if got else []}


# ── snapshots ────────────────────────────────────────────────────────────────

def _psql_env() -> dict:
    """psql and pg_dump read nothing of the machine's own config."""
    env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
           "PGCONNECT_TIMEOUT": "10"}
    return env


def take_snapshot(reason: str, dsn: str | None = None, directory: Path | None = None) -> dict:
    """pg_dump of `world` as host_world, onto the apps disk. Sync (run in a thread)."""
    if dsn is None:
        _pool, dsn = _lane()
    d = directory or _snapshot_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {"status": "error", "result": f"cannot make the snapshot folder {d}: {e}"}
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    reason = re.sub(r"[^a-z-]", "-", (reason or "manual").lower())[:24] or "manual"
    sid = f"{stamp}-{reason}"
    tmp = d / f".{sid}.{uuid.uuid4().hex[:6]}.part"
    final = d / f"{sid}.sql"
    try:
        with open(tmp, "wb") as out:
            r = subprocess.run(["pg_dump", "--dbname", dsn, "--schema", "world", "--no-owner",
                                "--no-privileges", "--format", "plain"],
                               stdout=out, stderr=subprocess.PIPE, timeout=SNAPSHOT_TIMEOUT_S,
                               env=_psql_env())
        if r.returncode != 0:
            tmp.unlink(missing_ok=True)
            return {"status": "error",
                    "result": "pg_dump failed: " + r.stderr.decode(errors="replace")[-400:]}
        if not _has_restrict(tmp):
            tmp.unlink(missing_ok=True)
            return {"status": "error", "result": _NO_RESTRICT}
        newest = next(iter(_snapshots(d)), None)
        if newest is not None and _dump_digest(newest) == _dump_digest(tmp):
            # Nothing changed since the last one: keep that one, not a copy that
            # would push an older, different snapshot out of the last KEEP_RECENT.
            tmp.unlink(missing_ok=True)
            return {"status": "ok", "snapshot": newest.stem, "unchanged": True,
                    "bytes": newest.stat().st_size}
        tmp.replace(final)
    except (OSError, subprocess.TimeoutExpired) as e:
        tmp.unlink(missing_ok=True)
        return {"status": "error", "result": f"snapshot failed: {e}"}
    prune(d)
    return {"status": "ok", "snapshot": sid, "bytes": final.stat().st_size}


# A snapshot is the Host's own data, replayed by psql. pg_dump from PostgreSQL
# 17.6 on opens a plain dump with `\restrict <key>`, after which psql refuses
# every meta-command the dump's text could carry until the matching
# `\unrestrict` (CVE-2025-8714). The kernel image's client is 17.11
# (measured). A dump without it is not taken
# and not restored, so an older client fails closed rather than replaying the
# Host's text unguarded.
_NO_RESTRICT = ("snapshots need pg_dump and psql from PostgreSQL 17.6 or later (their \\restrict "
                "guard); this one has none, so nothing was kept or replayed")


def _has_restrict(path: Path) -> bool:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for _ in range(60):
                line = f.readline()
                if not line:
                    break
                if line.startswith("\\restrict "):
                    return True
    except OSError:
        return False
    return False


def _dump_digest(path: Path) -> str:
    """A dump's content, without its per-dump \\restrict key lines."""
    import hashlib
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for line in f:
                if not (line.startswith(b"\\restrict ") or line.startswith(b"\\unrestrict ")):
                    h.update(line)
    except OSError:
        return ""
    return h.hexdigest()


def _snapshots(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted((p for p in directory.glob("*.sql") if _SNAPSHOT_ID.match(p.stem)),
                  key=lambda p: p.stem, reverse=True)


def prune(directory: Path | None = None, now: _dt.datetime | None = None) -> list[str]:
    """Keep the newest KEEP_RECENT, plus the newest of each of the last KEEP_DAYS days."""
    d = directory or _snapshot_dir()
    snaps = _snapshots(d)
    keep = {p.stem for p in snaps[:KEEP_RECENT]}
    now = now or _dt.datetime.now(_dt.timezone.utc)
    seen_days = set()
    for p in snaps:
        day = p.stem[:8]
        try:
            age = now.date() - _dt.datetime.strptime(day, "%Y%m%d").date()
        except ValueError:
            continue
        if age.days < KEEP_DAYS and day not in seen_days:
            seen_days.add(day)
            keep.add(p.stem)
    removed = []
    for p in snaps:
        if p.stem not in keep:
            p.unlink(missing_ok=True)
            removed.append(p.stem)
    return removed


def history(directory: Path | None = None) -> dict:
    """data(op='history'): the snapshots, newest first."""
    d = directory or _snapshot_dir()
    out = []
    for p in _snapshots(d):
        stamp, _, reason = p.stem.partition("-")
        when = _dt.datetime.strptime(stamp, "%Y%m%dT%H%M%S%fZ").replace(tzinfo=_dt.timezone.utc)
        out.append({"snapshot": p.stem, "taken": when.isoformat(timespec="seconds"),
                    "reason": reason, "bytes": p.stat().st_size})
    return {"status": "ok", "snapshots": out, "count": len(out),
            "note": None if out else "No snapshots yet. One is taken before any change that can "
                                     "remove or reshape data."}


def _restore_script(dump_text: str) -> str:
    """The snapshot as one transaction: empty `world`, then load it. The dump's own
    CREATE of the schema is dropped (the schema stays; host_world cannot create one)."""
    # Compared word by word, so this module carries no DDL text (the no-DDL
    # gate reads every string literal in brain/).
    kept = [ln for ln in dump_text.splitlines()
            if [w.upper() for w in ln.split()] != ["CREATE", "SCHEMA", "WORLD;"]]
    return "CALL world_admin.empty_world();\n" + "\n".join(kept) + "\n"


def restore(snapshot: str, dsn: str | None = None, directory: Path | None = None) -> dict:
    """data(op='restore'): put `world` back as it was. Sync (run in a thread).

    Takes a snapshot first, so a restore can itself be undone. One psql
    transaction (ON_ERROR_STOP, --single-transaction): the snapshot lands whole
    or `world` is left as it was."""
    if not isinstance(snapshot, str) or not _SNAPSHOT_ID.match(snapshot.strip()):
        return {"status": "error", INPUT_REFUSED: True, "result": "snapshot is an id from operate(tool='data', args={op:'history'}, intent='...')"}
    if dsn is None:
        _pool, dsn = _lane()
    d = directory or _snapshot_dir()
    src = d / f"{snapshot.strip()}.sql"
    # Path.is_file() is False on ANY OSError, so a folder that cannot be read
    # would answer like an id that is not there. Only "not there" is the Host's.
    try:
        regular = _stat.S_ISREG(src.stat().st_mode)
    except FileNotFoundError:
        return {"status": "error", INPUT_REFUSED: True, "result": f"No snapshot {snapshot!r}. operate(tool='data', args={{op:'history'}}, intent='...') lists them."}
    except OSError as e:
        return {"status": "error", "result": f"the snapshot {snapshot.strip()} cannot be read: {e}"}
    if not regular:
        return {"status": "error", "result": f"the snapshot {snapshot.strip()} is not a file in {d}"}
    if not _has_restrict(src):
        return {"status": "error", "result": _NO_RESTRICT}
    before = take_snapshot("before-restore", dsn, d)
    if before.get("status") != "ok":
        return {"status": "error", "result": "Nothing changed: the snapshot taken before a restore "
                                             "failed. " + before.get("result", "")}
    script = _restore_script(src.read_text(encoding="utf-8", errors="replace"))
    try:
        r = subprocess.run(["psql", "--dbname", dsn, "-X", "-q", "--single-transaction",
                            "-v", "ON_ERROR_STOP=1", "-f", "-"],
                           input=script.encode(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           timeout=SNAPSHOT_TIMEOUT_S, env=_psql_env())
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"status": "error", "result": f"restore failed, nothing changed: {e}",
                "undo_snapshot": before["snapshot"]}
    if r.returncode != 0:
        return {"status": "error",
                "result": "restore failed, nothing changed: " + r.stderr.decode(errors="replace")[-400:],
                "undo_snapshot": before["snapshot"]}
    return {"status": "ok", "restored": snapshot.strip(), "undo_snapshot": before["snapshot"],
            "note": f"Your database is as it was at {snapshot.strip()}. To undo, restore "
                    f"{before['snapshot']}."}


# ── functions the Host wrote, as tools ───────────────────────────────
# `world:<name>` is a function in `world`, found by find_tools (tool_doors.
# add_world) and run by operate (direct_ops.run_world_tool) after the capability
# gate (meta_tool_caps.WORLD_TOOL_CAPS) and the turn type (turn_types.check_call:
# a typed turn must hold the key `world:`). Names are [a-z_][a-z0-9_]*, so no
# world tool can look like a code runner (code_exec_guard's name rules).
WORLD_TOOL_PREFIX = "world:"
WORLD_TYPE_KEY = "world:"
_FN_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def parse_tool_name(name) -> str | None:
    """The function name in `world:<name>`, or None."""
    if not isinstance(name, str) or not name.startswith(WORLD_TOOL_PREFIX):
        return None
    fn = name[len(WORLD_TOOL_PREFIX):]
    return fn if _FN_NAME.match(fn) else None


_FUNCTION_ARGS = """
SELECT p.oid, p.provolatile::text AS volatility, p.pronargdefaults AS defaults,
       COALESCE(p.proargnames, ARRAY[]::text[]) AS names,
       COALESCE(p.proargmodes::text[], ARRAY[]::text[]) AS modes,
       ARRAY(SELECT t.typname::text FROM unnest(p.proargtypes) WITH ORDINALITY u(oid, n)
               JOIN pg_type t ON t.oid = u.oid ORDER BY u.n) AS in_types,
       ARRAY(SELECT format_type(u.oid, NULL) FROM unnest(p.proargtypes) WITH ORDINALITY u(oid, n)
              ORDER BY u.n) AS in_type_names
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname = 'world' AND p.proname = $1 AND p.prokind = 'f'
"""


def function_arg_schema(identity_args: str) -> dict:
    """A tool schema for a function's arguments, from
    pg_get_function_identity_arguments ("n integer, label text"). Every value
    is passed as a string and converted to the parameter's type, as `data`'s
    params are, so each property is a string described by its type."""
    props = {}
    for part in [p.strip() for p in (identity_args or "").split(",") if p.strip()]:
        words = part.split()
        while words and words[0].upper() in ("IN", "INOUT", "VARIADIC"):
            words = words[1:]
        if len(words) < 2 or not _FN_NAME.match(words[0]):
            continue
        props[words[0]] = {"type": "string", "description": " ".join(words[1:])}
    return {"type": "object", "properties": props}


async def call_function(fn: str, args: dict | None, *, timeout_s=None,
                        pool=None, dsn=None) -> dict:
    """Run world.<fn>(...) with named arguments, as host_world. A VOLATILE
    function can change data, so a snapshot comes first (a function declared
    STABLE or IMMUTABLE skips it)."""
    if not fn or not _FN_NAME.match(fn):
        return {"status": "error", INPUT_REFUSED: True, "result": f"{fn!r} is not a function name in your database"}
    a = args if isinstance(args, dict) else {}
    pool, dsn = _lane(pool, dsn)
    if pool is None:
        return _unavailable()
    timeout = _clamp_timeout(timeout_s)
    async with pool.acquire() as conn:
        found = await conn.fetch(_FUNCTION_ARGS, fn)
        has_objects = await _objects_in_world(conn)
    if not found:
        return {"status": "error", INPUT_REFUSED: True, "reason": "no_function",
                "result": f"No function world.{fn}. read(tool='data', args={{'op': 'describe'}}) lists what you have; "
                          "operate(tool='data', args={op:'sql', ...}, intent='...') makes one."}
    if len(found) > 1:
        return {"status": "error", INPUT_REFUSED: True, "reason": "overloaded",
                "result": f"world.{fn} has {len(found)} versions; a tool needs one. Drop or rename "
                          "the others."}
    f = found[0]
    modes = list(f["modes"]) or ["i"] * len(f["in_types"])
    in_names = [n for n, m in zip(list(f["names"]) or [""] * len(modes), modes) if m in ("i", "b", "v")]
    if len(in_names) != len(f["in_types"]) or any(not n for n in in_names):
        return {"status": "error", INPUT_REFUSED: True, "reason": "unnamed_args",
                "result": f"world.{fn} has unnamed parameters; a tool passes arguments by name. "
                          "Give each parameter a name."}
    unknown = sorted(set(a) - set(in_names))
    if unknown:
        return {"status": "error", INPUT_REFUSED: True, "reason": "bad_args",
                "result": f"world.{fn} takes {', '.join(in_names) or 'no arguments'}; not {', '.join(unknown)}"}
    required = in_names[:len(in_names) - int(f["defaults"] or 0)]
    missing = [n for n in required if n not in a]
    if missing:
        return {"status": "error", INPUT_REFUSED: True, "reason": "bad_args",
                "result": f"world.{fn} needs {', '.join(missing)}"}
    snapshot = None
    if f["volatility"] == "v" and has_objects and not _nearly_full(_db_room()):
        snap = await asyncio.to_thread(take_snapshot, "before-call", dsn)
        if snap.get("status") != "ok":
            return {"status": "error", "reason": "snapshot_failed",
                    "result": "Nothing ran: the snapshot before a function that can change data "
                              "failed. " + snap.get("result", "")}
        snapshot = snap["snapshot"]
    import asyncpg
    params, parts = [], []
    try:
        for name, typ, tname in zip(in_names, f["in_types"], f["in_type_names"]):
            if name in a:
                params.append(_coerce(a[name], typ, len(params) + 1))
                parts.append(f'"{name}" => ${len(params)}::{tname}')
    except _BadParam as e:
        return {"status": "error", INPUT_REFUSED: True, "reason": "bad_param", "result": str(e)}
    sql = f'SELECT * FROM world."{fn}"({", ".join(parts)})'
    try:
        async with pool.acquire() as conn:
            async with conn.transaction(readonly=_nearly_full(_db_room())):
                await conn.execute("SET LOCAL search_path = world, public, pg_temp")
                stmt = await conn.prepare(sql, timeout=timeout)
                cur = await stmt.cursor(*params, timeout=timeout)
                got = await cur.fetch(ROW_CAP + 1, timeout=timeout)
    except asyncio.TimeoutError:
        return {"status": "error", "reason": "timeout",
                "result": f"Cancelled after {timeout:g}s and rolled back.", "snapshot": snapshot}
    except asyncpg.PostgresError as e:
        # A world:<name> call sends no statement of the Host's: what it can
        # fix is the function it wrote, or what it passed to it.
        return _sql_error(e, resend=f"The error is in world.{fn} or the arguments it was "
                                    "given: fix whichever is wrong and call it again.",
                          snapshot=snapshot)
    rows = [{k: _jsonable(v) for k, v in r.items()} for r in got[:ROW_CAP]]
    out = {"status": "ok", "function": f"world.{fn}", "rows": rows, "row_count": len(rows)}
    if len(got) > ROW_CAP:
        out["truncated"] = True
    if snapshot:
        out["snapshot"] = snapshot
    return out


async def run_op(args: dict, *, pool=None, dsn=None) -> dict:
    """The `data` tool: op = sql | describe | history | restore."""
    a = args or {}
    op = str(a.get("op") or "describe").strip().lower()
    if op == "sql":
        return await run_sql(a.get("sql"), a.get("params"), a.get("timeout_s"), pool=pool, dsn=dsn)
    if op == "describe":
        return await describe(a.get("name"), pool=pool, dsn=dsn)
    if op == "history":
        return history()
    if op == "restore":
        _p, _d = _lane(pool, dsn)
        if _p is None:
            return _unavailable()
        return await asyncio.to_thread(restore, a.get("snapshot"), _d)
    return {"status": "error", INPUT_REFUSED: True, "result": "op is sql, describe, history or restore"}


def as_json(result: dict) -> str:
    return json.dumps(result, default=str)
