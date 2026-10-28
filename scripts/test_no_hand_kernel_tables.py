#!/usr/bin/env python3
# gate-scope: whole-tree — every migration and every .py, .sh and .sql file under scripts/ and tests/
"""Gate: no gate builds, patches or loosens a KERNEL table by hand.

The kernel's tables are declared in init-db.sql and migrations/ and nowhere
else, and a gate that needs one gets it from scripts/kernel_schema.py — the
schema entrypoint.sh builds, cloned per gate. A gate that CREATEs its own copy
tests the copy: two infra gates once failed on correct code because
their copies lacked columns migrations had added since (nac_finding_contain.
approval, v179; network_bastion_heartbeat.detect, v146), and nine more were
carrying copies that had not been caught yet. A copy passes on drift; that is
the failure this gate closes structurally.

HOW IT READS A GATE. Every gate under scripts/ and tests/ — .py, .sh and .sql.
  - Python: every string a gate BUILDS, found with the AST (docstrings
    excluded): a literal, an f-string, `%`, .format() and `+` chains. A value
    the gate fills in at run time is a HOLE; a hole that names a variable bound
    in the same file to a string is resolved to it (f"rig_{n}" stays rig_*),
    and so is a loop variable over a tuple of strings bound once (`for t in
    TABLES`) when nothing else in the file binds either name — by any binding
    Python has: an assignment or `+=`, another loop, `with … as`, a walrus,
    del, a parameter, `except … as`, an import, a def or class, a match
    capture (_bound_names). A
    string in a SEARCH position — either side of a comparison, the argument of
    find/count/startswith/re.search/re.compile and the like, or a variable only
    ever used that way — is text the gate looks for, not SQL it runs.
    Also built: .replace() (old text swapped for the new), a str.join of a
    list or tuple, and psycopg's sql.SQL / Identifier / Literal / Composed
    (an Identifier is a quoted name; one built at run time is a hole).
  - Shell: heredoc bodies and quoted strings, comments dropped; $VAR is
    resolved from the file's own VAR=value, else a hole; $(…) is a hole.
  - SQL handed to a shell: a string (Python or shell) that is itself a
    command — psql, sh/bash -c, docker exec … psql, kubectl … -- psql — has
    its own quoted strings and heredocs read as SQL too. Prose that merely
    mentions psql is not a command (it does not start with one).
  - The SQL is then PARSED, not matched line by line: comments stripped,
    split into statements on `;` outside quotes and dollar-quotes, tokenised
    with quoted identifiers ("public"."t") read as names. A statement counts
    only from its first word, so prose that mentions SQL mid-sentence is not
    a statement; DO blocks, function bodies and EXECUTE strings (format()'s
    %I/%s are holes) are read as SQL too.

WHAT COUNTS, on a kernel table (one init-db.sql or a migration creates, or a
migration renames something to):
  DEFINES  CREATE [TEMP|UNLOGGED] TABLE … with a column list that reads as
           columns (a name and a type of any name, a table constraint) and
           nothing but table options after it, AS, LIKE, OF, PARTITION OF;
           CREATE [MATERIALIZED] VIEW;
           SELECT … INTO; ALTER TABLE … RENAME TO a kernel name;
  PATCHES  any ALTER TABLE action (ADD COLUMN, ALTER COLUMN … DROP NOT NULL,
           DROP CONSTRAINT, DISABLE TRIGGER, row security …); CREATE INDEX /
           TRIGGER / POLICY / RULE on it;
  DROPS    DROP TABLE; DROP TRIGGER / POLICY / RULE on it; DROP INDEX of a
           kernel index; DROP SCHEMA of a kernel schema (public, the AGE
           catalog, the kernel's graph) — every kernel table goes with it;
           DROP OWNED BY a role that owns the kernel's tables or is the
           kernel's own (entity, postgres, CURRENT_USER …, host_recall);
           DROP DATABASE of the kernel's database (hostmem, or its legacy
           name); DROP EXTENSION of one the kernel installs (vector,
           age, pgcrypto), with or without CASCADE;
  EMPTIES  TRUNCATE; DELETE (or WITH … DELETE, or a DELETE in a CTE's body:
           WITH d AS (DELETE FROM t RETURNING 1) SELECT …) with no WHERE of its own;
  LOOSENS  GRANT / REVOKE on it, or on ALL TABLES IN / a kernel schema;
           ALTER INDEX of a kernel index; ALTER SCHEMA of a kernel schema.
Each name is checked against the list for what stands in that place of the
statement — tables, indexes, schemas, roles, databases or extensions — never
chosen by the words of the finding's label. A name built at run time that
could BE one of them (a bare hole, or a pattern a kernel name fits) counts as
one. A scratch table of the gate's own (_marker_probe, rig_*) is not a kernel
table and is allowed.

ALLOW lists the hand-built kernel tables that ARE the point of a gate, each
with why; an entry may name one action only (a gate that empties a table in
the clone it made is excused for that, not for a hand copy of it). An entry
that no longer matches anything fails as stale.

Run: APP_ROOT=<repo> python3 scripts/test_no_hand_kernel_tables.py
"""
from __future__ import annotations

import ast
import itertools
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
EXEMPT = {"scripts/kernel_schema.py", "scripts/test_no_hand_kernel_tables.py"}

_EMPTIES_ALL = "empties (DELETE with no WHERE)"

# (gate, kernel table[, action]) -> why a hand copy is the thing that gate proves.
ALLOW = {
    ("scripts/test_role_split_rls.sh", "kernel_secrets"):
        "creates kernel_secrets AFTER v139 on purpose: the proof is that a table born after the "
        "host_recall grant is readable by host_recall until tokens.py's REVOKE runs — that order "
        "is the hazard under test, so it cannot come from the finished schema",
    ("scripts/test_role_split_rls.sh", "nac_agent_delegations"):
        "the same proof for the delegations DDL: dropped and re-created after v139 to show the "
        "late-created table is readable until its REVOKE runs",
    ("scripts/test_role_split_rls.sh", "entity_memory"):
        "turns row security on and off on the real entity_memory (phases B-D): the RLS policy "
        "proof is the experiment, not a copy of the table",
    ("scripts/test_role_split_rls.sh", "host_recall"):
        "phase E drops what host_recall owns and is granted, then the role, on the gate's own "
        "throwaway container: exactly the state a --no-owner --no-privileges restore leaves, so it "
        "can prove the next boot recreates the role and its grants",
    # The Host's own schema: section D hand-grants host_world what v233 must take back, in the
    # kernel_schema_db clone the gate makes and drops, then proves the next
    # boot's v233 run removes it. Excused for the GRANT only.
    ("scripts/test_host_world_pg.py", "entity_memory", "loosens (GRANT on it)"):
        "section D grants host_world INSERT on entity_memory as drift, in its own clone, to prove "
        "the next boot's v233 reset takes it back",
    ("scripts/test_host_world_pg.py", "kernel_secrets", "loosens (GRANT on it)"):
        "section D grants host_world SELECT on kernel_secrets as drift, in its own clone, to prove "
        "the next boot's v233 reset takes it back",
    # A third element scopes an entry to that one action: these gates empty a
    # kernel table between scenarios, and only that is excused — a hand copy of
    # the same table in the same gate still fails.
    ("scripts/test_contacts_request_peer.py", "contacts", _EMPTIES_ALL):
        "section 5 starts from no contacts, in the kernel_schema_db clone the gate makes and drops "
        "(line 218) — a database nothing else reads",
    ("scripts/test_contain_finding_sql.py", "nac_finding_contain", _EMPTIES_ALL):
        "each section (lapse, v179 approval, late answer, reaper) starts from no orders, in the "
        "kernel_schema_db clone the gate makes and drops (line 181)",
    ("scripts/test_other_bastion_alerts.py", "network_bastion_other", _EMPTIES_ALL):
        "sections 7 and 10 start from no findings, in the kernel_schema_db clone the gate makes and "
        "drops (line 183)",
    **{("scripts/test_ship_runner_pg.py", t, _EMPTIES_ALL):
       "reset() starts every scenario from no pipelines, runs, steps or machine keys, in the "
       "kernel_schema_db clone for one Host the gate makes and drops (line 935)"
       for t in ("ship_steps", "ship_runs", "ship_pipelines", "ship_machine_keys")},
    # The CEIGAS rollup test's crash trigger, scoped to the two trigger actions:
    # it puts one on its OWN copy (a schema it creates on TEST_DATABASE_URL,
    # tables from init-db.sql and v196) for one batch, then drops it — a
    # statement failing part way is the case under test (nothing lost, nothing
    # counted twice, the re-run finishes).
    # The integrations store was a SQLite file until v201. These two build that
    # FILE, in the shape it had, to prove its rows are read into Postgres (and
    # its grants are not, on a restore): a SQLite table that shares the name,
    # never the Postgres one.
    ("scripts/test_integrations_store_pg.py", "integration_grants", "defines"):
        "builds the retired SQLite store file (M1) whose rows retire_sqlite_store copies in — "
        "the old file's table, not the kernel's, which comes from init-db.sql + v201",
    ("scripts/test_restore_carry_real_schema.py", "integration_grants", "defines"):
        "builds a pre-v201 backup's (and a pre-v201 live Host's) retired SQLite store file, which "
        "the carry reads connections and live grants from — the old file's table, not the kernel's",
    ("scripts/test_host_restore.py", "integration_grants", "defines"):
        "builds a backup's retired SQLite store file, which _carry_integration_grants reads "
        "connections from and must NOT take grants from",
    # An older backup's own shape, made in the pre-v235 clone the gate builds
    # (kernel_schema_db upto=235) and drops: a column no migration drops, and
    # columns of types the restore must refuse to add for the load.
    **{("scripts/test_restore_older_backup_pg.py", "entity_memory", f"patches (ALTER TABLE ADD COLUMN {c})"):
       "the older backup's own column, added in the pre-v235 clone it is dumped from, which the restore "
       "must add for the load (or refuse, for a type that is not a built-in one)"
       for c in ("invented_extra", "invented_dom_col", "invented_row")},
    # The credential audit rollup test's crash trigger, on its own copy
    # (tables from init-db.sql and v205).
    **{("scripts/test_vault_audit_rollup_pg.py", "vault_audit_rollup", a):
       "a crash trigger on the gate's own schema copy for one batch (the failing-statement case)"
       for a in ("patches (a trigger)", "drops (a trigger)")},
}


