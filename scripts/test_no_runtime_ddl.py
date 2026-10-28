#!/usr/bin/env python3
# gate-scope: whole-tree — every .py file in the tree
"""Invariant: the Host kernel does not create or alter its own schema at RUNTIME.

THE RULE

    SCHEMA IS DECLARED IN migrations/*.sql AND NOWHERE ELSE.

Runtime DDL — a `CREATE TABLE IF NOT EXISTS` inside a feature module, an
`ensure_tables()` helper called on the first request — means the kernel's
database role must hold DDL rights forever, on every host, for every request.
That is what blocks the DB role split: `host_recall` cannot be the runtime role
while the runtime still needs to CREATE. It also means the schema a host ends up
with depends on which code paths happened to run there, which is how eight hosts
came to disagree about `nac_agent_activity` and `network_bastion_heartbeat`.

WHY THE GATE COMES FIRST, BEFORE THE MIGRATIONS. A survey found 39 CREATE
TABLE sites in server/ where a count the day before found 38 —
`network_machine_offers` (server/network/machine_offers.py) was added in
between. The set GROWS while the migration work waits. From here on, a new
runtime CREATE cannot be added without deliberately editing
scripts/runtime-ddl-allowlist.txt, which is a line a reviewer sees.

WHAT IS CHECKED, AND WHERE. Every string literal under BOTH roots below, found
with the AST, matched against CREATE TABLE / CREATE [UNIQUE] INDEX / ALTER TABLE
/ CREATE TRIGGER / FUNCTION / VIEW / SEQUENCE / TYPE / DROP / TRUNCATE.

    server/   the HTTP surface
    brain/    the engine behind it

brain/ is here because the DDL there is on a request path, not in a batch job:
brain/mastercode/context_backends.py and brain/mastercode/dcn.py are both
reached from server/routes/context_manager.py, so a Context Manager request
issues CREATE TABLE as the `entity` role exactly the way a server/ module does.
Scoping this gate to server/ would let the allowlist reach empty and report
the migration finished while the role still needs DDL rights — a gate that can report
done while the thing it guards is still broken is worse than no gate.

A file named in scripts/runtime-ddl-allowlist.txt is allowed to still have DDL —
that list is the not-yet-migrated backlog and it ONLY EVER SHRINKS.

TWO EXCLUSIONS, BOTH LOAD-BEARING. The statement count on this ticket was wrong
three separate times, each time because prose ABOUT SQL was counted as SQL:

  1. DOCSTRINGS are excluded. A module/class/function's leading string Expr is
     documentation; a docstring that explains a CREATE is not a CREATE.
  2. SQL COMMENTS are stripped OUT OF THE LITERAL before matching — `--` to end
     of line and `/* ... */`. server/applications/registry.py:70 is a `--`
     comment INSIDE a real SQL string reading "the CREATE TABLE above is
     IF NOT EXISTS". It is not a docstring, so exclusion (1) does not catch it,
     and counting it inflates the ticket by a statement that does not exist.

INTERPOLATED IDENTIFIERS ARE MATCHED. The object name in a DDL statement is
routinely an f-string placeholder — `f"ALTER TABLE {t} ADD COLUMN ..."` is the
single most natural dynamic shape, and ALTERs are the largest share of this
ticket. An f-string is an ast.JoinedStr, so its literal halves are flattened and
each `{...}` becomes an identifier-shaped placeholder before matching. The live
instance this was measured against is brain/mastercode/memory_engine.py:137-141,
which builds `CREATE INDEX IF NOT EXISTS {index_name} ON entity_memory USING
hnsw (...)` and executes it lazily the first time a dim column is used — a
per-host index divergence created at request time, which is the same defect the
survey found on `user_context`.

THE ALLOWLIST CANNOT ROT OPEN. This also fails when the allowlist names a file
that no longer contains DDL, or a file that no longer exists. Without that, a
migrated band leaves its entry behind and the next runtime CREATE in that file
lands silently under a stale exemption. The end state is an EMPTY allowlist.

TWO SKIPS, NEITHER OF THEM A STANDING EXEMPTION. Both are NAMED lists of
specific files, and both are RE-VALIDATED on every run against the property that
earned the skip. If a listed file loses that property the gate FAILS, because a
list that keeps exempting a file after its reason expired is the blanket
exemption this gate exists to avoid.

  SQLITE_MODULES — SQLite DDL is out of scope. This ticket is about the
  PostgreSQL `entity` role; a pod-local SQLite file has no database role to
  downgrade and no second host to disagree with it, so there is nothing for a
  migration to fix. Validated per run: the module imports sqlite3, opens its own
  connection (sqlite3.connect, or a factory annotated -> sqlite3.Connection),
  does not use the Postgres lane, and — the load-bearing part — EVERY ONE of its
  DDL literals both

      reaches an execute call on a receiver known to be a sqlite connection, AND
      reaches NOTHING ELSE that is execute- or fetch-shaped.

  The second half makes the first a STRONGER NECESSARY condition — it is not a
  sufficient one, and the limits are in the honest admission below. What it buys
  is the ordinary migration: "every literal runs through sqlite" is satisfied
  forever by a module that KEEPS its SQLite path and ADDS a Postgres one, which
  is how this would actually be staged — dual-write first, cut over later. Under
  a necessary-only check that module prints "out of scope" while a request
  issues CREATE TABLE as the entity role. Receivers are resolved as names AND
  attributes (`self.db`, `self.pool`), because an attribute-held pool was
  invisible to a Name-only check, and a receiver that cannot be resolved at all
  counts as not-sqlite.

  NON_EXECUTED_DDL_MODULES — DDL that is test DATA, never executed.
  brain/mastercode/sandbox/test_sql_proxy.py's CASES table holds six DDL
  literals (:39, :40, :41, :43, :46 and — in the stacked-statement guard block —
  :90), each a (sql, bits, expected, description) tuple fed to `authorize_sql`
  at its :143, asserting that DDL is DENIED. They are inputs to a pure-function
  test of the SQL authorizer, not statements anything runs, so allowlisting them
  would be a lie about the backlog: there is nothing to migrate. Validated per
  run: the file is a test module, and NONE of its DDL literals reaches any
  execute/fetch-shaped call on any receiver at all. The moment one does, the
  skip dies.

HONEST ADMISSION — WHAT THIS CANNOT SEE. It reads string literals with the AST,
so:

  - SQL built by concatenation, .format() or %, or assembled from fragments held
    in a list, is invisible. f-strings ARE covered (above); the other builders
    are not.
  - FIXED, and recorded here because this bullet used to say the
    opposite was fine. `--` inside a SQL string literal WAS treated as a comment
    wherever it appeared, on the reasoning that the failure direction is only a
    miss. It is a miss that hides runtime DDL from the gate — the whole point of
    the gate — in one line: `SELECT '--' AS d; CREATE TABLE x (...)` scanned as
    `SELECT '`. The stripper is quote-aware now (_mask_sql). "Only a miss" is
    not a reason to leave a hole in a ratchet.
  - What the masker still cannot resolve, named rather than implied: a
    dollar-quoted body is treated as CODE, because in migrations it is (plpgsql);
    a dollar-quoted string used as DATA therefore has its contents scanned. And
    an f-string whose QUOTE character is itself interpolated — `f"SELECT {q}--{q}
    ; CREATE TABLE ..."` — cannot be resolved by any static reader.
  - It will FLAG PROSE that reads like DDL. Flattening f-strings means an error
    message — `f"could not create table {name}"`, `f"failed to alter table {t}:
    {e}"` — now matches, and this codebase is full of those. There are none in
    a scanned file today, and the failure is loud rather than silent, so it is
    the cost of catching `ALTER TABLE {t}` rather than a defect. If this gate
    fires on a file you know has no SQL in it, THAT is the likely cause: check
    whether the flagged line is a message rather than a statement. The fix is
    to reword the message (or, if it truly is DDL, migrate it) — never to
    loosen the matcher, which would put `ALTER TABLE {t}` back in the blind
    spot it was once in.
  - DDL outside the two roots, in a directory listed in NON_RUNTIME_DIRS. The
    roots list itself is checked (any OTHER directory holding DDL fails the
    gate), so this is now a named-and-reasoned exemption rather than a promise.
    The previous version of this docstring asserted the outside was empty
    because "migrations/ is where it is supposed to be"; brain/ held 20
    statements.
  - THE SKIP'S REACHABILITY IS DIRECT-CALL ONLY, in both directions. It asks
    whether a DDL literal — or the name it is assigned to — appears AS AN
    ARGUMENT at an execute-shaped call site. One hop of indirection defeats
    that, and five shapes were measured against the real
    server/integrations/core/storage.py (SQLite until v201):

        sql = SCHEMA; await pool.execute(sql)      (re-aliased through a local)
        await _pg(SCHEMA)                          (passed to a helper)
        await pool.execute(SCHEMA.strip())         (a method call on the name)
        await pool.execute(f"{SCHEMA}")            (re-wrapped in an f-string)
        await pool.execute(query=SCHEMA)           (keyword — NOW HANDLED)

    Only the last is fixed, because it was two lines. The other four need
    interprocedural dataflow, which an AST gate does not have. So state the bar
    plainly: keeping the exemption while moving to Postgres now requires
    retaining the sqlite execution AND adding a Postgres path AND routing the
    literal through an alias or a helper on the way. That is three deliberate
    steps, not an accident, which is the most this check can be.

    DO NOT "fix" this by requiring that a SQLite module contain NO execute or
    fetch call on a non-sqlite receiver anywhere. That was tried and rejected:
    it false-fails BOTH real modules, which hold 6 and 24 such call sites.
    The two files fail it for different reasons:

        storage.py           6 sites, all 6 chained cursor calls
                             (`_connect().execute(...).fetchone()`), where the
                             receiver is an expression no resolver can name.
        codesearch/store.py  24 sites — 7 of that same chained shape, and 17
                             plain `db.execute(...)` where `db` is an
                             UNANNOTATED parameter that the scope rules
                             deliberately refuse to treat as sqlite.

    So the rule would fire on ordinary correct code in both, by two separate
    routes, and a gate that fails on a correct module is how people learn to
    bypass it. The per-literal check does not have this problem because it only
    looks at call sites the DDL itself reaches.

It is a ratchet against the plausible addition, not a proof of absence.

Pure: reads files, runs nothing. Container-free; runs in pre-merge and smoke.
Run from the repo root:  APP_ROOT=<repo> python3 scripts/test_no_runtime_ddl.py

Exit codes:  0 clean · 1 violated, or a skip that no longer holds · 2 the gate
could not check what it claims to check.
"""
from __future__ import annotations

