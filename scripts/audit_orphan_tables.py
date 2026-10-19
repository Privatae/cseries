#!/usr/bin/env python3
"""
Schema-vs-code table-hygiene ratchet. Four arms, three of them static.

The rule: the DB is where latent defects hide. A table declared
in schema but never touched by runtime code (the `project_files` / `voice_cost_ledger`
pattern) is a silent defect — the feature it was for was never wired, or was
ripped out and left the table behind.

That rule shipped with ONE arm, and it could only ever see one direction:
`declared - referenced`, where the candidate set IS `declared`. Two defect
classes lived in its blind spot:

  * REFERENCED BUT NEVER DECLARED. `scored_outcomes` had a live INSERT in
    brain_memory.py and no CREATE in any .sql, ever. The write raised on every
    host built from the current schema and the exception was swallowed by a
    log.warning. Arm 1 cannot see it: a name that was never declared is never
    a candidate, so it is never searched for.
  * RETIREMENT RESIDUE. Removing a CREATE only affects hosts built afterwards;
    every existing host keeps the table and its rows forever. And because the
    removal also drops the name from `declared`, the orphan it leaves becomes
    INVISIBLE to arm 1 at exactly the moment it is created. `signal_queue` was
    retired (init-db.sql:1154-1161), the writer genuinely deleted, and no DROP
    migration shipped — when measured it still held 2229 rows / 32 MB on
    one Host and an empty table on another.

    AND IT IS WHY THIS TOOL REPORTS RATHER THAN PRESCRIBES. Finding
    signal_queue was correct. The conclusion drawn from it — "abandoned, drop
    it" — was not. It is the staging table for research provenance: an
    `embedding` column, a `promoted_memory_ids` column, rows moving pending ->
    summarized, output landing in entity_memory. It is to be re-wired, not
    deleted. A drop migration had already been
    written for it and was reverted before it shipped.

    So a table this tool names is a table nobody has looked at recently. That
    is ALL it means. "Declared nowhere" is computable from the schema; "safe to
    delete" is a judgement about a product and is not. The two errors are not
    symmetric — a table left standing costs storage, a table dropped costs data
    nobody can get back.

WHY THE NEW COVERAGE IS STILL STATIC. The honest answer to "just read a live
DB" is that the smoke gate has no host DB to read (scripts/smoke_test.sh drives
a throwaway container), so a DB-dependent gate would be either skipped or
permanently red — and a permanently red light is one the operator learns to
skip past. So arms 2 and 3 are static and run in the default gate with the same
contract arm 1 always had (parse files, no DB, exit 1 on a NEW finding), and the
live arm is OPT-IN and never part of a bare invocation:

  1. declared - referenced        static   baseline: orphan_tables_baseline.txt
  2. sql-referenced - declared    static   baseline: undeclared_tables_baseline.txt
  3. retirement needs a DROP      static   ledger:   declared_tables_baseline.txt
  4. live - (declared u known)    OPT-IN   needs a table list from a real host

  python3 scripts/audit_orphan_tables.py                  # the gate (arms 1-3)
  python3 scripts/audit_orphan_tables.py --list           # what each arm sees
  python3 scripts/audit_orphan_tables.py --update-baseline  # freeze today's sets
  python3 scripts/audit_orphan_tables.py --live <file>     # + arm 4, from a
        # newline-separated list of live table names, e.g.
        #   docker exec <container> psql -U entity \\
        #     -d hostmem -h /var/run/postgresql -tAc \\
        #     "SELECT tablename FROM pg_tables WHERE schemaname='public'" > /tmp/live.txt
"""
from __future__ import annotations

import ast
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BASELINE = os.path.join(_ROOT, "scripts", "orphan_tables_baseline.txt")
_UNDECLARED_BASELINE = os.path.join(_ROOT, "scripts", "undeclared_tables_baseline.txt")
_DECLARED_LEDGER = os.path.join(_ROOT, "scripts", "declared_tables_baseline.txt")

# Where runtime code lives (a table named here is "wired"). Deliberately EXCLUDES
# migrations/, docs/, *.md, NOTES/, and scripts/test_* — a table that appears
# ONLY in a migration or a doc is exactly the orphan we want to surface.
_CODE_DIRS = ("server", "brain", "cli", "services", "system-apps", "sidecars")