def _allowed(rel: str, name: str, action: str) -> tuple | None:
    """The ALLOW entry that excuses this finding, if any."""
    return next((k for k in ((rel, name, action), (rel, name)) if k in ALLOW), None)

HOLE = "\x00"
_SQL_CREATE = re.compile(r"CREATE\s+(?:UNLOGGED\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:public\.)?\"?([A-Za-z_]\w*)\"?", re.I)
_SQL_RENAME = re.compile(r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?(?:public\.)?\"?\w+\"?\s+RENAME\s+TO\s+\"?(\w+)\"?", re.I)
_SQL_INDEX = re.compile(r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?\"?([A-Za-z_]\w*)\"?\s+ON\b", re.I)


def _strip_sql_comments(sql: str) -> str:
    return re.sub(r"/\*.*?\*/", " ", re.sub(r"--[^\n]*", "", sql), flags=re.S)


def kernel_tables(root: Path = ROOT) -> set[str]:
    out: set[str] = set()
    for f in [root / "init-db.sql", *sorted((root / "migrations").glob("v*.sql"))]:
        sql = _strip_sql_comments(f.read_text(errors="replace"))
        out |= {m.group(1).lower() for m in _SQL_CREATE.finditer(sql)}
        out |= {m.group(1).lower() for m in _SQL_RENAME.finditer(sql)}
    return out


def kernel_indexes(root: Path = ROOT) -> set[str]:
    out: set[str] = set()
    for f in [root / "init-db.sql", *sorted((root / "migrations").glob("v*.sql"))]:
        out |= {m.group(1).lower() for m in _SQL_INDEX.finditer(_strip_sql_comments(f.read_text(errors="replace")))}
    return out


_SQL_SCHEMA = re.compile(r"CREATE\s+SCHEMA\s+(?:IF\s+NOT\s+EXISTS\s+)?\"?([A-Za-z_]\w*)\"?", re.I)
_SQL_GRAPH = re.compile(r"create_graph\s*\(\s*'([A-Za-z_]\w*)'", re.I)
_SQL_ROLE = re.compile(r"CREATE\s+(?:ROLE|USER)\s+\"?([A-Za-z_]\w*)\"?", re.I)
# The roles that own the kernel's tables (entrypoint.sh builds them as
# `entity`; a test server's superuser is `postgres`) and the names that mean
# "whoever is connected": DROP OWNED BY any of these drops the kernel's tables.
OWNER_ROLES = {"entity", "postgres", "current_user", "session_user", "current_role"}


def kernel_schemas(root: Path = ROOT) -> set[str]:
    """public, every schema init-db.sql or a migration creates, the AGE
    catalog when the kernel installs AGE, and the kernel's own graph."""
    out = {"public"}
    for f in [root / "init-db.sql", *sorted((root / "migrations").glob("v*.sql"))]:
        sql = _strip_sql_comments(f.read_text(errors="replace"))
        out |= {m.group(1).lower() for m in _SQL_SCHEMA.finditer(sql)}
        out |= {m.group(1).lower() for m in _SQL_GRAPH.finditer(sql)}
        if re.search(r"CREATE\s+EXTENSION\s+(?:IF\s+NOT\s+EXISTS\s+)?\"?age\b", sql, re.I):
            out.add("ag_catalog")
    return out


def kernel_roles(root: Path = ROOT) -> set[str]:
    out = set(OWNER_ROLES)
    for f in [root / "init-db.sql", *sorted((root / "migrations").glob("v*.sql"))]:
        out |= {m.group(1).lower() for m in _SQL_ROLE.finditer(_strip_sql_comments(f.read_text(errors="replace")))}
    return out


_SQL_EXTENSION = re.compile(r"CREATE\s+EXTENSION\s+(?:IF\s+NOT\s+EXISTS\s+)?\"?([A-Za-z_]\w*)\"?", re.I)


def kernel_extensions(root: Path = ROOT) -> set[str]:
    """Every extension init-db.sql or a migration installs (vector, age …):
    DROP EXTENSION … CASCADE takes every kernel column, index and graph built
    on it."""
    out: set[str] = set()
    for f in [root / "init-db.sql", *sorted((root / "migrations").glob("v*.sql"))]:
        out |= {m.group(1).lower() for m in _SQL_EXTENSION.finditer(_strip_sql_comments(f.read_text(errors="replace")))}
    return out


def kernel_databases(root: Path = ROOT) -> set[str]:
    """The database the kernel's schema lives in: the names entrypoint-postgres.sh
    creates or renames it under (ENTITY_DB_NEW / ENTITY_DB_OLD) and the ones
    entrypoint.sh waits for (`for _cand in …`)."""
    out: set[str] = set()
    pg = root / "entrypoint-postgres.sh"
    if pg.is_file():
        out |= {m.group(1).lower() for m in re.finditer(r'^ENTITY_DB_(?:NEW|OLD)="?([A-Za-z_]\w*)"?\s*$',
                                                         pg.read_text(errors="replace"), re.M)}
    ep = root / "entrypoint.sh"
    if ep.is_file():
        for m in re.finditer(r"^\s*for _cand in ([\w ]+); do\s*$", ep.read_text(errors="replace"), re.M):
            out |= {n.lower() for n in m.group(1).split()}
    return out


def kernel_pools(root: Path = ROOT) -> dict[str, set[str]]:
    """What a finding's name is matched against, by the kind of thing it names."""
    return {"table": kernel_tables(root), "index": kernel_indexes(root),
            "schema": kernel_schemas(root), "role": kernel_roles(root),
            "database": kernel_databases(root), "extension": kernel_extensions(root)}


# ── SQL: tokens and statements ──────────────────────────────────────────────

@dataclass
class Tok:
    kind: str        # word | qident | string | dollar | num | punct
    text: str


_IDENT = re.compile(r"[A-Za-z_\x00][\w$\x00]*")


def lex(sql: str) -> list[Tok]:
    out: list[Tok] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c.isspace():
            i += 1
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif c == "'" or (c in "eE" and sql.startswith("'", i + 1)):
            j = i + (2 if c != "'" else 1)
            buf = []
            while j < n:
                if sql[j] == "'" and sql.startswith("''", j):
                    buf.append("'")
                    j += 2
                elif sql[j] == "'":
                    break
                else:
                    buf.append(sql[j])
                    j += 1
            out.append(Tok("string", "".join(buf)))
            i = j + 1
        elif c == '"':
            j = i + 1
            buf = []
            while j < n:
                if sql.startswith('""', j):
                    buf.append('"')
                    j += 2
                elif sql[j] == '"':
                    break
                else:
                    buf.append(sql[j])
                    j += 1
            out.append(Tok("qident", "".join(buf)))
            i = j + 1
        elif c == "$" and (m := re.match(r"\$([A-Za-z_]\w*)?\$", sql[i:])):
            tag = m.group(0)
            j = sql.find(tag, i + len(tag))
            body_end = n if j < 0 else j
            out.append(Tok("dollar", sql[i + len(tag):body_end]))
            i = n if j < 0 else j + len(tag)
        elif m := _IDENT.match(sql, i):
            out.append(Tok("word", m.group(0)))
            i = m.end()
        elif m := re.compile(r"\d+(\.\d+)?").match(sql, i):
            out.append(Tok("num", m.group(0)))
            i = m.end()
        else:
            out.append(Tok("punct", c))
            i += 1
    return out