import ast
import os
import re
import sys
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
_ALLOWLIST = _ROOT / "scripts" / "runtime-ddl-allowlist.txt"

# Both roots hold modules on the request path. See "WHAT IS CHECKED, AND WHERE".
ROOTS: tuple[str, ...] = ("server", "brain")

# ROOTS DEFENDS ITSELF. A hand-maintained roots list with nothing checking its
# completeness is how brain/ once went unnoticed — the allowlist could
# not rot open because the gate checks it, and ROOTS had no equivalent. So every
# .py in the repo outside ROOTS is scanned too, and DDL found there fails the
# gate unless its top-level directory is named below WITH a reason.
#
# Why there is no third runtime root today, read off Dockerfile.linux's COPY set
# (brain, cli, dashboard, default-skills, docs, etc, factory-bundles, migrations,
# native-bundles, native-sdk, provisioning, scripts, server, system-apps) and
# measured:
#   - Of those, only cli/ (2 .py), system-apps/ (13) and scripts/ (324) carry
#     Python outside the roots at all. The first two hold no DDL.
#   - dashboard/, default-skills/, docs/, etc/, factory-bundles/, migrations/,
#     native-bundles/, native-sdk/ and provisioning/ contain ZERO .py files, so
#     they are clear because there is no Python in them, not because their
#     Python was checked.
#   - The only DDL anywhere outside the roots is in scripts/ and tests/, below.
# Nothing here is load-bearing for the check itself: _unclaimed_roots() walks
# every directory regardless of what this comment lists.
NON_RUNTIME_DIRS: dict[str, str] = {
    # 92 scripts/ files ARE COPY'd into the image, so "not in the image" is not
    # the reason and would be false. The reason is that nothing on a request
    # path imports them — which _exempt_dirs_unimported() re-derives every run
    # rather than trusting this sentence.
    "scripts": "gates and one-off tools. Present in the image, but nothing under "
               "ROOTS imports them, so no request path reaches their DDL. This "
               "gate is itself here.",
    "tests": "test code, not COPY'd into the image at all; its DDL builds "
             "fixtures and no request path reaches it.",
}

# Never walked: not source, or not ours.
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache",
              ".pytest_cache", ".ruff_cache", "site-packages", ".worktrees"}

# ── the refused surface ───────────────────────────────────────────────────
# An object name may be a bare identifier, a "quoted identifier", or — once
# f-strings are flattened — the placeholder standing in for {...}.
_PLACEHOLDER = "x_privatae_interpolated_x"
_NAME = r'(?:"[^"\n]+"|[A-Za-z_][\w.]*)'

DDL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("CREATE TABLE", re.compile(
        r"CREATE\s+(?:(?:GLOBAL|LOCAL)\s+)?"
        r"(?:(?:TEMP|TEMPORARY|UNLOGGED|VIRTUAL)\s+)*"
        rf"TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?({_NAME})", re.I)),
    ("CREATE INDEX", re.compile(
        r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?"
        rf"(?:IF\s+NOT\s+EXISTS\s+)?({_NAME})\s+ON\s+{_NAME}", re.I)),
    ("ALTER TABLE", re.compile(
        rf"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?({_NAME})", re.I)),
    ("CREATE TRIGGER", re.compile(
        rf"CREATE\s+(?:OR\s+REPLACE\s+)?(?:CONSTRAINT\s+)?TRIGGER\s+({_NAME})", re.I)),
    ("CREATE FUNCTION", re.compile(
        rf"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+({_NAME})", re.I)),
    ("CREATE VIEW", re.compile(
        r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:(?:TEMP|TEMPORARY|MATERIALIZED)\s+)*"
        rf"VIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?({_NAME})", re.I)),
    ("CREATE SEQUENCE", re.compile(
        r"CREATE\s+(?:(?:TEMP|TEMPORARY)\s+)?SEQUENCE\s+"
        rf"(?:IF\s+NOT\s+EXISTS\s+)?({_NAME})", re.I)),
    ("CREATE TYPE", re.compile(rf"CREATE\s+TYPE\s+({_NAME})", re.I)),
    ("DROP", re.compile(
        r"DROP\s+(?:TABLE|INDEX|MATERIALIZED\s+VIEW|VIEW|TYPE|TRIGGER|FUNCTION|SEQUENCE)"
        rf"\s+(?:CONCURRENTLY\s+)?(?:IF\s+EXISTS\s+)?({_NAME})", re.I)),
    ("TRUNCATE", re.compile(
        rf"TRUNCATE\s+(?:TABLE\s+)?(?:ONLY\s+)?({_NAME})", re.I)),
    # Each of these needs privilege the role split is meant to take
    # away, and none of them appeared in server/ or brain/ when they were added
    # — so they are a pure ratchet, closing the spelling before someone reaches
    # for it rather than after.
    ("CREATE SCHEMA", re.compile(
        rf"CREATE\s+SCHEMA\s+(?:IF\s+NOT\s+EXISTS\s+)?({_NAME})", re.I)),
    ("CREATE EXTENSION", re.compile(
        rf"CREATE\s+EXTENSION\s+(?:IF\s+NOT\s+EXISTS\s+)?({_NAME})", re.I)),
    # ROLE and USER, but NOT GROUP. All three are the same PostgreSQL statement.
    # GROUP is out on measured evidence, not caution: "list/create group circles,
    # and post to a circle" — public_tool_registry.py's circles_manage
    # description — matched it and failed the gate on a sentence of English, and
    # a matcher that fires on prose teaches people to route around the gate.
    # USER has no such collision TODAY — zero matches of `CREATE USER <name>`
    # under server/ or brain/ — and leaving it out would close CREATE ROLE app_ro
    # while leaving CREATE USER app_ro open, a ratchet with a hole in it. That is
    # a fact about this corpus, not about the word: "let the host create user
    # accounts" in some future tool description would match it the way the
    # circles sentence matched GROUP. If that happens, the answer is the same as
    # it was for GROUP — drop the alias, and say here what collided.
    ("CREATE ROLE", re.compile(
        rf"CREATE\s+(?:ROLE|USER)\s+(?:IF\s+NOT\s+EXISTS\s+)?({_NAME})", re.I)),
    ("ALTER INDEX", re.compile(
        rf"ALTER\s+INDEX\s+(?:IF\s+EXISTS\s+)?({_NAME})", re.I)),
)

# DELIBERATELY NOT MATCHED, so the omissions are a decision rather than a gap:
#
#   GRANT / REVOKE — two of these are load-bearing in Python ON PURPOSE and are
#     written down in the allowlist header: nac_agents.py's REVOKE of
#     host_recall on nac_agent_delegations, and tokens.py's REVOKE half of
#     _SECRET_SCHEMA. Both exist because a table created inside the migration
#     loop is created AFTER v139 arms ALTER DEFAULT PRIVILEGES on that same
#     boot, so the runtime half is the second lock, not a leftover. Matching
#     GRANT/REVOKE would fail the gate on both and the only way back would be an
#     allowlist entry — which is exactly the standing exemption this file exists
#     to prevent. They are also not schema: they change no shape.
#
#   SELECT ... INTO — `SELECT col INTO var` is plpgsql variable assignment, and
#     a DO block embedded in a Python string is legitimate. The table-creating
#     form (`SELECT ... INTO newtab FROM ...`) is indistinguishable from it
#     without parsing the surrounding statement, and a matcher that guesses is
#     how a gate gets trained around. If runtime DDL ever arrives by this route,
#     parse it properly rather than widening this regex.

# ── the two skips ─────────────────────────────────────────────────────────
# Named, not pattern-matched: narrow and loud beats clever and broad. Every
# entry is re-validated on each run — see sqlite_faults() / fixture_faults().
SQLITE_MODULES: tuple[str, ...] = (
    # Empty: both SQLite stores moved into the Host's Postgres —
    # brain/codesearch/store.py (migrations/v200) and
    # server/integrations/core/storage.py (migrations/v201).
)

NON_EXECUTED_DDL_MODULES: tuple[str, ...] = (
    "brain/mastercode/sandbox/test_sql_proxy.py",
)