# Schema-qualified or not, so `CREATE TABLE IF NOT EXISTS public.foo` declares
# `foo`. It once did not: the pattern captured the first
# identifier, so a qualified CREATE registered a table called `public` and left
# the real one reading as UNDECLARED — arm 2 would then fail on a name the repo
# does declare, and arm 1 would carry a phantom `public` forever. _DROP_RE below
# had always been written the tolerant way; this is the same rule on both halves.
_CREATE_RE = re.compile(
    r"CREATE TABLE\s+(?:IF NOT EXISTS\s+)?(?:[A-Za-z_][A-Za-z0-9_]*\.)?([A-Za-z_][A-Za-z0-9_]*)", re.I)
# A retirement's other half. Matched case-insensitively and schema-qualified or
# not, so `DROP TABLE IF EXISTS public.foo` counts.
_DROP_RE = re.compile(
    r"DROP TABLE\s+(?:IF EXISTS\s+)?(?:[A-Za-z_][A-Za-z0-9_]*\.)?([A-Za-z_][A-Za-z0-9_]*)", re.I)

# ── arm 2: table names as they appear in SQL the app actually runs ────────────
# Scanned over STRING LITERALS ONLY (via ast), because a bare regex over file
# text reads English prose in comments as SQL: the first cut of this returned
# "THE", "WHERE", "a", "can", "it" and "the" as table names. Docstrings are
# excluded for the same reason — they are prose that happens to contain SQL
# words, never SQL the app runs.
_IDENT = r"(?:[A-Za-z_][A-Za-z0-9_]*\.)?([A-Za-z_][A-Za-z0-9_]*)"
_DML_RES = [re.compile(p) for p in (
    r"INSERT\s+INTO\s+(?:ONLY\s+)?" + _IDENT,
    r"UPDATE\s+(?:ONLY\s+)?" + _IDENT + r"\s+SET\b",
    r"DELETE\s+FROM\s+(?:ONLY\s+)?" + _IDENT,
    r"\bFROM\s+(?:ONLY\s+)?" + _IDENT,
    r"\bJOIN\s+" + _IDENT,
)]
# `name AS (` is a CTE or a derived-table alias, never a table. Collected
# PER FILE, not per string: a query assembled from several f-string pieces
# defines its CTE in one literal and references it in another.
_ALIAS_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s+AS\s*\(")
_SQL_HINT_RE = re.compile(r"\b(SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM|CREATE\s+TABLE)\b")
_SQL_COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)
# Single-quoted values and double-quoted identifiers, contents blanked rather
# than removed so offsets and word boundaries around them survive. '' and ""
# escapes are consumed as part of the run, which is why the inner alternation
# allows a doubled quote.
_SQL_STRING_RE = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"")
# GRANT/REVOKE name a ROLE where the DML matchers expect a table, and the bare
# `\bFROM\s+IDENT` arm below cannot tell the difference:
# `REVOKE ALL ON kernel_secrets FROM host_recall` reads as a reference to a
# table called `host_recall`, which no .sql declares, so the gate fails on a
# role. Measured the first time runtime SQL in this repo contained a
# REVOKE. Stripped for the same reason comments are — this pass is looking for
# tables, and a privilege statement names none. The TABLE in `REVOKE ... ON
# <table>` is not lost by this: it is declared in a .sql or it is caught at its
# CREATE, which is where this gate wants it named anyway.
#
# A PRIVILEGE KEYWORD IS REQUIRED AFTER THE VERB, because the first cut matched
# the bare WORD and that is not the same thing. Three constructions each hid a
# genuinely undeclared table behind a data value rather than a statement:
#     WHERE op = 'revoke' AND EXISTS (SELECT 1 FROM totally_undeclared ...)
#     terms->>'grant' = 'yes' AND EXISTS (SELECT 1 FROM also_hidden)
#     SELECT 'grant refused' FROM undeclared_src
# In each case everything from the quoted word to the end of the statement was
# stripped, taking the real FROM with it — a gate that hides the thing it exists
# to find. No runtime SQL in this repo contains such a value today, so the
# earlier 17/17 measurement stands, but "today" is not a property worth
# depending on.
_PRIVILEGES = (
    r"ALL|SELECT|INSERT|UPDATE|DELETE|TRUNCATE|REFERENCES|TRIGGER|CREATE|"
    r"CONNECT|TEMPORARY|TEMP|EXECUTE|USAGE|SET|MAINTAIN|ALTER\s+SYSTEM"
)
# ...AND `ON` IS REQUIRED AFTER THE PRIVILEGE LIST, because a privilege keyword
# alone is still satisfied by a two-word data value: `op = 'revoke all'`,
# `'grant select'`, `'grant usage'` all passed the keyword test and went on
# swallowing the rest of the statement.
#
# THAT IS NARROWING, NOT A FIX, and it is worth saying so rather than claiming
# more than it does. A quoted value CAN contain the full statement shape —
# `'grant all on'`, `'revoke select on demand'`, `"grant all on"` as an
# identifier — and each of those still swallowed its statement. What actually
# closes the class is blanking string literals BEFORE this pass runs; see
# _SQL_STRING_RE and its use below. This pattern stays tight anyway, because
# defence that only works in one layer is the kind that gets removed by someone
# who cannot see why it is there.
_PRIV_ITEM = rf"(?:{_PRIVILEGES})(?:\s*\([^)]*\))?"
_GRANT_REVOKE_RE = re.compile(
    r"\b(?:GRANT|REVOKE)\s+(?:GRANT\s+OPTION\s+FOR\s+)?"
    rf"{_PRIV_ITEM}(?:\s*,\s*{_PRIV_ITEM})*"
    r"(?:\s+PRIVILEGES)?\s+ON\b.*?(?=;|$)",
    re.I | re.S)