def statements(tokens: list[Tok]) -> list[list[Tok]]:
    out, cur = [], []
    for t in tokens:
        if t.kind == "punct" and t.text == ";":
            if cur:
                out.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        out.append(cur)
    return out


def _w(ts, i):
    return ts[i].text.lower() if i < len(ts) and ts[i].kind == "word" else None


def _p(ts, i):
    return ts[i].text if i < len(ts) and ts[i].kind == "punct" else None


def _qname(ts, i):
    """A [schema.]name at i -> (Tok of the name, next index), or (None, i)."""
    if i >= len(ts) or ts[i].kind not in ("word", "qident"):
        return None, i
    name, i = ts[i], i + 1
    while _p(ts, i) == "." and i + 1 < len(ts) and ts[i + 1].kind in ("word", "qident"):
        name, i = ts[i + 1], i + 2
    return name, i


def _close(ts, i):
    """Index just after the parenthesis opened at i, or None when unbalanced."""
    depth = 0
    for j in range(i, len(ts)):
        if _p(ts, j) == "(":
            depth += 1
        elif _p(ts, j) == ")":
            depth -= 1
            if depth == 0:
                return j + 1
    return None


_TYPES = set("""int integer int2 int4 int8 bigint smallint serial serial4 serial8 bigserial smallserial text varchar
char character boolean bool bytea jsonb json uuid timestamptz timestamp date time timetz interval numeric decimal
real double float float4 float8 inet cidr macaddr macaddr8 tsvector tsquery vector halfvec sparsevec money bit
varbit xml oid regclass regtype citext point line box polygon circle path int4range int8range tstzrange daterange
name""".split())
_COLUMN_WORDS = {"not", "null", "default", "primary", "references", "unique", "check", "generated", "collate",
                 "constraint"}
_TABLE_CONSTRAINT = {"constraint", "primary", "unique", "check", "foreign", "exclude", "like"}
_ALTER_ACTIONS = {"add", "drop", "alter", "rename", "enable", "disable", "force", "no", "set", "reset", "owner",
                  "attach", "detach", "cluster", "inherit", "of", "not", "validate", "replica", "*"}


_TABLE_TAIL = {"with", "without", "inherits", "partition", "tablespace", "using", "on", "server", "options"}


def _reads_as_columns(ts, lo, hi) -> bool:
    """Whether ts[lo:hi] (inside a CREATE TABLE's parentheses) is a column list:
    empty, or every item a table constraint or `name type …` where the type is
    a known one, an array, a hole, followed by a column keyword, or ANY name
    standing alone (an enum or domain the kernel defines: `kind my_kind`,
    `x public.my_kind`). Prose in parentheses ("the shape a pod carries") is
    not: its items run on word after word."""
    items, cur, depth = [], [], 0
    for t in ts[lo:hi]:
        if t.kind == "punct" and t.text in "([":
            depth += 1
        elif t.kind == "punct" and t.text in ")]":
            depth -= 1
        if depth == 0 and t.kind == "punct" and t.text == ",":
            items.append(cur)
            cur = []
        else:
            cur.append(t)
    items.append(cur)
    if items == [[]]:
        return True
    for it in items:
        if not it:
            return False
        if _w(it, 0) in _TABLE_CONSTRAINT:
            continue
        if it[0].kind not in ("word", "qident") or len(it) < 2:
            return False
        typ = it[1]
        rest = {_w(it, k) for k in range(2, len(it))}
        if typ.kind == "word" and (typ.text.lower() in _TYPES or HOLE in typ.text):
            continue
        if _p(it, 2) in ("[", "(") or (rest & _COLUMN_WORDS) or typ.kind == "qident":
            continue
        # A type of any name: `name type`, or `name schema.type`, alone.
        _t, after = _qname(it, 1)
        if _t is not None and (after == len(it) or _p(it, after) in ("[", "(")):
            continue
        return False
    return True


def _ends_as_table(ts, end) -> bool:
    """After a CREATE TABLE's column list: the statement ends, or a table
    option follows (WITH, INHERITS, PARTITION BY …). Prose runs on."""
    return end >= len(ts) or _w(ts, end) in _TABLE_TAIL


_GRANT_OTHER_OBJECTS = {"sequence", "function", "procedure", "routine", "database", "domain", "foreign",
                        "language", "large", "parameter", "tablespace", "type"}
_LIST_STOP = {"to", "from", "cascade", "restrict", "restart", "continue", "granted", "with"}


def _names(ts, i) -> list:
    """A comma-separated list of [ONLY] [schema.]name [*] at i."""
    out = []
    while True:
        if _w(ts, i) == "only":
            i += 1
        if _w(ts, i) in _LIST_STOP:
            break
        name, i = _qname(ts, i)
        if name is None:
            break
        out.append(name)
        if _p(ts, i) == "*":
            i += 1
        if _p(ts, i) != ",":
            break
        i += 1
    return out


def _delete_hits(ts: list[Tok], i: int):
    """DELETE FROM at i with no WHERE of its own: every row of the table goes.
    Its own WHERE is one at its own depth, before the `)` that closes the
    parentheses it stands in (a CTE's body: WITH d AS (DELETE FROM t …))."""
    w = lambda k: _w(ts, k)      # noqa: E731
    if w(i) != "delete" or w(i + 1) != "from":
        return []
    j = i + 2
    if w(j) == "only":
        j += 1
    name, j = _qname(ts, j)
    if name is None:
        return []
    depth = 0
    for k in range(j, len(ts)):
        if _p(ts, k) == "(":
            depth += 1
        elif _p(ts, k) == ")":
            depth -= 1
            if depth < 0:
                break                                   # the end of the CTE body it stands in
        elif depth == 0 and w(k) == "where":
            return []
    return [(_EMPTIES_ALL, "table", name)]