# ── one named exception: a restore's load, in the restore's own database ──
# A restore (server/host_restore.py) builds a SECOND database, hostmem_restore,
# from this tree's own schema, loads a backup's rows into it, and swaps it in at
# the next boot. Loading an older backup changes that database's shape for the
# load and back: the columns the backup's COPY names that this version dropped
# are added (_add_load_columns), the CHECKs are set aside (_set_checks_aside),
# the rows the schema step put in are emptied (_load_rows), and at the boot,
# once the migrations have run over the restored rows, what is left of the
# load's own columns is dropped (settle). None of it touches the database the
# Host is serving from, and none of it is schema a migration could declare: it
# is the shape of one backup, for one load. So these four functions, and only
# the DDL kinds named for each, are exempt, and only while every statement they
# send goes through host_restore._psql to the named database:
#   RESTORE_DB  the restore's own database, by its constant;
#   db          settle's parameter, which is the restored database: settle is
#               called only from carry() (checked below), which entrypoint.sh
#               runs at boot after the swap and before the server starts, with
#               the columns the restore's own plan says it added.
# Re-validated every run (restore_load_faults): a function that holds other
# DDL, sends a statement anywhere but _psql to that database, runs an
# execute- or fetch-shaped call, holds no DDL any more, or (settle) is called
# from anywhere but carry() fails the gate, and DDL anywhere else in the file
# is counted like any other.
RESTORE_LOAD_DDL: dict[tuple[str, str], tuple[frozenset[str], str, frozenset[str]]] = {
    # (file, function): (DDL kinds, the db= its _psql calls must name,
    #                    the functions of the file that may call it; empty = any)
    ("server/host_restore.py", "_add_load_columns"): (frozenset({"ALTER TABLE"}), "RESTORE_DB", frozenset()),
    ("server/host_restore.py", "_set_checks_aside"): (frozenset({"ALTER TABLE"}), "RESTORE_DB", frozenset()),
    ("server/host_restore.py", "_load_rows"): (frozenset({"TRUNCATE"}), "RESTORE_DB", frozenset()),
    ("server/host_restore.py", "settle"): (frozenset({"ALTER TABLE"}), "db", frozenset({"carry"})),
}


def _hits_in(tree: ast.AST) -> list[tuple[ast.AST, int, str, str]]:
    """(literal node, statement line, kind, object) for every DDL statement,
    as scan_source counts them."""
    out = []
    for node, raw in literal_texts(tree):
        text = strip_sql_comments(raw)
        for kind, rx in DDL_PATTERNS:
            for m in rx.finditer(text):
                out.append((node, _statement_line(node, text, m.start()), kind,
                            _object_name(m.group(1))))
    return out


def _enclosing_function(tree: ast.Module, node: ast.AST) -> str | None:
    """The module-level function a node sits in, by its lines."""
    line = getattr(node, "lineno", 0)
    for f in tree.body:
        if (isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
                and f.lineno <= line <= (f.end_lineno or f.lineno)):
            return f.name
    return None


def restore_load_faults(src: str, rel: str, filename: str = "<probe>",
                        table: dict | None = None
                        ) -> tuple[list[tuple[int, str, str]], list[tuple[int, str, str]], list[str]]:
    """(exempt hits, counted hits, faults) of one file under RESTORE_LOAD_DDL.
    A fault means an entry no longer holds what earned it: the gate fails."""
    table = RESTORE_LOAD_DDL if table is None else table
    tree = ast.parse(src, filename=filename)
    entries = {fn: v for (path, fn), v in table.items() if path == rel}
    funcs = {f.name: f for f in tree.body if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))}
    faults: list[str] = []
    psql = funcs.get("_psql")
    if entries and (psql is None or "db" not in {a.arg for a in psql.args.args + psql.args.kwonlyargs}):
        faults.append("it has no module-level _psql(..., db=...) for the exempt statements to go through")
    for fn, (kinds, db_name, callers) in entries.items():
        f = funcs.get(fn)
        if f is None:
            faults.append(f"{fn}() is gone: remove it from RESTORE_LOAD_DDL")
            continue
        for call in ast.walk(f):
            if not isinstance(call, ast.Call):
                continue
            name = (call.func.id if isinstance(call.func, ast.Name)
                    else call.func.attr if isinstance(call.func, ast.Attribute) else None)
            if name == "_psql":
                db = next((k.value for k in call.keywords if k.arg == "db"), None)
                if not (isinstance(db, ast.Name) and db.id == db_name):
                    faults.append(f"{fn}() line {call.lineno} sends a statement to another database "
                                  f"than {db_name}")
            elif isinstance(call.func, ast.Attribute) and name in _ANY_EXEC_METHODS:
                faults.append(f"{fn}() line {call.lineno} runs .{name}(): its statements must all go "
                              f"through _psql to {db_name}")
        if callers:
            for call in ast.walk(tree):
                if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == fn
                        and _enclosing_function(tree, call) not in callers):
                    faults.append(f"{fn}() is called at line {call.lineno}, outside "
                                  f"{', '.join(sorted(callers))}()")
    exempt, counted = [], []
    held: dict[str, int] = {}
    for node, line, kind, obj in _hits_in(tree):
        fn = _enclosing_function(tree, node)
        entry = entries.get(fn) if fn else None
        if entry and kind in entry[0]:
            exempt.append((line, kind, obj))
            held[fn] = held.get(fn, 0) + 1
        else:
            counted.append((line, kind, obj))
    for fn in entries:
        if fn in funcs and not held.get(fn):
            faults.append(f"{fn}() holds no exempt DDL any more: remove it from RESTORE_LOAD_DDL")
    return exempt, counted, faults


def restore_load_callers_elsewhere(files: list[Path]) -> list[str]:
    """Files under ROOTS, other than the exempt functions' own, that call a
    caller-restricted exempt function as an attribute (host_restore.settle)."""
    restricted = {(path, fn) for (path, fn), (_, _, callers) in RESTORE_LOAD_DDL.items() if callers}
    out = []
    for p in files:
        rel = p.relative_to(_ROOT).as_posix()
        try:
            src = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for path, fn in restricted:
            if rel == path or Path(path).stem not in src:
                continue
            try:
                tree = ast.parse(src, filename=str(p))
            except SyntaxError:
                continue
            module = path[:-3].replace("/", ".")
            stem = Path(path).stem
            bound: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    for a in node.names:
                        if node.module == module and a.name == fn:
                            bound.add("")       # the function itself, imported
                        elif a.name == stem and (node.module or "") == module.rsplit(".", 1)[0]:
                            bound.add(a.asname or a.name)
                elif isinstance(node, ast.Import):
                    for a in node.names:
                        if a.name == module:
                            bound.add(a.asname or a.name)
            for call in ast.walk(tree):
                if not isinstance(call, ast.Call):
                    continue
                if "" in bound and isinstance(call.func, ast.Name) and call.func.id == fn:
                    out.append(f"{rel}:{call.lineno} calls {fn}() ({path} allows only "
                               f"{', '.join(sorted(RESTORE_LOAD_DDL[(path, fn)][2]))}())")
                elif (isinstance(call.func, ast.Attribute) and call.func.attr == fn
                        and (_dotted(call.func.value) or "") in bound):
                    out.append(f"{rel}:{call.lineno} calls .{fn}() ({path} allows only "
                               f"{', '.join(sorted(RESTORE_LOAD_DDL[(path, fn)][2]))}())")
    return out

# What "the Postgres lane" looks like in this repo: 20 modules under server/
# import asyncpg (89 repo-wide), and get_pool appears 397 times under server/.
_PG_IMPORTS = {"asyncpg", "psycopg", "psycopg2"}
_PG_CALLS = {"get_pool"}
_SQLITE_EXEC_METHODS = {"execute", "executescript", "executemany"}
# Receiver-agnostic, and only for the "executed by nothing at all" check.
_ANY_EXEC_METHODS = _SQLITE_EXEC_METHODS | {
    "fetch", "fetchrow", "fetchval", "fetchall", "fetchone",
}