# Words that can sit where a table name sits and are not one.
_SQL_NOISE = {"LATERAL", "ONLY", "VALUES", "SELECT", "DUAL", "UNNEST"}


def _iter_files(root: str, exts: tuple[str, ...]):
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            if f.endswith(exts):
                yield os.path.join(dirpath, f)


def _iter_code_files():
    for d in _CODE_DIRS:
        root = os.path.join(_ROOT, d)
        if not os.path.isdir(root):
            continue
        for path in _iter_files(root, (".py",)):
            # Same exclusion the module docstring claims for scripts/test_*:
            # a fixture's `unknown_table` is not a schema defect.
            if os.path.basename(path).startswith("test_"):
                continue
            yield path


def declared_tables() -> set[str]:
    """Table names from every CREATE TABLE across the repo's .sql files."""
    tables: set[str] = set()
    for path in _iter_files(_ROOT, (".sql",)):
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for m in _CREATE_RE.finditer(text):
            tables.add(m.group(1))
    return tables


def dropped_tables() -> set[str]:
    """Table names a migration DROPs — the half a retirement has to ship.

    Scoped to migrations/ on purpose. A DROP in init-db.sql runs only on a
    brand-new host, which is precisely the host that never had the table; only
    a migration reaches the EXISTING hosts that carry the residue.
    """
    tables: set[str] = set()
    mig = os.path.join(_ROOT, "migrations")
    if not os.path.isdir(mig):
        return tables
    for path in _iter_files(mig, (".sql",)):
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for m in _DROP_RE.finditer(text):
            tables.add(m.group(1))
    return tables


def referenced_tables(candidates: set[str]) -> set[str]:
    """Of `candidates`, which appear (word-bounded) in any runtime .py file."""
    if not candidates:
        return set()
    # One big alternation, matched per file. Word-bounded so `decisions` doesn't
    # match `decision_patterns`.
    pat = re.compile(r"\b(" + "|".join(re.escape(t) for t in sorted(candidates)) + r")\b")
    found: set[str] = set()
    for path in _iter_code_files():
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for m in pat.finditer(text):
            found.add(m.group(1))
        if found == candidates:
            return found
    return found