def _hits(ts: list[Tok], plpgsql: bool = False):
    """(action, kind, name Tok) for one statement's tokens. `kind` is the pool
    the name is matched against — table, index, schema, role, database or
    extension — decided by where the name stands in the statement, never by
    the words of the action's label."""
    out = []
    w = lambda i: _w(ts, i)      # noqa: E731
    first = w(0)
    if first == "create":
        i = 1
        if w(i) == "or" and w(i + 1) == "replace":
            i += 2
        while w(i) in ("global", "local", "temp", "temporary", "unlogged", "materialized", "unique", "constraint",
                       "recursive"):
            i += 1
        obj, i = w(i), i + 1
        if obj in ("table", "view"):
            if (w(i), w(i + 1), w(i + 2)) == ("if", "not", "exists"):
                i += 3
            name, i = _qname(ts, i)
            if name is not None:
                if obj == "table" and _p(ts, i) == "(":
                    end = _close(ts, i)
                    if end is not None and _reads_as_columns(ts, i + 1, end - 1) and _ends_as_table(ts, end):
                        out.append(("defines", "table", name))
                elif obj == "table" and w(i) in ("as", "like", "of", "partition"):
                    out.append(("defines", "table", name))
                elif obj == "view" and (w(i) in ("as", "with") or _p(ts, i) == "("):
                    out.append(("defines (a view)", "table", name))
        elif obj == "index":
            if w(i) == "concurrently":
                i += 1
            if (w(i), w(i + 1), w(i + 2)) == ("if", "not", "exists"):
                i += 3
            if w(i) != "on":
                i += 1                                      # the index's own name
            if w(i) == "on":
                i += 1
                if w(i) == "only":
                    i += 1
                name, i = _qname(ts, i)
                if name is not None and (_p(ts, i) == "(" or w(i) == "using"):
                    out.append(("patches (an index)", "table", name))
        elif obj in ("trigger", "policy", "rule"):
            key = "to" if obj == "rule" else "on"
            j = next((k for k in range(i, len(ts)) if w(k) == key), None)
            if j is not None:
                name, _ = _qname(ts, j + 1)
                if name is not None:
                    out.append((f"patches (a {obj})", "table", name))
    elif first == "alter" and w(1) == "table":
        i = 2
        if (w(i), w(i + 1)) == ("if", "exists"):
            i += 2
        if w(i) == "only":
            i += 1
        name, i = _qname(ts, i)
        if name is not None and (w(i) in _ALTER_ACTIONS or _p(ts, i) == "*"):
            out.append(("patches (ALTER TABLE " + " ".join(t.text for t in ts[i:i + 3]) + ")", "table", name))
            for k in range(i, len(ts) - 1):
                if w(k) == "rename" and w(k + 1) == "to":
                    target, _ = _qname(ts, k + 2)
                    if target is not None:
                        out.append(("defines (renamed to)", "table", target))
    elif first == "drop" and w(1) in ("table", "view"):
        i = 2
        if (w(i), w(i + 1)) == ("if", "exists"):
            i += 2
        names = []
        while True:
            name, i = _qname(ts, i)
            if name is None:
                break
            names.append(name)
            if _p(ts, i) != ",":
                break
            i += 1
        if names and (i >= len(ts) or w(i) in ("cascade", "restrict")):
            out += [("drops", "table", n) for n in names]
    elif first == "drop" and w(1) in ("trigger", "policy", "rule"):
        j = next((k for k in range(2, len(ts)) if w(k) == "on"), None)
        if j is not None:
            name, _ = _qname(ts, j + 1)
            if name is not None:
                out.append((f"drops (a {w(1)})", "table", name))
    elif first == "drop" and w(1) == "index":
        i = 2
        if w(i) == "concurrently":
            i += 1
        if (w(i), w(i + 1)) == ("if", "exists"):
            i += 2
        name, _ = _qname(ts, i)
        if name is not None:
            out.append(("drops (an index)", "index", name))
    elif first == "drop" and w(1) == "schema":
        i = 2
        if (w(i), w(i + 1)) == ("if", "exists"):
            i += 2
        out += [("drops (a schema, with every kernel table in it)", "schema", n) for n in _names(ts, i)]
    elif first == "drop" and w(1) == "owned" and w(2) == "by":
        out += [("drops (everything the role owns, and its grants)", "role", n) for n in _names(ts, 3)]
    elif first == "drop" and w(1) == "database":
        i = 2
        if (w(i), w(i + 1)) == ("if", "exists"):
            i += 2
        name, _ = _qname(ts, i)
        if name is not None:
            out.append(("drops (the kernel's database, every table in it)", "database", name))
    elif first == "drop" and w(1) == "extension":
        i = 2
        if (w(i), w(i + 1)) == ("if", "exists"):
            i += 2
        names = _names(ts, i)
        how = ("CASCADE: every kernel column, index and graph built on it goes too"
               if "cascade" in {_w(ts, k) for k in range(i, len(ts))} else "one the kernel installs")
        out += [(f"drops (an extension, {how})", "extension", n) for n in names]
    elif first == "truncate":
        i = 2 if w(1) == "table" else 1
        out += [("empties (TRUNCATE)", "table", n) for n in _names(ts, i)]
    elif first == "delete":
        out += _delete_hits(ts, 0)
    elif first in ("grant", "revoke"):
        j = next((k for k in range(1, len(ts)) if w(k) == "on"), None)
        if j is not None:                            # no ON: role membership, not a table
            k, kind = j + 1, w(j + 1)
            verb = first.upper()
            if kind == "all" and w(k + 2) == "in" and w(k + 3) == "schema":
                out += [(f"loosens ({verb} on every table in a schema)", "schema", n) for n in _names(ts, k + 4)]
            elif kind == "schema":
                out += [(f"loosens ({verb} on a schema)", "schema", n) for n in _names(ts, k + 1)]
            elif kind not in _GRANT_OTHER_OBJECTS:
                out += [(f"loosens ({verb} on it)", "table", n)
                        for n in _names(ts, k + 1 if kind == "table" else k)]
    elif first == "alter" and w(1) == "index":
        i = 2
        if (w(i), w(i + 1)) == ("if", "exists"):
            i += 2
        if w(i) != "all":
            name, _ = _qname(ts, i)
            if name is not None:
                out.append(("loosens (ALTER INDEX)", "index", name))
    elif first == "alter" and w(1) == "schema":
        name, _ = _qname(ts, 2)
        if name is not None:
            out.append(("loosens (ALTER SCHEMA)", "schema", name))
    elif first in ("select", "with") and not plpgsql:
        depth = 0
        for k, t in enumerate(ts):
            if t.kind == "punct" and t.text == "(":
                depth += 1
            elif t.kind == "punct" and t.text == ")":
                depth -= 1
            elif first == "with" and w(k) == "delete":
                # WITH … DELETE FROM t, or a DELETE in a CTE's body:
                # WITH d AS (DELETE FROM t RETURNING 1) SELECT … (every one)
                out += _delete_hits(ts, k)
            elif depth == 0 and w(k) == "into" and w(k - 1) not in ("insert", "merge"):
                j = k + 1
                while w(j) in ("temp", "temporary", "unlogged", "table"):
                    j += 1
                name, _ = _qname(ts, j)
                if name is not None:
                    out.append(("defines (SELECT … INTO)", "table", name))
                break
    return out


_SQLISH = re.compile(r"\b(create|alter|drop|select|with|do|truncate|grant|revoke|delete)\b", re.I)


def scan_sql(text: str, plpgsql: bool = False, depth: int = 0) -> list[tuple[str, str, Tok]]:
    """Every (action, kind, name) the SQL in `text` performs, recursing into dollar
    bodies (DO, functions) and, inside them, EXECUTE strings."""
    if not _SQLISH.search(text):
        return []                                   # no statement this gate counts can start here
    found = []
    toks = lex(text)
    for st in statements(toks):
        if plpgsql:
            # A PL/pgSQL statement may follow BEGIN/THEN/ELSE/LOOP in the same span.
            starts = [k for k in range(len(st)) if _w(st, k) in ("create", "alter", "drop", "truncate",
                                                                   "grant", "revoke", "delete")
                      and (k == 0 or _w(st, k - 1) in ("begin", "then", "else", "loop", "exception"))]
            for k in starts:
                found += _hits(st[k:], plpgsql=True)
            if depth < 3:
                for t in st:
                    if t.kind == "string":
                        found += scan_sql(re.sub(r"%[ILs]", HOLE, t.text), depth=depth + 1)
        else:
            found += _hits(st)
        if depth < 3:
            for t in st:
                if t.kind == "dollar":
                    found += scan_sql(t.text, plpgsql=True, depth=depth + 1)
    return found


def name_matches(tok: Tok, owned: set[str]) -> list[str]:
    """The kernel names `tok` is (or, with holes, could be)."""
    text = tok.text if tok.kind == "qident" else tok.text.lower()
    if HOLE not in text:
        return [text] if text in owned else []
    rx = re.compile("".join(r"\w*" if part == HOLE else re.escape(part)
                            for part in re.split(f"({HOLE})", text) if part))
    return sorted(n for n in owned if rx.fullmatch(n))


# ── Python: the strings a gate builds ───────────────────────────────────────

_NEEDLE_METHODS = {"find", "rfind", "index", "rindex", "count", "startswith", "endswith", "search", "match",
                   "fullmatch", "findall", "finditer", "sub", "subn", "split", "compile", "replace", "partition",
                   "rpartition", "removeprefix", "removesuffix", "escape"}


# Methods of a string that return the string built another way.
_STR_METHODS = {"format", "replace", "join", "strip", "lstrip", "rstrip", "lower", "upper"}
# psycopg's composable SQL: sql.SQL("…"), sql.Identifier(…), sql.Literal(…),
# sql.Composed([…]) — the way a DSN-taking gate builds names it must not splice.
_PSYCOPG_SQL = {"SQL", "Identifier", "Literal", "Composed", "Placeholder"}


def _callee(node) -> str | None:
    f = node.func if isinstance(node, ast.Call) else None
    return f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else None)