def _mask_sql(text: str, comments: bool, literals: bool) -> str:
    """One quote-aware pass. Masked spans become spaces; newlines always survive.

    QUOTE-AWARENESS IS NOT COSMETIC HERE, and this is why it was added.
    The matcher that stood here was `--[^\\n]*`, which fires
    anywhere — including inside a SQL string literal, where `--` is data. So:

        SELECT '--' AS dash; CREATE TABLE evil (id int);

    stripped to `SELECT '` and the CREATE TABLE was gone before any pattern
    ran. That is a way PAST this gate, on one line, and the gate is the only
    thing keeping the 38 retired statements from coming back.

    Handles the three things that can contain a `--` without starting a comment:
    single-quoted literals (with the '' escape), double-quoted identifiers, and
    block comments, which PostgreSQL allows to NEST — a depth counter, not a
    lazy `.*?`.

    A DOLLAR-QUOTED BODY IS DELIBERATELY NOT ONE OF THEM, and that is measured,
    not stylistic. `$$ ... $$` in these migrations wraps plpgsql, which is CODE:
    v147's five ALTER TABLEs and its backfill UPDATE all sit inside a
    `DO $$ ... END $$` guard. Masking the body as a literal deleted them — the
    first version of this function did exactly that and the caller asserting on
    those ALTERs went red. So the scan walks INTO a dollar body, which is also
    what makes its `--` comments and its RAISE '...' literals get handled. The
    trade is a dollar-quoted string used as DATA rather than code: its contents
    are scanned. No migration does that, and inside the scanned roots the two
    that do — Apache AGE's `cypher('...', $$ ... $$)` at
    brain/mastercode/memory_consolidation.py:236 and memory_graph.py:107 — mask
    to themselves unchanged, so nothing is lost on them today.

    AN UNTERMINATED `/*` RESTORES RATHER THAN SWALLOWS, and that is a bug fix,
    not a nicety. A depth counter with no closing `*/` blanks everything to the
    end of the literal, where the `/\\*.*?\\*/` regex it replaced required the
    close and blanked NOTHING. That turned an ordinary shell glob into a hole:

        COPY staging FROM PROGRAM $$ cat /var/backups/nightly/* $$;
        CREATE TABLE IF NOT EXISTS network_bastion_heartbeat (...);

    PostgreSQL runs both statements; the `/*` is the tail of a path. With the
    plain counter the CREATE was blanked and the gate printed PASSED over real
    runtime DDL. An unterminated `/*` is a syntax error to PostgreSQL, so
    masking it can only ever HIDE and never help — hence: remember where the
    outermost `/*` opened, and if the scan ends still inside it, hand back the
    `/*` as ordinary text and re-mask the rest (see the tail of this function
    for why re-masking rather than verbatim). Measured over all 75,797 literals under server/ + brain/: 20
    reach the restore path, 17 of them with text after the `/*` (HTTP Accept
    headers, path globs like `/var/lib/cseries/projects/**`, a refspec), and 0
    where restoring rather than swallowing changes the DDL verdict today. The
    hole was latent, which is the only kind this gate can still have.
    """
    out, i, n = [], 0, len(text)
    depth = 0                      # block-comment nesting depth
    open_at = -1                   # index of the OUTERMOST unclosed `/*`
    out_at = 0                     # len of `out` when that `/*` opened
    def blank(s: str) -> str:
        return "".join(c if c == "\n" else " " for c in s)
    while i < n:
        ch = text[i]
        if depth:                                       # inside /* ... */
            if text.startswith("/*", i):
                depth += 1; out.append("  " if comments else "/*"); i += 2; continue
            if text.startswith("*/", i):
                depth -= 1; out.append("  " if comments else "*/"); i += 2; continue
            out.append(blank(ch) if comments else ch); i += 1; continue
        if text.startswith("/*", i):
            depth = 1; open_at = i; out_at = len(out)
            out.append("  " if comments else "/*"); i += 2; continue
        if text.startswith("--", i):
            # PostgreSQL ends a `--` comment at a bare CR as well as a newline.
            # NOT for the reason first written here: a CR in the SOURCE never
            # reaches this function, because ast.parse normalises CR and CRLF to
            # LF (in a triple-quoted literal) or rejects them outright (in a
            # single-quoted one, where a raw CR is an unterminated string). The
            # reachable shape is an ESCAPED one — "-- note\\rCREATE TABLE x" —
            # and there this closes a real miss.
            j = min([x for x in (text.find("\n", i), text.find("\r", i)) if x >= 0] or [n])
            out.append(blank(text[i:j]) if comments else text[i:j]); i = j; continue
        if ch == "'":
            j = i + 1
            while j < n:
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        j += 2; continue
                    j += 1; break
                j += 1
            span = text[i:j]
            out.append(blank(span) if literals else span); i = j; continue
        if ch == '"':
            j = i + 1
            while j < n:
                if text[j] == '"':
                    if j + 1 < n and text[j + 1] == '"':
                        j += 2; continue
                    j += 1; break
                j += 1
            out.append(text[i:j]); i = j; continue
        out.append(ch); i += 1
    if depth:
        # NEVER TERMINATED. Hand back the two `/*` characters as ordinary text
        # and re-mask everything after them, rather than returning the tail
        # verbatim.
        #
        # Verbatim was the first attempt and it was WRONG in the silent
        # direction, which I had argued it could not be: with masking off for
        # the rest of the literal, a comment INSIDE a later statement survives,
        # and the DDL patterns need contiguous whitespace between keywords. So
        #     CREATE /* new in v151 */ TABLE IF NOT EXISTS x (...)
        # after an unterminated `/*` stopped matching — a MISS, not a loud
        # false positive. Measured at 72.8% of DDL statements carrying an
        # interior comment. Re-masking keeps every later comment and literal
        # handled while the unterminated `/*` itself stays inert.
        #
        # The recursion terminates: each level consumes at least the two `/*`
        # characters before recursing on a strictly shorter string.
        del out[out_at:]
        out.append(text[open_at:open_at + 2])
        out.append(_mask_sql(text[open_at + 2:], comments, literals))
    return "".join(out)


def strip_sql_comments(text: str) -> str:
    """Blank out SQL comments, preserving newlines so line offsets stay put.

    Exclusion (2) in this file's header. A `--` comment inside a real SQL
    literal is prose, not a statement. Newlines survive deliberately: the
    statement line each hit is reported at is derived by counting them.
    """
    return _mask_sql(text, comments=True, literals=False)


def blank_sql_literals(text: str) -> str:
    """Comments AND the CONTENTS of string literals blanked, offsets preserved.

    For callers asserting something about a migration's STATEMENTS, where text
    inside a literal must not be able to impersonate one. v147's house style
    puts long English in RAISE WARNING/NOTICE literals, so a sentence quoting
    `ALTER TABLE installed_applications ...` is an ordinary thing to write and
    would otherwise satisfy a matcher looking for that statement.
    """
    return _mask_sql(text, comments=True, literals=True)


def docstring_node_ids(tree: ast.AST) -> set[int]:
    """id() of every Constant node that is a docstring — exclusion (1)."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            out.add(id(first.value))
    return out


def _flatten_joined(node: ast.JoinedStr) -> str:
    """An f-string as text, each {...} replaced by an identifier-shaped
    placeholder so `ALTER TABLE {t}` matches the way `ALTER TABLE t` does."""
    parts: list[str] = []
    for piece in node.values:
        if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
            parts.append(piece.value)
        else:
            parts.append(_PLACEHOLDER)
    return "".join(parts)


def literal_texts(tree: ast.AST) -> list[tuple[ast.AST, str]]:
    """(node, text) for every string-bearing literal worth scanning.

    f-strings are flattened and their inner Constants suppressed, so a JoinedStr
    is counted exactly once. Byte literals are decoded: b"CREATE TABLE ..." is
    still DDL.
    """
    inner: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for child in ast.walk(node):
                if child is not node:
                    inner.add(id(child))
    skip = docstring_node_ids(tree)

    out: list[tuple[ast.AST, str]] = []
    for node in ast.walk(tree):
        if id(node) in inner or id(node) in skip:
            continue
        if isinstance(node, ast.JoinedStr):
            out.append((node, _flatten_joined(node)))
        elif isinstance(node, ast.Constant):
            if isinstance(node.value, str):
                out.append((node, node.value))
            elif isinstance(node.value, bytes):
                out.append((node, node.value.decode("latin-1", "replace")))
    return out


def _statement_line(node: ast.AST, text: str, offset: int) -> int:
    """The line the STATEMENT is on, not the line the literal opens on.

    This is the output a developer acts on: the six statements in
    server/vault/store.py run from :24 to :46, and reporting all six at the
    literal's :23 is six wrong line numbers in a file that trades on
    registry.py:70-grade precision. Derived by counting the newlines ahead of
    the match — exact for triple-quoted SQL, which is every real case here — and
    clamped to the literal's own end so an escape-heavy literal cannot point
    past it.
    """
    line = getattr(node, "lineno", 0) + text[:offset].count("\n")
    end = getattr(node, "end_lineno", None)
    if end:
        line = min(line, end)
    return line


def _object_name(raw: str) -> str:
    return "{interpolated}" if _PLACEHOLDER in raw.lower() else raw.lower()


def scan_source(src: str, filename: str = "<probe>") -> list[tuple[int, str, str]]:
    """Return (statement_line, kind, object_named) for every DDL statement.

    Raises SyntaxError / UnicodeDecodeError to the caller — a file under a
    scanned root that will not parse is a failure of this gate, never a skip.
    """
    tree = ast.parse(src, filename=filename)
    hits: list[tuple[int, str, str]] = []
    for node, raw in literal_texts(tree):
        text = strip_sql_comments(raw)
        for kind, rx in DDL_PATTERNS:
            for m in rx.finditer(text):
                hits.append((_statement_line(node, text, m.start()),
                             kind, _object_name(m.group(1))))
    return hits


def _ddl_nodes(tree: ast.AST) -> list[ast.AST]:
    return [n for n, raw in literal_texts(tree)
            if any(rx.search(strip_sql_comments(raw)) for _, rx in DDL_PATTERNS)]


def _names_bound_to(tree: ast.AST, node_ids: set[int]) -> dict[int, set[str]]:
    """node id -> the names it is assigned to.

    Assign AND AnnAssign: a module writing `SCHEMA: str = "CREATE TABLE ..."` is
    as ordinary as `SCHEMA = ...`, and missing the annotated form would fail a
    genuine module over its type hint and send the reader off to fix a
    non-problem.
    """
    out: dict[int, set[str]] = {}
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if value is None or id(value) not in node_ids:
            continue
        for t in targets:
            if isinstance(t, ast.Name):
                out.setdefault(id(value), set()).add(t.id)
    return out


def _is_sqlite_conn_annotation(ann: ast.expr | None) -> bool:
    if ann is None:
        return False
    if isinstance(ann, ast.Attribute) and ann.attr == "Connection":
        return isinstance(ann.value, ast.Name) and ann.value.id == "sqlite3"
    if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
        return ann.value.replace(" ", "") in {"sqlite3.Connection", "Connection"}
    return False


def _sqlite_factories(tree: ast.AST) -> set[str]:
    """Functions annotated `-> sqlite3.Connection`, i.e. connection factories."""
    return {n.name for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and _is_sqlite_conn_annotation(n.returns)}


def _from_sqlite(value: ast.expr | None, factories: set[str]) -> bool:
    if not isinstance(value, ast.Call):
        return False
    f = value.func
    if (isinstance(f, ast.Attribute) and f.attr == "connect"
            and isinstance(f.value, ast.Name) and f.value.id == "sqlite3"):
        return True
    return isinstance(f, ast.Name) and f.id in factories


def _dotted(node: ast.AST) -> str | None:
    """`self.db` for an Attribute chain rooted in a Name, else None.

    Instance attributes are how OO SQLite code holds its connection
    (`self.db = sqlite3.connect(...)`), and `self.pool` is how a Postgres mirror
    holds its. Both are ast.Attribute, so a receiver check that only understood
    ast.Name was blind to each of them.
    """
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _sqlite_attr_names(tree: ast.AST, factories: set[str]) -> set[str]:
    """Dotted names (`self.db`) that hold a sqlite3 connection.

    Module-wide rather than scope-local, because an attribute is bound in
    __init__ and used in every other method. Fail-closed: a dotted name that is
    EVER assigned from a non-sqlite source is excluded outright, so two classes
    sharing the name `self.db` cannot lend one another an exemption.
    """
    sqlite_bound: set[str] = set()
    other_bound: set[str] = set()
    for node in ast.walk(tree):
        pairs: list[tuple[ast.expr, ast.expr | None]] = []
        annotated = False
        if isinstance(node, ast.Assign):
            pairs = [(t, node.value) for t in node.targets]
        elif isinstance(node, ast.AnnAssign):
            pairs = [(node.target, node.value)]
            annotated = _is_sqlite_conn_annotation(node.annotation)
        for target, value in pairs:
            if not isinstance(target, ast.Attribute):
                continue
            dotted = _dotted(target)
            if dotted is None:
                continue
            if annotated or _from_sqlite(value, factories):
                sqlite_bound.add(dotted)
            else:
                other_bound.add(dotted)
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if not isinstance(item.optional_vars, ast.Attribute):
                    continue
                dotted = _dotted(item.optional_vars)
                if dotted is None:
                    continue
                if _from_sqlite(item.context_expr, factories):
                    sqlite_bound.add(dotted)
                else:
                    other_bound.add(dotted)
    return sqlite_bound - other_bound


def _scopes(tree: ast.AST) -> list[tuple[list[ast.AST], set[str]]]:
    """(nodes owned by a scope, names holding a sqlite3 connection IN it).

    Name resolution has to be per SCOPE, not per module. `conn` is the
    conventional name for an asyncpg connection AND the name the SQLite
    integrations store (server/integrations/core/storage.py, until v201) bound
    to sqlite3.connect() inside `_connect()`. A module-wide name set therefore lets an unrelated function's
    `async def go(conn)` inherit the sqlite label and keep the exemption while
    running its DDL on asyncpg — measured on the real file, which passed.
    So: a parameter SHADOWS an inherited name unless it is itself annotated
    sqlite3.Connection, and an assignment from a non-sqlite source clears the
    name. Fail-closed in both directions.

    Three ways a module legitimately gets a connection: sqlite3.connect()
    directly, a parameter annotated sqlite3.Connection, and a factory
    annotated -> sqlite3.Connection used in a with-block. The first two were
    brain/codesearch/store.py's until its index moved to Postgres (v200), the
    third the SQLite integrations store's until v201.
    """
    factories = _sqlite_factories(tree)
    out: list[tuple[list[ast.AST], set[str]]] = []

    def visit(scope: ast.AST, inherited: set[str]) -> None:  # noqa: C901
        own: list[ast.AST] = []
        nested: list[ast.AST] = []

        def rec(node: ast.AST) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    nested.append(child)
                    continue
                own.append(child)
                rec(child)
        rec(scope)

        conns = set(inherited)
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = scope.args
            args = list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)
            args += [x for x in (a.vararg, a.kwarg) if x is not None]
            for arg in args:
                if _is_sqlite_conn_annotation(arg.annotation):
                    conns.add(arg.arg)
                else:
                    conns.discard(arg.arg)

        for node in own:
            if isinstance(node, ast.Assign):
                sqlite_src = _from_sqlite(node.value, factories)
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        (conns.add if sqlite_src else conns.discard)(t.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if (_from_sqlite(node.value, factories)
                        or _is_sqlite_conn_annotation(node.annotation)):
                    conns.add(node.target.id)
                else:
                    conns.discard(node.target.id)
            elif isinstance(node, ast.withitem) and isinstance(node.optional_vars, ast.Name):
                sqlite_src = _from_sqlite(node.context_expr, factories)
                (conns.add if sqlite_src else conns.discard)(node.optional_vars.id)

        out.append((own, conns))
        for fn in nested:
            visit(fn, conns)

    # Dotted names are seeded into every scope: `self.db` is not a local, so no
    # parameter can shadow it and scope-walking it would only lose it.
    visit(tree, _sqlite_attr_names(tree, factories))
    return out


def _sqlite_conn_names(tree: ast.AST) -> set[str]:
    """Every name that holds a sqlite3 connection in SOME scope of the module."""
    names: set[str] = set()
    for _own, conns in _scopes(tree):
        names |= conns
    return names


def _receiver_key(recv: ast.expr) -> str | None:
    """The name a call's receiver is known by — `db` or `self.db` — else None."""
    if isinstance(recv, ast.Name):
        return recv.id
    return _dotted(recv)