def sql_referenced_tables() -> dict[str, set[str]]:
    """Every table name runtime SQL names, mapped to the files naming it.

    The inverse of referenced_tables(): it needs no candidate set, so it can see
    a name that was never declared anywhere. That is the whole point — arm 1
    structurally cannot.
    """
    refs: dict[str, set[str]] = {}
    for path in _iter_code_files():
        try:
            src = open(path, encoding="utf-8", errors="replace").read()
            tree = ast.parse(src)
        except (OSError, SyntaxError, ValueError):
            continue
        docstrings = {
            ast.get_docstring(n, clean=False)
            for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        literals = [
            n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and n.value not in docstrings and _SQL_HINT_RE.search(n.value)
        ]
        # STRING LITERALS GO FIRST, and that is what actually closes the class.
        # Tightening the GRANT/REVOKE pattern only ever moves the goalposts: it
        # started matching the bare word, then a word plus a privilege, and a
        # quoted value can carry any of those shapes — `'grant all on'`,
        # `'revoke select on demand'`, even a quoted IDENTIFIER. Blanking the
        # contents of every literal first means no data value can look like a
        # statement to either pass, and the DML matchers below stop reading a
        # value as a table name at the same time.
        cleaned = [
            _GRANT_REVOKE_RE.sub(" ", _SQL_STRING_RE.sub("''", _SQL_COMMENT_RE.sub(" ", s)))
            for s in literals
        ]
        aliases: set[str] = set()
        for s in cleaned:
            aliases |= {m.group(1) for m in _ALIAS_RE.finditer(s)}
        for s in cleaned:
            for rx in _DML_RES:
                for m in rx.finditer(s):
                    name = m.group(1)
                    if name in aliases or name.upper() in _SQL_NOISE:
                        continue
                    if name.startswith("pg_") or name.startswith("information_schema"):
                        continue
                    # A catalog view named with its schema (information_schema.columns,
                    # pg_catalog.pg_class): the matcher keeps the part after the dot.
                    if re.search(r"(information_schema|pg_catalog)\.\s*$", s[:m.start(1)]):
                        continue
                    if s[m.end():m.end() + 1] == "(":       # FROM json_each(...)
                        continue
                    if "EXTRACT(" in s[max(0, m.start() - 40):m.start()].upper():
                        continue                            # EXTRACT(EPOCH FROM col)
                    refs.setdefault(name, set()).add(os.path.relpath(path, _ROOT))
    return refs


def _load_list(path: str) -> set[str]:
    if not os.path.exists(path):
        return set()
    out: set[str] = set()
    for line in open(path, encoding="utf-8"):
        line = line.split("#", 1)[0].strip()
        if line:
            out.add(line)
    return out


def _split_qualified(entries: set[str]) -> tuple[set[str], dict[str, set[str]]]:
    """Baseline entries are a bare table name, excused wherever it is named, or
    `name @ path`, excused ONLY in that file: a table that belongs to another
    program's own database (opencode's `part`) must never excuse a future Host
    table of the same name."""
    plain: set[str] = set()
    qualified: dict[str, set[str]] = {}
    for e in entries:
        if "@" in e:
            name, path = (x.strip() for x in e.split("@", 1))
            qualified.setdefault(name, set()).add(path)
        else:
            plain.add(e)
    return plain, qualified


def load_baseline() -> set[str]:
    return _load_list(_BASELINE)


def compute_orphans() -> set[str]:
    """Arm 1 — declared in .sql, named by no runtime .py. Unchanged."""
    declared = declared_tables()
    referenced = referenced_tables(declared)
    return declared - referenced


def compute_undeclared() -> dict[str, set[str]]:
    """Arm 2 — runtime SQL names it, no .sql declares it."""
    declared = declared_tables()
    return {n: files for n, files in sql_referenced_tables().items() if n not in declared}


def compute_undropped_retirements() -> set[str]:
    """Arm 3 — a name that LEFT the declared set with no DROP migration behind it.

    The ledger is the memory arm 1 does not have. Without it a retirement is
    indistinguishable from a table that never existed, which is why the residue
    it leaves on existing hosts is invisible from the moment it is created.
    """
    ledger = _load_list(_DECLARED_LEDGER)
    if not ledger:
        return set()
    return ledger - declared_tables() - dropped_tables()


def _existing_entries(path: str) -> list[tuple[list[str], str]]:
    """The file's entries as (comment lines above it, name), in file order.

    A baseline entry's REASON is the only thing that makes it reviewable — it is
    what separates "accepted, here is why" from "silenced". --update-baseline
    used to rewrite the file from the header down, which DELETED every reason in
    it: measured, one run dropped the retirement notes on
    bug_reports / debug_reporters / codebook_recommendations. So a regeneration
    now preserves existing lines verbatim and in place, and only appends what is
    new. Order is kept rather than re-sorted, because a comment block written
    above a PAIR of names cannot survive re-sorting intact.
    """
    entries: list[tuple[list[str], str]] = []
    if not os.path.exists(path):
        return entries
    pending: list[str] = []
    # The comment run at the top is the file header, not an annotation on
    # whichever name follows it. It is delimited by the blank line _write_list
    # always emits after it. Ending the header at the first NAME instead would
    # swallow the first real annotation on the next regeneration and lose it
    # permanently — measured: four regenerations ate the "Frozen" block and
    # bastion_pairing_tokens' reason.
    in_header = True
    for lineno, raw in enumerate(open(path, encoding="utf-8"), start=1):
        line = raw.rstrip("\n")
        stripped = line.strip()
        if not stripped:
            if in_header:
                in_header = False     # the blank line after the header ends it
            else:
                pending.append("")    # keep the author's spacing
            continue
        if stripped.startswith("#"):
            if not in_header:
                pending.append(line)
            continue
        in_header = False
        name = stripped.split("#", 1)[0].strip()
        if name:
            entries.append((pending, name))
        pending = []
    return entries


def _write_list(path: str, header: list[str], names) -> None:
    names = set(names)
    kept = [(c, n) for c, n in _existing_entries(path) if n in names]
    added = sorted(names - {n for _c, n in kept})
    with open(path, "w", encoding="utf-8") as fh:
        for line in header:
            fh.write(line.rstrip() + "\n")
        fh.write("\n")               # the delimiter _existing_entries reads back
        for comments, name in kept:
            for c in comments:
                fh.write(c + "\n")
            fh.write(name + "\n")
        for name in added:
            fh.write(name + "\n")


def main() -> int:
    args = sys.argv[1:]
    arg = args[0] if args else ""

    if arg == "--list":
        orphans = compute_orphans()
        undeclared = compute_undeclared()
        retired = compute_undropped_retirements()
        print("── arm 1: declared in .sql, no runtime caller ──")
        for t in sorted(orphans):
            print(f"  {t}")
        print(f"  {len(orphans)} orphan table(s).\n")
        print("── arm 2: named by runtime SQL, declared in no .sql ──")
        for t in sorted(undeclared):
            print(f"  {t:34s} {', '.join(sorted(undeclared[t])[:2])}")
        print(f"  {len(undeclared)} undeclared reference(s).\n")
        print("── arm 3: retired from schema with no DROP migration ──")
        for t in sorted(retired):
            print(f"  {t}")
        print(f"  {len(retired)} undropped retirement(s).")
        return 0

    if arg == "--update-baseline":
        orphans = compute_orphans()
        _write_list(_BASELINE, [
            "# Orphan tables: declared in *.sql, referenced by no runtime .py.",
            "# Accepted/known as of the freeze below. Regenerate with:",
            "#   python3 scripts/audit_orphan_tables.py --update-baseline",
            "# The linter FAILS on any orphan NOT listed here (new dead schema).",
        ], orphans)
        undeclared = compute_undeclared()
        _write_list(_UNDECLARED_BASELINE, [
            "# Tables runtime SQL names that NO *.sql declares (arm 2).",
            "# Legitimate entries are tables the application CREATEs at runtime",
            "# (entrypoint.sh, an ensure_*_table() helper) or that live in a",
            "# non-Postgres store. Anything else here is a write that raises on",
            "# every host built from the current schema.",
            "# The linter FAILS on any name NOT listed here.",
        ], undeclared)
        _write_list(_DECLARED_LEDGER, [
            "# Every table name the schema declares, frozen. This is arm 3's memory.",
            "# A name that disappears from here without a DROP TABLE in migrations/",
            "# is a retirement that left the table (and its rows) on every EXISTING",
            "# host while becoming invisible to arm 1.",
            "# Retire a table: delete its CREATE, ship migrations/vNNN_*.sql with the",
            "# DROP, THEN regenerate this file.",
        ], declared_tables())
        print(f"baselines written: {len(orphans)} orphan(s), {len(undeclared)} undeclared, "
              f"{len(declared_tables())} declared -> scripts/")
        return 0

    live_path = ""
    if arg == "--live":
        if len(args) < 2:
            print("FAIL  --live needs a file of live table names (one per line)")
            return 2
        live_path = args[1]

    failed = False

    # ── arm 1 (unchanged contract) ──────────────────────────────────────────
    orphans = compute_orphans()
    baseline = load_baseline()
    new_orphans = orphans - baseline
    resolved = baseline - orphans
    if resolved:
        print(f"note: {len(resolved)} baseline orphan(s) now wired (trim the baseline): "
              + ", ".join(sorted(resolved)))
    if new_orphans:
        print("FAIL  new orphan table(s) — declared in schema, no runtime caller:")
        for t in sorted(new_orphans):
            print(f"        - {t}")
        print("      Wire a reader/writer, or if intentional add it to "
              "scripts/orphan_tables_baseline.txt with a reason.")
        failed = True

    # ── arm 2: referenced but never declared ────────────────────────────────
    undeclared = compute_undeclared()
    known_undeclared, only_in = _split_qualified(_load_list(_UNDECLARED_BASELINE))
    new_undeclared = {n: f for n, f in undeclared.items()
                      if n not in known_undeclared
                      and not (n in only_in and set(f) <= only_in[n])}
    if new_undeclared:
        print("FAIL  table(s) named by runtime SQL that NO .sql declares:")
        for t in sorted(new_undeclared):
            print(f"        - {t:32s} {', '.join(sorted(new_undeclared[t])[:2])}")
        print("      Every host built from the current schema will raise on that "
              "statement. Add the CREATE (init-db.sql + a migration), or if the "
              "app creates it at runtime add it to "
              "scripts/undeclared_tables_baseline.txt with a reason.")
        failed = True

    # ── arm 3: a retirement must ship its DROP ──────────────────────────────
    undropped = compute_undropped_retirements()
    if undropped:
        print("FAIL  table(s) removed from the schema with no DROP migration:")
        for t in sorted(undropped):
            print(f"        - {t}")
        print("      Removing a CREATE only affects NEW hosts; every existing host "
              "keeps the table and its rows, and arm 1 can no longer see it. Ship "
              "migrations/vNNN_*.sql with DROP TABLE IF EXISTS <table>, then rerun "
              "--update-baseline.")
        failed = True

    # ── arm 4: live schema (opt-in; never part of a bare invocation) ────────
    if live_path:
        try:
            live = {l.strip() for l in open(live_path, encoding="utf-8") if l.strip()}
        except OSError as e:
            print(f"FAIL  --live: cannot read {live_path}: {e}")
            return 2
        accounted = declared_tables() | _split_qualified(_load_list(_UNDECLARED_BASELINE))[0]
        stray = sorted(live - accounted)
        if stray:
            print(f"FAIL  {len(stray)} live table(s) in {live_path} that no .sql "
                  f"declares and no runtime creator is registered for:")
            for t in stray:
                print(f"        - {t}")
            print("      These exist on a real host and nothing in the tree "
                  "accounts for them. Each is either residue a retirement should "
                  "have dropped, or a runtime-created table missing from "
                  "scripts/undeclared_tables_baseline.txt.")
            failed = True
        else:
            print(f"OK    live schema accounted for ({len(live)} tables in {live_path})")

    if failed:
        # smoke_test.sh:861 reads this gate as `... | tail -1` and asserts the
        # line starts with OK. Without a summary line the LAST line of a failure
        # is an indented remediation hint, so smoke went red (correctly) while
        # printing advice instead of the finding. End on one line that names the
        # counts, so `tail -1` is useful in both directions.
        print(f"FAIL  table hygiene: {len(new_orphans)} new orphan(s), "
              f"{len(new_undeclared)} undeclared reference(s), "
              f"{len(undropped)} retirement(s) missing a DROP"
              + (f", {len(stray)} unaccounted live table(s)" if live_path and stray else ""))
        return 1
    print(f"OK    no new orphan tables ({len(orphans)} known, baseline current); "
          f"{len(undeclared)} known undeclared reference(s); every retirement has its DROP")
    return 0


if __name__ == "__main__":
    sys.exit(main())