def _is_str_expr(node, bindings: dict | None = None, depth: int = 0) -> bool:
    """Whether `node` builds a string: a literal, an f-string, `%`, .format()
    or a `+` chain, a string's .replace()/.join()/… or psycopg's sql.SQL /
    Identifier / Literal / Composed — counting a variable bound in the file
    to one of those."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        return _is_str_expr(node.left, bindings, depth) or (
            isinstance(node.op, ast.Add) and _is_str_expr(node.right, bindings, depth))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _STR_METHODS:
        return _is_str_expr(node.func.value, bindings, depth)
    if isinstance(node, ast.Call) and _callee(node) in _PSYCOPG_SQL and (
            _callee(node) in ("Identifier", "Placeholder")
            or any(_is_str_expr(a, bindings, depth) or isinstance(a, (ast.List, ast.Tuple)) for a in node.args)):
        return True
    if isinstance(node, ast.Name) and bindings and depth < 3 and node.id in bindings:
        return any(_is_str_expr(v, bindings, depth + 1) for v in bindings[node.id])
    return False


def _bound_names(node) -> list[str]:
    """The names `node` binds (or unbinds), however Python does it."""
    if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
        return [node.id]                                 # =, +=, for, with … as, :=, del, comprehension
    if isinstance(node, ast.arg):
        return [node.arg]                                # def / lambda parameters, *args, **kw
    if isinstance(node, ast.ExceptHandler):
        return [node.name] if node.name else []
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, ast.alias):
        return [node.asname or node.name.split(".")[0]]  # import a.b binds a
    if isinstance(node, (ast.MatchAs, ast.MatchStar)):
        return [node.name] if node.name else []
    if isinstance(node, ast.MatchMapping):
        return [node.rest] if node.rest else []
    return []


class _PyFile:
    def __init__(self, tree: ast.AST):
        self.tree = tree
        self.bindings: dict[str, list[ast.AST]] = {}
        self.needle_ids: set[int] = set()
        self.docstrings: set[int] = set()
        self.nodes = list(ast.walk(tree))           # walked once, read four times
        for node in self.nodes:
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                body = getattr(node, "body", [])
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                        and isinstance(body[0].value.value, str):
                    self.docstrings.add(id(body[0].value))
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                self.bindings.setdefault(node.targets[0].id, []).append(node.value)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
                self.bindings.setdefault(node.target.id, []).append(node.value)
            elif isinstance(node, (ast.For, ast.comprehension)) and isinstance(node.target, ast.Name) \
                    and isinstance(node.iter, (ast.Tuple, ast.List, ast.Set)):
                # `for t in ("a", "b")`: t is each of them
                self.bindings.setdefault(node.target.id, []).extend(node.iter.elts)
            for needle in self._needles_of(node):
                self.needle_ids.add(id(needle))
        # `for t in NAMES`, NAMES a tuple of strings: t is each of them — only
        # when NAMES is bound once and t nowhere else in the file (no scopes
        # are tracked, so a name bound twice could be either). Bound counts
        # every way a name gets a value or loses one: =, +=, a loop or `with
        # … as`, a walrus, del, a function's or lambda's parameter, `except
        # … as`, an import, a def or class, a match capture.
        stores: dict[str, int] = {}
        for node in self.nodes:
            for bound in _bound_names(node):
                stores[bound] = stores.get(bound, 0) + 1
        for node in self.nodes:
            if isinstance(node, (ast.For, ast.comprehension)) and isinstance(node.target, ast.Name) \
                    and isinstance(node.iter, ast.Name) and stores.get(node.iter.id) == 1 \
                    and stores.get(node.target.id) == 1:
                vals = self.bindings.get(node.iter.id, [])
                if len(vals) == 1 and isinstance(vals[0], ast.Tuple) and vals[0].elts and all(
                        isinstance(e, ast.Constant) and isinstance(e.value, str) for e in vals[0].elts):
                    self.bindings.setdefault(node.target.id, []).extend(vals[0].elts)
        # A variable used only as a needle: what it is bound to is a needle too.
        needle_names: dict[str, bool] = {}
        for node in self.nodes:
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                needle_names[node.id] = needle_names.get(node.id, True) and id(node) in self.needle_ids
        for name, only_needle in needle_names.items():
            if only_needle:
                for v in self.bindings.get(name, []):
                    self.needle_ids.add(id(v))

    @staticmethod
    def _needles_of(node):
        if isinstance(node, ast.Compare):
            yield node.left
            yield from node.comparators
        elif isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr in _NEEDLE_METHODS and not _is_str_expr(f.value):
                yield from node.args
            elif isinstance(f, ast.Attribute) and f.attr in _NEEDLE_METHODS and _is_str_expr(f.value):
                yield from node.args                         # "x".startswith(needle)

    def templates(self, node, depth: int = 0) -> list[str]:
        """The texts `node` can build, values the gate fills in at run time as
        HOLE. Variables bound in the file to strings are followed (bounded)."""
        if depth > 6:
            return [HOLE]
        if isinstance(node, ast.Constant):
            return [node.value] if isinstance(node.value, str) else [HOLE]
        if isinstance(node, ast.JoinedStr):
            parts = [self.templates(v.value, depth + 1) if isinstance(v, ast.FormattedValue)
                     else [v.value] for v in node.values]
            return self._product(parts)
        if isinstance(node, ast.FormattedValue):
            return self.templates(node.value, depth + 1)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return self._product([self.templates(node.left, depth + 1), self.templates(node.right, depth + 1)])
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod) and _is_str_expr(node.left, self.bindings):
            args = node.right.elts if isinstance(node.right, ast.Tuple) else [node.right]
            out = []
            for fmt in self.templates(node.left, depth + 1):
                pieces = re.split(r"(%(?:\([^)]*\))?[-#0 +]*\d*(?:\.\d+)?[sdirfx%])", fmt)
                parts, k = [], 0
                for p in pieces:
                    if re.fullmatch(r"%(?:\([^)]*\))?[-#0 +]*\d*(?:\.\d+)?[sdirfx%]", p or "x"):
                        if p == "%%":
                            parts.append(["%"])
                        elif p.startswith("%(") or k >= len(args):
                            parts.append([HOLE])
                        else:
                            parts.append(self.templates(args[k], depth + 1))
                            k += 1
                    else:
                        parts.append([p])
                out += self._product(parts)
            return out[:16]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format" \
                and _is_str_expr(node.func.value, self.bindings):
            kw = {k.arg: k.value for k in node.keywords if k.arg}
            out = []
            for fmt in self.templates(node.func.value, depth + 1):
                parts, auto = [], 0
                for lit, field, _spec, _conv in __import__("string").Formatter().parse(fmt.replace(HOLE, "\x01")):
                    parts.append([lit.replace("\x01", HOLE)])
                    if field is None:
                        continue
                    if field == "":
                        field, auto = str(auto), auto + 1
                    if field.isdigit() and int(field) < len(node.args):
                        parts.append(self.templates(node.args[int(field)], depth + 1))
                    elif field in kw:
                        parts.append(self.templates(kw[field], depth + 1))
                    else:
                        parts.append([HOLE])
                out += self._product(parts)
            return out[:16]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "replace" \
                and _is_str_expr(node.func.value, self.bindings) and len(node.args) >= 2:
            olds = [t for t in self.templates(node.args[0], depth + 1) if t and HOLE not in t]
            news = self.templates(node.args[1], depth + 1)
            out = []
            for base in self.templates(node.func.value, depth + 1):
                for new in news:
                    out.append(base.replace(olds[0], new) if olds else base)
            return out[:16]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "join" \
                and _is_str_expr(node.func.value, self.bindings) and node.args:
            items = node.args[0]
            if isinstance(items, ast.Name) and len(self.bindings.get(items.id, [])) == 1:
                items = self.bindings[items.id][0]
            if not isinstance(items, (ast.List, ast.Tuple)):
                return [HOLE]
            out = []
            for sep in self.templates(node.func.value, depth + 1):
                out += [sep.join(parts) for parts in self._product_lists(
                    [self.templates(e, depth + 1) for e in items.elts])]
            return out[:16]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in ("strip", "lstrip", "rstrip", "lower", "upper") \
                and _is_str_expr(node.func.value, self.bindings):
            return [getattr(t, node.func.attr)() for t in self.templates(node.func.value, depth + 1)]
        if isinstance(node, ast.Call) and _callee(node) in _PSYCOPG_SQL:
            kind = _callee(node)
            if kind == "Placeholder":
                return [HOLE]
            if kind == "Identifier":            # a quoted (schema.)name; a run-time part is a hole
                parts = [[f'"{t}"' for t in self.templates(a, depth + 1)] for a in node.args] or [[HOLE]]
                return [".".join(c) for c in self._product_lists(parts)]
            if kind == "Literal":
                return [f"'{t}'" for t in self.templates(node.args[0], depth + 1)] if node.args else [HOLE]
            if kind == "Composed":
                items = node.args[0] if node.args else None
                if not isinstance(items, (ast.List, ast.Tuple)):
                    return [HOLE]
                return ["".join(c) for c in self._product_lists(
                    [self.templates(e, depth + 1) for e in items.elts])]
            return self.templates(node.args[0], depth + 1) if node.args else [HOLE]
        if isinstance(node, ast.Name) and node.id in self.bindings:
            vals = self.bindings[node.id]
            if all(_is_str_expr(v, self.bindings, 1) or isinstance(v, ast.Name) for v in vals):
                out = []
                for v in vals:
                    out += self.templates(v, depth + 1)
                return out[:16] or [HOLE]
        return [HOLE]

    @staticmethod
    def _product(parts: list[list[str]]) -> list[str]:
        return ["".join(c) for c in itertools.islice(itertools.product(*parts), 16)]

    @staticmethod
    def _product_lists(parts: list[list[str]]) -> list[tuple[str, ...]]:
        return list(itertools.islice(itertools.product(*parts), 16))

    def built_strings(self):
        """(line, text) for every outermost string-building expression that is
        not a docstring and not in a search position."""
        inner: set[int] = set()
        outer = []
        for node in self.nodes:                     # breadth-first: an outer string comes first
            if id(node) not in inner and not isinstance(node, ast.Name) and _is_str_expr(node, self.bindings):
                outer.append(node)
                for sub in ast.walk(node):
                    if sub is not node:
                        inner.add(id(sub))
        for node in outer:
            if id(node) in self.docstrings or id(node) in self.needle_ids:
                continue
            for text in self.templates(node):
                yield node.lineno, text


def python_units(src: str):
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return
    for line, text in _PyFile(tree).built_strings():
        yield line, text
        yield from ((line, t) for _n, t in command_units(text))


# ── Shell: heredocs and quoted strings ──────────────────────────────────────

_HEREDOC = re.compile(r"<<(-?)\s*(['\"]?)([A-Za-z_]\w*)\2")


def shell_units(src: str):
    lines = src.splitlines()
    assigns: dict[str, set[str]] = {}
    for ln in lines:
        m = re.match(r"\s*(?:export\s+|local\s+|readonly\s+)?([A-Za-z_]\w*)=('([^']*)'|\"([^\"$`]*)\"|([^\s;'\"$`]*))\s*(?:;|$|#)", ln)
        if m:
            assigns.setdefault(m.group(1), set()).add(next(g for g in m.group(3, 4, 5) if g is not None))

    def expand(text: str) -> str:
        text = re.sub(r"\$\((?:[^()]|\([^()]*\))*\)|`[^`]*`", HOLE, text)

        def var(m):
            vals = assigns.get(m.group(1) or m.group(2))
            return next(iter(vals)) if vals and len(vals) == 1 else HOLE
        return re.sub(r"\$\{([A-Za-z_]\w*)[^}]*\}|\$([A-Za-z_]\w*|\d|[@*#?$!])", var, text)

    body_lines: set[int] = set()
    i = 0
    while i < len(lines):
        for m in _HEREDOC.finditer(lines[i]):
            tag, strip = m.group(3), m.group(1) == "-"
            j = i + 1
            while j < len(lines) and (lines[j].lstrip("\t") if strip else lines[j]) != tag:
                j += 1
            body = "\n".join(lines[i + 1:j])
            yield i + 2, (body if m.group(2) else expand(body))
            yield from ((i + 2 + n, t) for n, t in _quoted(body, expand))
            body_lines |= set(range(i + 1, j + 1))
        i += 1
    rest = "\n".join("" if n in body_lines else ln for n, ln in enumerate(lines))
    for n, t in _quoted(rest, expand):
        yield n + 1, t
        yield from ((n + 1, inner) for _m, inner in command_units(t, 1))


# A string that IS a command which hands SQL on: psql, a shell's -c, or
# either behind docker exec / kubectl exec / env / sudo / timeout. Prose that
# mentions psql does not START with one, so it is not read this way.
_COMMAND_START = re.compile(
    r"^\s*(?:[A-Za-z_]\w*=\S*\s+)*"
    r"(?:(?:sudo|env|nohup|exec)\s+|timeout\s+\S+\s+|docker\s+(?:exec|run)\b[^|;&]*?\s"
    r"|kubectl\b[^|;&]*?\s--\s+)*"
    r"(?:\S*/)?(?:psql|bash|sh|dash)\b")


def command_units(text: str, depth: int = 0):
    """(line, SQL text) for the quoted strings and heredocs inside `text` when
    `text` is itself a shell command that hands SQL on (see _COMMAND_START),
    recursively (sh -c "psql -c '…'"). Run-time values are holes."""
    if depth > 2 or not _COMMAND_START.match(text):
        return
    for line, inner in shell_units(text):
        yield line, inner
        yield from command_units(inner, depth + 1)


def _quoted(text: str, expand):
    """(line index, content) for each quoted string in shell text, comments
    dropped."""
    i, n, line = 0, len(text), 0
    while i < n:
        c = text[i]
        if c == "\n":
            line += 1
            i += 1
        elif c == "#" and (i == 0 or text[i - 1].isspace()):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif c == "\\":
            i += 2
        elif c == "'":
            j = text.find("'", i + 1)
            j = n if j < 0 else j
            yield line, text[i + 1:j]
            line += text.count("\n", i, j)
            i = j + 1
        elif c == '"':
            j, buf = i + 1, []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    buf.append(text[j + 1] if text[j + 1] in '"\\$`' else text[j:j + 2])
                    j += 2
                else:
                    buf.append(text[j])
                    j += 1
            yield line, expand("".join(buf))
            line += text.count("\n", i, j)
            i = j + 1
        else:
            i += 1


# ── the gate ────────────────────────────────────────────────────────────────

def file_findings(rel: str, src: str, pools: dict[str, set[str]]):
    if rel.endswith(".py"):
        units = python_units(src)
    elif rel.endswith(".sh"):
        units = shell_units(src)
    else:
        units = [(1, src)]
    seen = set()
    for line, text in units:
        for action, kind, tok in scan_sql(text):
            names = name_matches(tok, pools[kind])
            if not names:
                continue
            if HOLE in tok.text:
                pattern = tok.text.replace(HOLE, "…")
                found = [(pattern, f"a name built at run time ({pattern!r}) that could be "
                                   f"{len(names)} kernel {kind}(s): {', '.join(names[:4])}"
                                   + (", …" if len(names) > 4 else ""))]
            else:
                found = [(n, n) for n in names]
            for name, shown in found:
                if (line, action, shown) not in seen:
                    seen.add((line, action, shown))
                    yield line, action, name, shown


def hand_copies(root: Path = ROOT, pools: dict[str, set[str]] | None = None):
    pools = pools if pools is not None else kernel_pools(root)
    found = []
    for d in ("scripts", "tests"):
        for f in sorted(p for p in (root / d).rglob("*") if p.suffix in (".py", ".sh", ".sql") and p.is_file()):
            rel = f.relative_to(root).as_posix()
            if rel in EXEMPT or "/node_modules/" in rel:
                continue
            for line, action, name, shown in file_findings(rel, f.read_text(errors="replace"), pools):
                found.append((rel, line, action, name, shown))
    return found


def _probe(src: str, kind: str, pools) -> list[tuple[str, str]]:
    return sorted({(a, n) for _l, a, n, _s in file_findings("probe." + kind, src, pools)})


def main() -> int:
    pools = kernel_pools()
    owned, indexes = pools["table"], pools["index"]
    fails = 0

    def check(label, ok, detail=""):
        nonlocal fails
        print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"\n      {detail}" if detail and not ok else ""))
        fails += 0 if ok else 1

    check(f"the kernel's tables are read from init-db.sql and migrations/ ({len(owned)})",
          {"network_bastion_heartbeat", "mcp_client_tokens", "nac_finding_contain", "entity_registry"} <= owned,
          sorted(owned)[:10])
    check(f"the kernel's schemas and roles are read too ({sorted(pools['schema'])}, {sorted(pools['role'])})",
          {"public", "ag_catalog"} <= pools["schema"] and {"host_recall", "entity"} <= pools["role"])
    check(f"...and its databases and extensions ({sorted(pools['database'])}, {sorted(pools['extension'])})",
          {"hostmem", "parallax"} <= pools["database"] and {"vector", "age"} <= pools["extension"])
    copies = hand_copies(ROOT, pools)
    allowed = [c for c in copies if _allowed(c[0], c[3], c[2])]
    bad = [c for c in copies if not _allowed(c[0], c[3], c[2])]
    check("no gate defines, patches or drops a kernel table by hand — gates get the kernel's schema from "
          "scripts/kernel_schema.py", not bad,
          "\n      ".join(f"{rel}:{line}  {action}: {shown}" for rel, line, action, _n, shown in bad))
    stale = [k for k in ALLOW if not any(_allowed(c[0], c[3], c[2]) == k for c in allowed)]
    check(f"every ALLOW entry still names a hand table its gate builds ({len(ALLOW)} entries, "
          f"{len(allowed)} sites)", not stale, stale)

    # The detector itself, on shapes it once missed or misread. T = a kernel table.
    T = "mcp_client_tokens"
    caught = {
        "an f-string name the gate does not fix": 'def f(t):\n    run(f"CREATE TABLE {t} (id int)")\n',
        "an f-string name bound in the file": f't = "{T}"\nrun(f"CREATE TABLE {{t}} (id int)")\n',
        "a %-formatted name": 'def f(t):\n    run("CREATE TABLE %s (id int)" % t)\n',
        "a .format() name": f'run("CREATE TABLE {{}} (id int)".format("{T}"))\n',
        "a +-built name": 'def f(t):\n    run("CREATE TABLE " + t + " (id int)")\n',
        "a statement split across two variables": f'head = "CREATE TABLE {T} ("\nbody = "id int)"\nrun(head + body)\n',
        "a quoted schema and table": f'run(\'CREATE TABLE "public"."{T}" (id int)\')\n',
        "a quoted index name": f'run(\'CREATE INDEX "idx_q" ON {T} (url_handle)\')\n',
        "SELECT ... INTO": f'run("SELECT * INTO {T} FROM other")\n',
        "a view in the table's name": f'run("CREATE VIEW {T} AS SELECT 1")\n',
        "ALTER COLUMN ... DROP NOT NULL": f'run("ALTER TABLE {T} ALTER COLUMN token_hash DROP NOT NULL")\n',
        "DROP CONSTRAINT": f'run("ALTER TABLE ONLY public.{T} DROP CONSTRAINT x")\n',
        "DISABLE TRIGGER": f'run("ALTER TABLE {T} DISABLE TRIGGER ALL")\n',
        "a scratch table renamed into a kernel name": f'run("ALTER TABLE rig_x RENAME TO {T}")\n',
        "a DO block's EXECUTE format()": 'run("DO $$ BEGIN EXECUTE format(\'CREATE TABLE %I (id int)\', \'x\'); END $$")\n',
        "a statement after BEGIN;": f'run("BEGIN; CREATE TABLE IF NOT EXISTS {T} (id int); COMMIT")\n',
        "DROP TABLE": f'run("DROP TABLE IF EXISTS {T}")\n',
        "a loop over kernel names": f'for t in ("{T}",):\n    run(f"DROP TABLE IF EXISTS {{t}}")\n',
        "a trigger on it": f'run("CREATE TRIGGER t BEFORE INSERT ON {T} FOR EACH ROW EXECUTE FUNCTION f()")\n',
    }
    for label, src in caught.items():
        hits = _probe(src, "py", pools)
        check(f"caught: {label}", bool(hits) and all(n == T or n == "…" for _a, n in hits), hits)
    # More shapes it once missed (T, or what each names).
    IX = sorted(indexes)[0]
    caught_more = {
        "DROP SCHEMA public CASCADE": ('run("DROP SCHEMA public CASCADE")\n', "public"),
        "DROP SCHEMA IF EXISTS of the AGE catalog": ('run("DROP SCHEMA IF EXISTS ag_catalog CASCADE")\n',
                                                     "ag_catalog"),
        "DROP OWNED BY the role that owns the kernel": ('run("DROP OWNED BY entity CASCADE")\n', "entity"),
        "DROP OWNED BY CURRENT_USER": ('run("DROP OWNED BY CURRENT_USER")\n', "current_user"),
        "DROP OWNED BY a name built at run time": ('def f(r):\n    run(f"DROP OWNED BY {r}")\n', "…"),
        "TRUNCATE": (f'run("TRUNCATE TABLE {T} CASCADE")\n', T),
        "TRUNCATE ONLY, in a list": (f'run("TRUNCATE ONLY rig_a, {T} RESTART IDENTITY")\n', T),
        "GRANT on it": (f'run("GRANT ALL ON {T} TO PUBLIC")\n', T),
        "REVOKE on it": (f'run("REVOKE SELECT ON TABLE public.{T} FROM host_recall")\n', T),
        "GRANT on every table in the kernel's schema": (
            'run("GRANT SELECT ON ALL TABLES IN SCHEMA public TO gate_role")\n', "public"),
        "ALTER INDEX of a kernel index": (f'run("ALTER INDEX {IX} RENAME TO x")\n', IX),
        "a column type outside the word list": (f'run("CREATE TABLE {T} (kind my_kind NOT NULL, x public.thing)")\n', T),
        "a lone column of an unknown type": (f'run("CREATE TABLE {T} (kind my_kind)")\n', T),
        "psql -c inside a shell string": (f'subprocess.run(f"psql {{dsn}} -c \'DROP TABLE {T}\'", shell=True)\n', T),
        "bash -c 'psql -c ...' in an argv list": (
            f'subprocess.run(["bash", "-c", "psql -X -c \\"TRUNCATE {T}\\""])\n', T),
        "docker exec ... psql -c in a shell string": (
            f'os.system("docker exec c psql -U entity -c \'DROP SCHEMA public CASCADE\'")\n', "public"),
        "a name put in with .replace()": ('def f(t):\n    run("DROP TABLE X".replace("X", t))\n', "…"),
        "a kernel name put in with .replace()": (f'run("DROP TABLE X".replace("X", "{T}"))\n', T),
        "a statement joined from a list": (f'run(" ".join(["DROP TABLE", "{T}"]))\n', T),
        "psycopg sql.Identifier of a run-time name": (
            'def f(cur, t):\n    cur.execute(sql.SQL("DROP TABLE {}").format(sql.Identifier(t)))\n', "…"),
        "psycopg sql.Identifier of a kernel name": (
            f'cur.execute(sql.SQL("TRUNCATE {{}}").format(sql.Identifier("{T}")))\n', T),
    }
    for label, (src, want) in caught_more.items():
        hits = _probe(src, "py", pools)
        check(f"caught: {label}", bool(hits) and all(n == want for _a, n in hits), hits)
    # Which names a finding is checked against once came from the words of
    # its label, so a label that said "schema"
    # or "role" (the ALTER TABLE label carries the statement's next words)
    # checked a table against the schema or role list, and ALTER SCHEMA's
    # upper-case label checked a schema against the tables. And DELETE with no
    # WHERE, DROP DATABASE and DROP EXTENSION were not read.
    M = "entity_memory"
    caught_e35 = {
        "ALTER SCHEMA public RENAME": ('run("ALTER SCHEMA public RENAME TO old_public")\n', "public"),
        "ALTER TABLE … ADD COLUMN role": (f'run("ALTER TABLE {M} ADD COLUMN role text")\n', M),
        "ALTER TABLE … DROP COLUMN schema_version": (f'run("ALTER TABLE {T} DROP COLUMN schema_version")\n', T),
        "ALTER TABLE … OWNER TO a role": (f'run("ALTER TABLE {M} OWNER TO app_role")\n', M),
        "alter table … set schema (lower case)": (f'run("alter table {T} set schema scratch")\n', T),
        "DELETE with no WHERE": (f'run("DELETE FROM {M}")\n', M),
        "DELETE FROM ONLY, schema-qualified, RETURNING": (f'run("DELETE FROM ONLY public.{T} RETURNING id")\n', T),
        "DELETE … USING with no WHERE": (f'run("DELETE FROM {T} t USING rig_x r")\n', T),
        "DELETE whose only WHERE is in a subquery": (
            f'run("DELETE FROM {T} USING (SELECT id FROM rig_x WHERE true) s")\n', T),
        "WITH … DELETE with no WHERE": (f'run("WITH gone AS (SELECT 1) DELETE FROM {T}")\n', T),
        "DELETE in a DO block": (f'run("DO $$ BEGIN DELETE FROM {T}; END $$")\n', T),
        "DELETE of a name built at run time": ('def f(t):\n    run(f"DELETE FROM {t}")\n', "…"),
        "DELETE over a tuple of kernel names": (
            f'NAMES = ("{T}",)\nfor n in NAMES:\n    run(f"DELETE FROM {{n}}")\n', T),
        "a loop variable bound twice stays a hole, never a guess": (
            'NAMES = ("rig_a",)\nfor n in NAMES:\n    pass\nfor n in get_names():\n    run(f"DROP TABLE {n}")\n', "…"),
        "DROP DATABASE of the kernel's database": ('run("DROP DATABASE hostmem")\n', "hostmem"),
        "DROP DATABASE IF EXISTS of the legacy name, FORCE": (
            'run("DROP DATABASE IF EXISTS parallax WITH (FORCE)")\n', "parallax"),
        "DROP DATABASE of a name built at run time": ('def f(d):\n    run(f\'DROP DATABASE "{d}"\')\n', "…"),
        "DROP EXTENSION … CASCADE": ('run("DROP EXTENSION vector CASCADE")\n', "vector"),
        "DROP EXTENSION IF EXISTS … CASCADE": ('run("DROP EXTENSION IF EXISTS age CASCADE")\n', "age"),
        "DROP EXTENSION without CASCADE": ('run("DROP EXTENSION pgcrypto")\n', "pgcrypto"),
    }
    for label, (src, want) in caught_e35.items():
        hits = _probe(src, "py", pools)
        check(f"caught: {label}", bool(hits) and all(n == want for _a, n in hits), hits)
    not_e35 = {
        "DELETE with a WHERE": f'run("DELETE FROM {T} WHERE id = $1")\n',
        "DELETE with WHERE CURRENT OF": f'run("DELETE FROM {T} WHERE CURRENT OF c")\n',
        "DELETE of a scratch table": 'run("DELETE FROM rig_runs")\n',
        "DELETE in prose": f'print("the gate used to DELETE FROM {T} here")\n',
        "DROP DATABASE of the gate's own":
            'def f(n):\n    run(f\'DROP DATABASE IF EXISTS "kernel_{n}" WITH (FORCE)\')\n',
        "DROP EXTENSION the kernel does not install": 'run("DROP EXTENSION IF EXISTS pg_stat_statements")\n',
        "ALTER TABLE of a scratch table that names a role": 'run("ALTER TABLE rig_x OWNER TO entity")\n',
        "DELETE over a tuple of scratch names":
            'NAMES = ("rig_a", "rig_b")\nfor n in NAMES:\n    run(f"DELETE FROM {n}")\n',
    }
    for label, src in not_e35.items():
        hits = _probe(src, "py", pools)
        check(f"not counted: {label}", not hits, hits)
    # A DELETE in a CTE's body was not
    # read; the bound-once rule counted only assignments and loops, so a name
    # also bound as a parameter, by `except … as` or by an import resolved to
    # the loop's tuple; and three mutants survived (a DELETE's depth never
    # going back down, the iterable's bound-once guard dropped, DROP INDEX's
    # name checked against the tables).
    loop ='NAMES = ("rig_a",)\nfor n in NAMES:\n    pass\n'
    caught_6c4 = {
        "a DELETE in a CTE's body": (f'run("WITH d AS (DELETE FROM {T} RETURNING 1) SELECT count(*) FROM d")\n', T),
        "a DELETE in a CTE's body, a later CTE's WHERE is not its own": (
            f'run("WITH d AS (DELETE FROM {T} RETURNING 1), e AS (SELECT 1 WHERE true) SELECT 1")\n', T),
        "a DELETE in a CTE's body, the outer WHERE is not its own": (
            f'run("WITH d AS (DELETE FROM {T} RETURNING id) SELECT * FROM d WHERE id > 0")\n', T),
        "a loop's tuple grown by += stays a hole, never the first tuple": (
            f'NAMES = ("rig_a",)\nNAMES += ("{T}",)\nfor n in NAMES:\n    run(f"DELETE FROM {{n}}")\n', "…"),
        "a loop variable that is also a parameter stays a hole": (
            loop + 'def f(n):\n    run(f"DELETE FROM {n}")\n', "…"),
        "a loop variable that is also bound by `except … as` stays a hole": (
            loop + 'try:\n    pass\nexcept Exception as n:\n    run(f"DELETE FROM {n}")\n', "…"),
        "a loop variable that is also an import stays a hole": (
            loop + 'from names import n\nrun(f"DELETE FROM {n}")\n', "…"),
        "a loop variable that is also a dotted import's head stays a hole": (
            loop + 'import n.tables\nrun(f"DELETE FROM {n}")\n', "…"),
        "a loop variable that is also a lambda's parameter stays a hole": (
            loop + 'f = lambda n: run(f"DELETE FROM {n}")\n', "…"),
        "a loop variable that is also a def stays a hole": (
            loop + 'def n():\n    pass\nrun(f"DELETE FROM {n}")\n', "…"),
        "a loop variable that is also a class stays a hole": (
            loop + 'class n:\n    pass\nrun(f"DELETE FROM {n}")\n', "…"),
        "a loop variable that is also a match capture stays a hole": (
            loop + 'match v:\n    case n:\n        run(f"DELETE FROM {n}")\n', "…"),
        "a loop variable that is also a match star stays a hole": (
            loop + 'match v:\n    case [*n]:\n        run(f"DELETE FROM {n}")\n', "…"),
        "a loop variable that is also a match mapping's rest stays a hole": (
            loop + 'match v:\n    case {**n}:\n        run(f"DELETE FROM {n}")\n', "…"),
        "a loop variable that is also deleted and rebound stays a hole": (
            loop + 'del n\nwith open_names() as n:\n    run(f"DELETE FROM {n}")\n', "…"),
        "DROP INDEX of a kernel index": (f'run("DROP INDEX IF EXISTS {IX}")\n', IX),
    }
    for label, (src, want) in caught_6c4.items():
        hits = _probe(src, "py", pools)
        check(f"caught: {label}", bool(hits) and all(n == want for _a, n in hits), hits)
    not_6c4 = {
        "a DELETE in a CTE's body with its own WHERE":
            f'run("WITH d AS (DELETE FROM {T} WHERE id = 1 RETURNING 1) SELECT * FROM d")\n',
        "DELETE … USING a subquery, then its own WHERE":
            f'run("DELETE FROM {T} USING (SELECT 1 AS x) s WHERE {T}.id = s.x")\n',
        "DROP INDEX of a name that is a kernel TABLE, not a kernel index": f'run("DROP INDEX IF EXISTS {T}")\n',
        "a loop variable bound once over scratch names, alone": loop.replace("pass", 'run(f"DELETE FROM {n}")'),
    }
    check("the DROP INDEX probe's names are what they claim (an index, a table that is no index)",
          IX in indexes and IX not in owned and T in owned and T not in indexes)
    for label, src in not_6c4.items():
        hits = _probe(src, "py", pools)
        check(f"not counted: {label}", not hits, hits)
    check("an ALLOW entry with an action excuses only that action",
          _allowed("scripts/test_contain_finding_sql.py", "nac_finding_contain", _EMPTIES_ALL) is not None
          and _allowed("scripts/test_contain_finding_sql.py", "nac_finding_contain", "defines") is None)
    sh_more = {
        "sh -c with psql inside, in a shell gate": (f"sh -c \"psql -c 'TRUNCATE {T}'\"\n", T),
        "DROP OWNED BY in a heredoc": ("psql <<SQL\nDROP OWNED BY entity;\nSQL\n", "entity"),
    }
    for label, (src, want) in sh_more.items():
        hits = _probe(src, "sh", pools)
        check(f"caught in shell: {label}", any(n == want for _a, n in hits), hits)
    clean = {
        "a needle in `in`": f'assert "CREATE TABLE IF NOT EXISTS {T} (" in sql\n',
        "a needle with a column list, in .count()": f'n = sql.count("CREATE TABLE {T} (id int)")\n',
        "a needle held in a variable": f'NEEDLE = "CREATE TABLE {T} (id int)"\nok = NEEDLE in src\n',
        "a regex pattern": f'rx = re.compile(r"CREATE TABLE {T} \\(id int\\)")\n',
        "prose in a label": 'check("CREATE TABLE network_bastion_heartbeat (the shape a pod carries) is gone", ok)\n',
        "SQL mid-sentence": f'msg = "the gate used to CREATE TABLE {T} (id int) by hand"\n',
        "a runtime scratch name": 'name = f"rig_{n}"\nrun(f"CREATE TABLE {name} (id int)")\n',
        "a scratch table": 'run("CREATE TABLE _scratch_probe (id INT)")\n',
        "ALTER TABLE in prose": f'print("ALTER TABLE {T} is what the gate reads")\n',
        "INSERT ... SELECT": f'run("INSERT INTO {T} (a) SELECT 1")\n',
        "a docstring": f'"""CREATE TABLE {T} (id int)"""\n',
    }
    clean.update({
        "DROP SCHEMA of the gate's own scratch schema": 'run(f\'DROP SCHEMA IF EXISTS "grouptest_{os.getpid()}" CASCADE\')\n',
        "DROP OWNED BY a role the gate made": 'run("DROP OWNED BY gate_probe_role")\n',
        "TRUNCATE a scratch table": 'run("TRUNCATE rig_runs")\n',
        "GRANT on a scratch table": 'run("GRANT SELECT ON rig_runs TO gate_role")\n',
        "GRANT of a role to a role": 'run("GRANT host_recall TO gate_role")\n',
        "GRANT on a function": f'run("GRANT EXECUTE ON FUNCTION {T}_fn() TO gate_role")\n',
        "a CREATE TABLE sentence that runs on": f'msg = "CREATE TABLE {T} (the copy) went away"\n',
        "prose that mentions psql": f'print("never run psql -c \'DROP TABLE {T}\' on a Host")\n',
        "TRUNCATE in prose": f'print("the gate used to TRUNCATE {T} here")\n',
    })
    for label, src in clean.items():
        hits = _probe(src, "py", pools)
        check(f"not counted: {label}", not hits, hits)
    sh_caught = {
        "a heredoc": f"psql <<SQL\nCREATE TABLE {T} (id int);\nSQL\n",
        "psql -c with a quoted schema": f'psql -c "ALTER TABLE \\"public\\".{T} DROP CONSTRAINT x"\n',
        "a $VAR name from the file": f'T={T}\npsql -c "CREATE TABLE $T (id int)"\n',
        "a python heredoc's string": f"python3 - <<'EOF'\ncur.execute(\"CREATE TABLE {T} (id int)\")\nEOF\n",
    }
    for label, src in sh_caught.items():
        hits = _probe(src, "sh", pools)
        check(f"caught in shell: {label}", any(n == T for _a, n in hits), hits)
    sh_clean = {
        "a comment": f"# CREATE TABLE {T} (id int)\n",
        "prose in an echo": f'echo "CREATE TABLE {T} (the copy) is gone"\n',
    }
    for label, src in sh_clean.items():
        hits = _probe(src, "sh", pools)
        check(f"not counted in shell: {label}", not hits, hits)

    print()
    if fails:
        print(f"{fails} FAILED")
        return 1
    print("all gates passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