def _split_exec_args(tree: ast.AST) -> tuple[tuple[set[int], set[str]],
                                             tuple[set[int], set[str]]]:
    """((sqlite consts, sqlite names), (non-sqlite consts, non-sqlite names)).

    Both halves are needed and the second one is the point. Requiring that every
    DDL literal reaches a sqlite execute is NECESSARY but not SUFFICIENT: a
    module that keeps its SQLite path and ADDS a Postgres one satisfies it
    forever while issuing CREATE TABLE as the entity role. So the same literal
    is also checked for reaching anything else.

    A receiver this cannot resolve (`get_pool().execute(...)`) counts as
    NOT sqlite — fail-closed, because an unidentifiable connection is exactly
    what an exemption must not be granted on.
    """
    sq_ids: set[int] = set()
    sq_names: set[str] = set()
    other_ids: set[int] = set()
    other_names: set[str] = set()
    for own, conns in _scopes(tree):
        for node in own:
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            attr = node.func.attr
            key = _receiver_key(node.func.value)
            is_sqlite = key is not None and key in conns
            if is_sqlite:
                if attr not in _SQLITE_EXEC_METHODS:
                    continue
                ids, names = sq_ids, sq_names
            else:
                if attr not in _ANY_EXEC_METHODS:
                    continue
                ids, names = other_ids, other_names
            # Keywords as well as positionals: `pool.execute(query=SCHEMA)` is
            # the same call, and reading node.args alone did not see it.
            for arg in list(node.args) + [k.value for k in node.keywords]:
                if isinstance(arg, ast.Name):
                    names.add(arg.id)
                elif isinstance(arg, (ast.Constant, ast.JoinedStr)):
                    ids.add(id(arg))
    return (sq_ids, sq_names), (other_ids, other_names)


def _any_exec_args(tree: ast.AST) -> tuple[set[int], set[str]]:
    """(literal node ids, names) handed to ANY execute/fetch-shaped call, on any
    receiver. Only the inert-fixture check uses this: its claim is that nothing
    runs this DDL, so any such call at all kills the skip."""
    const_ids: set[int] = set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in _ANY_EXEC_METHODS:
            continue
        for arg in list(node.args) + [k.value for k in node.keywords]:
            if isinstance(arg, ast.Name):
                names.add(arg.id)
            elif isinstance(arg, (ast.Constant, ast.JoinedStr)):
                const_ids.add(id(arg))
    return const_ids, names


def _module_imports(tree: ast.AST) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module.split(".")[0])
    return out


def sqlite_faults(src: str, filename: str = "<probe>") -> list[str]:
    """Return the reasons this source is NOT a SQLite module. Empty == valid."""
    tree = ast.parse(src, filename=filename)
    faults: list[str] = []
    imports = _module_imports(tree)

    if "sqlite3" not in imports:
        faults.append("it does not import sqlite3")

    conn_names = _sqlite_conn_names(tree)
    if not conn_names:
        faults.append("nothing in it is known to hold a sqlite3 connection — no "
                      "sqlite3.connect(...), no parameter or factory annotated "
                      "sqlite3.Connection")

    # PER LITERAL, and BOTH directions. "Reaches a sqlite execute" is necessary;
    # "reaches nothing else" is what makes it sufficient. Without the second, a
    # module that keeps its SQLite path and adds a Postgres mirror — the ordinary
    # way this migration would be staged — holds its exemption while issuing
    # CREATE TABLE as the entity role.
    (sq_consts, sq_names), (other_consts, other_names) = _split_exec_args(tree)
    ddl = _ddl_nodes(tree)
    bound = _names_bound_to(tree, {id(n) for n in ddl})
    for node in ddl:
        line = getattr(node, "lineno", 0)
        names = bound.get(id(node), set())
        if not (id(node) in sq_consts or bool(names & sq_names)):
            faults.append(
                f"its DDL at line {line} does not reach an execute call on a name "
                "known to be a sqlite3 connection"
            )
        if id(node) in other_consts or bool(names & other_names):
            faults.append(
                f"its DDL at line {line} ALSO reaches an execute/fetch call on a "
                "receiver that is NOT a known sqlite3 connection — it is running "
                "against something else as well, and that something else is what "
                "this ticket is about"
            )

    pg_imports = sorted(imports & _PG_IMPORTS)
    if pg_imports:
        faults.append(f"it imports the Postgres lane: {', '.join(pg_imports)}")

    # Name AND Attribute forms: `get_pool()` and `db.get_pool()` are the same
    # call, and only the first was visible before.
    pg_calls: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = (f.id if isinstance(f, ast.Name)
                else f.attr if isinstance(f, ast.Attribute) else None)
        if name in _PG_CALLS:
            pg_calls.add(name)
    if pg_calls:
        faults.append("it calls the Postgres lane: "
                      + ", ".join(c + "()" for c in sorted(pg_calls)))
    return faults


def fixture_faults(src: str, rel: str, filename: str = "<probe>") -> list[str]:
    """Return the reasons this source's DDL is NOT inert test data. Empty == valid."""
    tree = ast.parse(src, filename=filename)
    faults: list[str] = []

    if not Path(rel).name.startswith("test_"):
        faults.append("it is not a test module — its filename does not start with 'test_'")

    # Receiver-agnostic on purpose: the claim is that this DDL is executed by
    # NOTHING, so an execute- or fetch-shaped call on ANY receiver kills it.
    exec_consts, exec_names = _any_exec_args(tree)
    ddl = _ddl_nodes(tree)
    bound = _names_bound_to(tree, {id(n) for n in ddl})
    if not ddl:
        faults.append("it holds no DDL at all — remove it from NON_EXECUTED_DDL_MODULES")
    for node in ddl:
        if id(node) in exec_consts or bool(bound.get(id(node), set()) & exec_names):
            faults.append(
                f"its DDL at line {getattr(node, 'lineno', 0)} IS handed to an "
                "execute/fetch call — it is not inert test data any more"
            )
    return faults


def read_allowlist(path: Path) -> list[str]:
    """One repo-relative path per line; blanks and #-comments ignored.

    Matches the reader conventions of scripts/gates-container-free.txt, so an
    inline trailing comment is allowed.
    """
    if not path.is_file():
        return []
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def _self_test() -> int:
    """A gate that cannot fire is not a gate. Measured, not assumed."""
    probe = (
        '"""Module docstring that says CREATE TABLE and must NOT count."""\n'
        "SQL = \"\"\"\n"
        "CREATE TABLE IF NOT EXISTS probe_one (id int);\n"
        "-- the CREATE TABLE above is IF NOT EXISTS\n"
        "/* and ALTER TABLE probe_block is a block comment */\n"
        "CREATE UNIQUE INDEX IF NOT EXISTS probe_idx ON probe_one (id);\n"
        "ALTER TABLE probe_one ADD COLUMN IF NOT EXISTS note text;\n"
        "\"\"\"\n"
    )
    hits = scan_source(probe)
    kinds = sorted({k for _, k, _ in hits})
    objects = sorted({o for _, _, o in hits})

    if kinds != ["ALTER TABLE", "CREATE INDEX", "CREATE TABLE"]:
        print("FAIL: the matcher itself did not flag all three statement kinds in a "
              f"literal it was handed (matched {kinds}).")
        print("      Fix the matcher. Do NOT ship the green run this would otherwise give.")
        return 2
    if len(hits) != 3:
        print(f"FAIL: the matcher found {len(hits)} statements in a literal holding "
              "exactly 3 real ones plus a docstring, a `--` comment and a /* */ block.")
        print(f"      Hits: {hits}")
        print("      Either an exclusion stopped working or the matcher double-counts.")
        return 2
    if "probe_block" in objects:
        print("FAIL: the matcher flagged an ALTER TABLE inside a /* ... */ SQL comment. "
              "Fix strip_sql_comments().")
        return 2
    # The STATEMENT lines, not the literal's opening line (which is 2).
    if sorted(l for l, _, _ in hits) != [3, 6, 7]:
        print(f"FAIL: statements were reported at lines {sorted(l for l, _, _ in hits)}, "
              "not [3, 6, 7] — the literal's opening line is not the line a developer "
              "needs to open. Fix _statement_line().")
        return 2
    if scan_source('"""A docstring mentioning CREATE TABLE foo (id int)."""\n'):
        print("FAIL: the matcher flagged a DOCSTRING. It would fire on prose, which "
              "trains people to bypass it. Fix docstring_node_ids().")
        return 2
    if scan_source('SQL = "-- the CREATE TABLE above is IF NOT EXISTS"\n'):
        print("FAIL: the matcher flagged a `--` SQL comment inside a string literal — "
              "the exact shape of server/applications/registry.py:70. "
              "Fix strip_sql_comments().")
        return 2

    # QUOTE-AWARENESS. Both of these shipped green once, and each one hid real
    # DDL from the gate — which is the only failure mode that matters here,
    # because it is silent. Neither is contrived: the first is a `--` used as
    # data, the second is a shell path glob inside COPY ... FROM PROGRAM.
    for shape, why in (
        ("""SQL = "SELECT '--' AS d; CREATE TABLE sneaky (id int);"\n""",
         "a `--` INSIDE a SQL string literal is data, not a comment. A line-based "
         "stripper ate the rest of the line and the CREATE TABLE with it."),
        ('SQL = """\nCOPY s FROM PROGRAM $$ cat /var/backups/nightly/* $$;\n'
         'CREATE TABLE IF NOT EXISTS sneaky (id int);\n"""\n',
         "an UNTERMINATED /* must restore rather than swallow. A bare depth "
         "counter blanked to end-of-literal, so a path glob hid the CREATE."),
        ('SQL = """\nCOPY s FROM PROGRAM $$ cat /var/backups/nightly/* $$;\n'
         'CREATE /* new in v151 */ TABLE IF NOT EXISTS sneaky (id int);\n"""\n',
         "after an unterminated /* the tail must be RE-MASKED, not handed back "
         "verbatim. Verbatim leaves interior comments in place, and the "
         "patterns need contiguous whitespace, so this DDL stopped matching."),
    ):
        if not scan_source(shape):
            print(f"FAIL: the matcher missed DDL hidden by quoting — {why}")
            print("      Fix _mask_sql(). This is a hole in the ratchet, not a nit.")
            return 2

    # Interpolated identifiers. Every one of these was MISSED before f-strings
    # were flattened; `ALTER TABLE {t}` is the shape that matters most here.
    for shape in (
        'x = f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS c text"\n',
        'x = f"CREATE TABLE {t} (id int)"\n',
        'x = f"CREATE INDEX {n} ON t (id)"\n',
        'x = f"CREATE INDEX IF NOT EXISTS {n} ON entity_memory USING hnsw (c o)"\n',
    ):
        if not scan_source(shape):
            print(f"FAIL: the matcher missed an interpolated identifier: {shape.strip()}")
            print("      The object name in dynamic DDL is routinely an f-string "
                  "placeholder. Fix _flatten_joined()/literal_texts().")
            return 2
    for kw in ("CREATE TRIGGER trg", "CREATE OR REPLACE FUNCTION fn",
               "CREATE VIEW v", "CREATE SEQUENCE s", "CREATE TYPE t",
               "DROP TABLE t", "TRUNCATE TABLE t"):
        if not scan_source(f'x = "{kw} ..."\n'):
            print(f"FAIL: the matcher missed {kw!r}. Fix DDL_PATTERNS.")
            return 2
    if not scan_source('x = "CREATE INDEX ix ON \\"quoted_tbl\\" (id)"\n'):
        print("FAIL: the matcher missed a quoted identifier. Fix _NAME.")
        return 2
    if not scan_source('x = b"CREATE TABLE b_probe (id int)"\n'):
        print("FAIL: the matcher missed a byte-string literal. Fix literal_texts().")
        return 2

    # ── the SQLite classifier, both directions ───────────────────────────
    # A classifier that says yes to everything turns SQLITE_MODULES into the
    # blanket exemption this gate exists to avoid, so it is measured, not
    # assumed. Each 'must fault' case below is a hole that was real.
    good = (
        "import sqlite3\n"
        'SCHEMA: str = "CREATE TABLE IF NOT EXISTS t (id int);"\n'
        "def _connect() -> sqlite3.Connection:\n"
        '    return sqlite3.connect("/app/x.db")\n'
        "def init():\n"
        "    with _connect() as c:\n"
        "        c.executescript(SCHEMA)\n"
    )
    cases: tuple[tuple[str, bool, str], ...] = (
        (good, False,
         "a plain SQLite module, including an annotated `SCHEMA: str = ...`"),
        (good.replace("import sqlite3\n", ""), True,
         "a module that does not import sqlite3"),
        (good.replace("import sqlite3\n", "import sqlite3\nimport asyncpg\n"), True,
         "a module that imports asyncpg"),
        (good.replace("def init():\n", "def init():\n    p = db.get_pool()\n"), True,
         "a module calling db.get_pool() — the attribute form"),
        (good + 'PG_SCHEMA = "CREATE TABLE IF NOT EXISTS u (id int);"\n'
                "async def other(conn):\n"
                "    await conn.fetch(PG_SCHEMA)\n", True,
         "a SECOND DDL literal handed to a non-sqlite connection"),
        ("import sqlite3\n"
         'SCHEMA = "CREATE TABLE IF NOT EXISTS t (id int);"\n'
         "async def init(conn):\n"
         "    await conn.execute(SCHEMA)\n", True,
         "DDL run by .execute() on an injected, non-sqlite receiver"),
        # The variant the synthetic probe above MISSED, found by running the
        # real file: a module that binds `conn = sqlite3.connect(...)` in one
        # function, and an unrelated `async def go(conn)` reusing the name. With
        # module-wide name resolution the parameter inherited the sqlite label
        # and the exemption survived a move to asyncpg.
        ("import sqlite3\n"
         'SCHEMA = "CREATE TABLE IF NOT EXISTS t (id int);"\n'
         "def _connect() -> sqlite3.Connection:\n"
         '    conn = sqlite3.connect("/app/x.db")\n'
         "    return conn\n"
         "async def init(conn):\n"
         "    await conn.execute(SCHEMA)\n", True,
         "an unannotated `conn` parameter shadowing a sqlite-bound name elsewhere"),
        # DUAL-WRITE: the SQLite path is untouched and a Postgres mirror is
        # added beside it. Necessary-only reachability calls this clean, and it
        # is how this module would actually be migrated.
        (good + "async def init_pg(pool):\n"
                "    await pool.execute(SCHEMA)\n", True,
         "a dual-write move — the sqlite path kept, a Postgres mirror added"),
        # ATTRIBUTE RECEIVER: the pool is held on self, so a Name-only receiver
        # check saw neither the exec nor the pg lane.
        (good + "class PgMirror:\n"
                "    async def init(self):\n"
                "        await self.pool.execute(SCHEMA)\n", True,
         "a Postgres mirror whose pool is an attribute (self.pool)"),
        # The FETCH-shaped half of _ANY_EXEC_METHODS. Without these three the
        # set could be trimmed back to the sqlite methods and every case above
        # would still pass, because they all use .execute() — a silent hole in
        # the one file whose thesis is that a gate which cannot fire is not one.
        (good + "async def init_pg(pool):\n    await pool.fetch(SCHEMA)\n", True,
         "a Postgres mirror using pool.fetch()"),
        (good + "async def init_pg(pool):\n    await pool.fetchrow(SCHEMA)\n", True,
         "a Postgres mirror using pool.fetchrow()"),
        (good + "async def init_pg(pool):\n    await pool.fetchval(SCHEMA)\n", True,
         "a Postgres mirror using pool.fetchval()"),
        # Keyword argument — read node.args only and this is invisible.
        (good + "async def init_pg(pool):\n    await pool.execute(query=SCHEMA)\n", True,
         "a Postgres mirror passing the literal as a keyword argument"),
        # The OO SQLite idiom must still PASS. self.db is the connection.
        ("import sqlite3\n"
         "class Store:\n"
         "    def __init__(self):\n"
         '        self.db = sqlite3.connect("/app/x.db")\n'
         '        self.db.executescript("CREATE TABLE IF NOT EXISTS t (id int);")\n',
         False, "a SQLite module holding its connection as self.db"),
    )
    for src, must_fault, what in cases:
        faults = sqlite_faults(src)
        if must_fault and not faults:
            print(f"FAIL: the SQLite classifier ACCEPTED {what}. It would keep exempting "
                  "a module whose DDL no longer runs through sqlite, which is the exact "
                  "hole the exemption would open. Fix sqlite_faults().")
            return 2
        if not must_fault and faults:
            print(f"FAIL: the SQLite classifier REJECTED {what} ({faults}). "
                  "Fix sqlite_faults().")
            return 2

    # ── the inert-fixture classifier, both directions ────────────────────
    fixture = 'CASES = ["CREATE TABLE foo (id INT)", "DROP TABLE entity_memory"]\n'
    if fixture_faults(fixture, "dir/test_probe.py"):
        print("FAIL: the fixture classifier REJECTED DDL that nothing executes "
              f"({fixture_faults(fixture, 'dir/test_probe.py')}). Fix fixture_faults().")
        return 2
    if not fixture_faults(fixture, "dir/probe.py"):
        print("FAIL: the fixture classifier ACCEPTED a module that is not a test. "
              "Fix fixture_faults().")
        return 2
    if not fixture_faults(
            fixture + "def run(conn):\n    conn.execute(CASES[0])\n"
                      '    conn.execute("CREATE TABLE foo (id INT)")\n',
            "dir/test_probe.py"):
        print("FAIL: the fixture classifier ACCEPTED DDL that IS executed. "
              "Fix fixture_faults().")
        return 2
    return _self_test_restore_load()


def _self_test_restore_load() -> int:
    """RESTORE_LOAD_DDL's negative controls: the exception holds only for the
    named functions, the named kinds, and statements sent through _psql to the
    named database; everything else in the file is counted."""
    rel = "server/probe_restore.py"
    table = {(rel, "_load_rows"): (frozenset({"TRUNCATE"}), "RESTORE_DB", frozenset()),
             (rel, "settle"): (frozenset({"ALTER TABLE"}), "db", frozenset({"carry"}))}
    head = ("RESTORE_DB = 'r'\n"
            "def _psql(*c, db='postgres'):\n    return c\n")
    good = (head
            + "def _load_rows(t):\n    _psql(f'TRUNCATE {t} RESTART IDENTITY', db=RESTORE_DB)\n"
            + "def settle(db, t, c):\n    _psql(f'ALTER TABLE {t} DROP COLUMN IF EXISTS {c}', db=db)\n"
            + "def carry(live):\n    return settle(live, 't', 'c')\n")
    exempt, counted, faults = restore_load_faults(good, rel, table=table)
    if len(exempt) != 2 or counted or faults:
        print(f"FAIL: RESTORE_LOAD_DDL refused its own good case ({exempt}, {counted}, {faults}). "
              "Fix restore_load_faults().")
        return 2
    bad = {
        "a statement sent to the live database": good.replace(
            "RESTART IDENTITY', db=RESTORE_DB", "RESTART IDENTITY', db=LIVE"),
        "a statement run on a pool": good.replace(
            "def _load_rows(t):\n", "def _load_rows(t, pool=None):\n    pool.execute('SELECT 1')\n"),
        "settle called from outside carry": good + "def boot():\n    settle('x', 't', 'c')\n",
        "the exempt function gone": good.replace("def settle(db, t, c)", "def settled(db, t, c)"),
    }
    for why, src in bad.items():
        if not restore_load_faults(src, rel, table=table)[2]:
            print(f"FAIL: RESTORE_LOAD_DDL accepted {why}. Fix restore_load_faults().")
            return 2
    counted_cases = {
        "a kind the function is not named for (CREATE ROLE)": good.replace(
            "def _load_rows(t):\n", "def _load_rows(t):\n    _psql('CREATE ROLE x LOGIN', db=RESTORE_DB)\n"),
        "DDL in a function that is not named": good + "def other(t):\n    _psql(f'ALTER TABLE {t} ADD COLUMN c int', db=RESTORE_DB)\n",
        "DDL at module level": good + "SQL = 'CREATE TABLE IF NOT EXISTS y (i int)'\n",
    }
    for why, src in counted_cases.items():
        if not restore_load_faults(src, rel, table=table)[1]:
            print(f"FAIL: RESTORE_LOAD_DDL exempted {why}: it must be counted. Fix restore_load_faults().")
            return 2
    return 0


def _unclaimed_roots() -> tuple[dict[str, list[str]], int]:
    """({top-level dir -> files holding DDL}, 2 if the walk itself failed).

    The completeness check on ROOTS. Anything outside ROOTS and outside
    NON_RUNTIME_DIRS that holds DDL is a root nobody declared.
    """
    found: dict[str, list[str]] = {}
    for path in sorted(_ROOT.rglob("*.py")):
        if not path.is_file():
            continue
        try:
            rel_path = path.relative_to(_ROOT)
        except ValueError:  # pragma: no cover - rglob cannot produce this
            continue
        parts = rel_path.parts
        if _SKIP_DIRS & set(parts):
            continue
        top = parts[0]
        if top in ROOTS or top in NON_RUNTIME_DIRS:
            continue
        try:
            if scan_source(path.read_text(encoding="utf-8"), filename=str(path)):
                found.setdefault(top, []).append(rel_path.as_posix())
        except (OSError, SyntaxError, UnicodeDecodeError):
            # A file this gate cannot parse is not evidence of a new root, and
            # failing on every unparseable .py anywhere in the repo would make
            # the completeness check the noisiest thing in the suite. Files
            # INSIDE the roots are still hard-failed; see main().
            continue
    return found, 0


def _exempt_dir_imports(files: list[Path]) -> dict[str, list[str]]:
    """{exempt dir -> files under ROOTS that import from it}.

    The reason `scripts/` is exempt is not "it is absent from the image" — 92 of
    its files are COPY'd in. It is "nothing on a request path imports it", and
    that is a claim about code, so it gets re-derived rather than remembered. If
    a module under ROOTS ever does `from scripts... import ...`, the exempt
    directory is on a request path and its DDL is in scope again.
    """
    out: dict[str, list[str]] = {}
    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue  # main() hard-fails these separately
        roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                roots.add(node.module.split(".")[0])
        for hit in sorted(roots & set(NON_RUNTIME_DIRS)):
            out.setdefault(hit, []).append(path.relative_to(_ROOT).as_posix())
    return out


def _validate_skips() -> tuple[set[str], list[tuple[str, list[str]]], int]:
    """(validated skips, broken skips, 2 if validation itself failed else 0)."""
    ok: set[str] = set()
    broken: list[tuple[str, list[str]]] = []
    for entries, kind in ((SQLITE_MODULES, "sqlite"),
                          (NON_EXECUTED_DDL_MODULES, "fixture")):
        for rel in entries:
            path = _ROOT / rel
            if not path.is_file():
                broken.append((rel, ["the file does not exist"]))
                continue
            try:
                src = path.read_text(encoding="utf-8")
                faults = (sqlite_faults(src, filename=str(path)) if kind == "sqlite"
                          else fixture_faults(src, rel, filename=str(path)))
            except (OSError, SyntaxError, UnicodeDecodeError) as exc:
                print(f"FAIL: cannot read or parse the skip-list entry {rel}: {exc!r}")
                print("      The skip could not be validated, so this gate does not know "
                      "whether that module is exempt. Fix the file rather than trusting it.")
                return ok, broken, 2
            if faults:
                broken.append((rel, faults))
            else:
                ok.add(rel)
    return ok, broken, 0


def main() -> int:
    rc = _self_test()
    if rc:
        return rc

    files: list[Path] = []
    for root in ROOTS:
        base = _ROOT / root
        if not base.is_dir():
            print(f"FAIL: no {root}/ directory under {_ROOT} — this gate checked only "
                  "part of what it claims to check, and a partial run that exits 0 is a "
                  "dead gate, not a clean tree.")
            return 2
        found = sorted(p for p in base.rglob("*.py") if p.is_file())
        if not found:
            print(f"FAIL: found 0 .py files under {base} — this gate checked nothing there.")
            return 2
        files.extend(found)

    skipped_ok, broken_skips, rc = _validate_skips()
    if rc:
        return rc

    ddl_by_file: dict[str, list[tuple[int, str, str]]] = {}
    total = 0
    skipped_statements = 0
    restore_load_statements = 0
    restore_files = {p for p, _ in RESTORE_LOAD_DDL}
    for rel in sorted(restore_files):
        if not (_ROOT / rel).is_file():
            broken_skips.append((rel, ["the file does not exist: remove it from RESTORE_LOAD_DDL"]))
    for path in files:
        rel = path.relative_to(_ROOT).as_posix()
        try:
            src = path.read_text(encoding="utf-8")
            hits = scan_source(src, filename=str(path))
            if rel in restore_files:
                exempt, hits, faults = restore_load_faults(src, rel, filename=str(path))
                restore_load_statements += len(exempt)
                if faults:
                    broken_skips.append((rel, [f"RESTORE_LOAD_DDL: {f}" for f in faults]))
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:
            print(f"FAIL: cannot read or parse {rel}: {exc!r}")
            print("      A file under a scanned root that this gate cannot parse is a "
                  "file it cannot check. Fix the file rather than skipping it.")
            return 2
        if not hits:
            continue
        if rel in skipped_ok:
            skipped_statements += len(hits)
            continue
        ddl_by_file[rel] = hits
        total += len(hits)

    elsewhere = restore_load_callers_elsewhere(files)
    if elsewhere:
        broken_skips.append(("RESTORE_LOAD_DDL callers", elsewhere))

    unclaimed, rc = _unclaimed_roots()
    if rc:
        return rc
    reached_exempt = _exempt_dir_imports(files)

    allowed = read_allowlist(_ALLOWLIST)
    violations = {f: h for f, h in ddl_by_file.items() if f not in set(allowed)}
    double_listed = [f for f in allowed if f in skipped_ok]
    # A double-listed file gets its own, more accurate message below; it would
    # otherwise read as "no DDL left", which is not why it is wrong.
    stale = [f for f in allowed if f not in ddl_by_file and f not in skipped_ok]

    failed = bool(violations or stale or broken_skips or double_listed
                  or unclaimed or reached_exempt)
    if failed:
        print("FAILED: runtime DDL gate")
        print()

    if reached_exempt:
        print(f"  {len(reached_exempt)} exempt director(y/ies) in NON_RUNTIME_DIRS "
              "are now imported from inside a scanned root:")
        print()
        for top in sorted(reached_exempt):
            importers = reached_exempt[top]
            shown = importers[:10]
            print(f"    {top}/  — imported by {len(importers)} file(s) under ROOTS:")
            for rel in shown:
                print(f"        {rel}")
            if len(importers) > len(shown):
                print(f"        ... and {len(importers) - len(shown)} more")
        print()
        print("  That directory is exempt ONLY because nothing on a request path")
        print("  imports it — not because it is absent from the image (92 scripts/")
        print("  files are COPY'd in). An import from server/ or brain/ puts its DDL")
        print("  back on a request path, which is exactly what this gate is about, so")
        print("  the exemption no longer holds.")
        print()
        print("  WHAT TO DO. Either drop the import (a request path should not be")
        print("  reaching into a gate or a one-off tool), or remove the directory from")
        print("  NON_RUNTIME_DIRS and treat its DDL as in scope — add it to ROOTS and")
        print("  allowlist the files as backlog. Editing the reason string is not a fix;")
        print("  the reason is derived here, not read from it.")
        print()

    if unclaimed:
        n = sum(len(v) for v in unclaimed.values())
        print(f"  {n} file(s) hold runtime DDL in {len(unclaimed)} top-level "
              f"director(y/ies) this gate does not scan: {', '.join(sorted(unclaimed))}")
        print()
        for top in sorted(unclaimed):
            for rel in unclaimed[top]:
                print(f"    {rel}")
        print()
        print("  ROOTS in scripts/test_no_runtime_ddl.py names the directories whose DDL")
        print("  is on a request path, and this check is what stops that list going stale")
        print("  the way it already did once: brain/ held 20 statements while the gate")
        print("  read server/ only, and nothing said so.")
        print()
        print("  WHAT TO DO. If that code runs inside a request, ADD its directory to")
        print("  ROOTS and allowlist the files as backlog — that is the whole point of")
        print("  this firing. If it genuinely never runs in a request (a gate, a one-off")
        print("  tool, a test fixture), add the directory to NON_RUNTIME_DIRS WITH the")
        print("  reason, so the next reader can check the claim instead of trusting it.")
        print("  Do not widen the exempt set to quieten this without writing down why.")
        print()

    if broken_skips:
        print(f"  {len(broken_skips)} module(s) on a skip list in "
              "scripts/test_no_runtime_ddl.py no longer qualify for the skip:")
        print()
        for rel, faults in broken_skips:
            print(f"    {rel}")
            for fault in faults:
                print(f"        {fault}")
        print()
        print("  SQLITE_MODULES is skipped because this rule is about the PostgreSQL")
        print("  `entity` role, and a pod-local SQLite file has no role to downgrade.")
        print("  NON_EXECUTED_DDL_MODULES is skipped because DDL that is test DATA")
        print("  creates nothing. A module that no longer fits its reason has no claim")
        print("  on the skip, and leaving it listed is what would turn these into the")
        print("  blanket exemption this gate exists to avoid — a module that moved to")
        print("  Postgres sailing through on a stale label.")
        print()
        print("  WHAT TO DO. If the module moved to Postgres: remove it from the skip")
        print("  list, then either migrate its DDL into migrations/vNNN_*.sql or add it")
        print("  to scripts/runtime-ddl-allowlist.txt as backlog. If it is still SQLite")
        print("  (or still inert) and this fired anyway, the property it lost is named")
        print("  above; restore that property rather than widening the classifier.")
        print()

    if violations:
        n = sum(len(h) for h in violations.values())
        print(f"  {n} runtime DDL statement(s) in {len(violations)} file(s) that are NOT "
              "on the allowlist:")
        print()
        for rel in sorted(violations):
            for lineno, kind, obj in sorted(violations[rel]):
                print(f"    {rel}:{lineno}  {kind} {obj}")
        print()
        print("  Schema is declared in migrations/*.sql and nowhere else. Runtime DDL")
        print("  keeps DDL rights on the kernel's database role forever — which is the")
        print("  thing blocking the DB role split — and lets a host's schema depend on")
        print("  which code paths happened to run there.")
        print()
        print("  WHAT TO DO. Put the statement in a new migrations/vNNN_*.sql and delete")
        print("  it from the feature module; reads and writes stay. The migration may only")
        print("  ADD what is missing: CREATE TABLE IF NOT EXISTS, ALTER TABLE ... ADD")
        print("  COLUMN IF NOT EXISTS, CREATE [UNIQUE] INDEX IF NOT EXISTS. Never")
        print("  ALTER COLUMN ... TYPE, never SET NOT NULL on an existing column, never a")
        print("  DROP — entrypoint.sh runs migrations with ON_ERROR_STOP=1, so a statement")
        print("  that collides with live data is a host that does not boot.")
        print()
        print("  Adding the file to scripts/runtime-ddl-allowlist.txt is NOT the fix. That")
        print("  list is the migration backlog and it only ever shrinks.")
        print()

    if stale:
        print(f"  {len(stale)} allowlist entr(y/ies) in scripts/runtime-ddl-allowlist.txt "
              "no longer carry runtime DDL:")
        print()
        for rel in stale:
            why = "file does not exist" if not (_ROOT / rel).is_file() else "no DDL left"
            print(f"    {rel}  ({why}) — remove it from the allowlist")
        print()
        print("  Remove it from the allowlist. An entry left behind after its band is")
        print("  migrated is a standing exemption for a file that has earned none: the")
        print("  next runtime CREATE added there would land silently. The end state of")
        print("  this file is EMPTY.")
        print()

    if double_listed:
        print(f"  {len(double_listed)} file(s) are on BOTH a skip list and "
              "scripts/runtime-ddl-allowlist.txt:")
        print()
        for rel in double_listed:
            print(f"    {rel} — remove it from the allowlist")
        print()
        print("  A validated skip is out of scope, not backlog. Its allowlist entry can")
        print("  never be worked off, so it would sit there forever looking like the")
        print("  migration is unfinished.")
        print()

    if failed:
        return 1

    scanned = " + ".join(r + "/" for r in ROOTS)
    skipped = ""
    if skipped_ok:
        skipped = (f" {skipped_statements} more in {len(skipped_ok)} re-validated "
                   "skip-list module(s) — SQLite, and DDL that is inert test data — "
                   "are out of scope.")
    if restore_load_statements:
        skipped += (f" {restore_load_statements} more are a restore's load in its own database "
                    "(RESTORE_LOAD_DDL, re-validated).")

    if not allowed:
        print(f"PASSED: no PostgreSQL runtime DDL anywhere in {scanned} and the allowlist "
              "is EMPTY; schema is declared in migrations/*.sql only."
              + skipped)
        return 0

    print(f"PASSED: runtime DDL gate — {total} statement(s) across {len(ddl_by_file)} "
          f"file(s) in {scanned}, every one of them on the {len(allowed)}-entry allowlist "
          "(scripts/runtime-ddl-allowlist.txt), and no allowlist entry has gone stale. "
          "The list only shrinks." + skipped)
    return 0


if __name__ == "__main__":
    sys.exit(main())
