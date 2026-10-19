#!/usr/bin/env python3
"""The saved request's state machine is pinned to the code.

docs/DESIGN-saved-request-state-machine-2026-10-01.md maps every state a saved
request (an identity-hold wake: a calendar_events row with payload.saved_request)
can hold and every transition between them, one row (T01...) per code site. Three
rounds of review found the same hazard on one path at a time — a saved request
that may have acted going back to scheduled and running again — because nothing
listed the paths. This gate makes the list the source of truth:

  1. every statement in server/ and brain/ that writes calendar_events (UPDATE,
     INSERT, DELETE) is in the table, by file::function. A new statement in a new
     function fails here until it has a row;
  2. every code site in the table still exists;
  3. every row has a scenario (SM_IDS in scripts/test_tools_before_first_turn.py)
     that starts a saved request in that state and checks the invariant — and the
     ids match both ways.

Container-free: reads source only.
"""
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "DESIGN-saved-request-state-machine-2026-10-01.md"
FAILS = []


def check(name, cond, extra=""):
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"[state-map] FAIL {name} {extra}")


WRITE = re.compile(r"\b(UPDATE|INSERT\s+INTO|DELETE\s+FROM)\s+calendar_events\b", re.I)
# A write whose table name is built at run time: `UPDATE {} SET ...`, `DELETE FROM {}`.
COMPUTED = re.compile(r"\b(UPDATE|INSERT\s+INTO|DELETE\s+FROM)\s+(\{\}|%s)", re.I)
# The whole repo except tests, vendored code and built output. community-web/node_modules
# is tracked in git, hence the explicit list.
SKIP_DIRS = {"node_modules", ".git", "tests", "dist", "native-bundles", "factory-bundles", "system-apps",
             "community-web", "circles-web", "workbench-web", "sites-web", "__pycache__"}


def _template(n):
    if isinstance(n, ast.Constant) and isinstance(n.value, str):
        return n.value
    if isinstance(n, ast.JoinedStr):
        return "".join(str(v.value) if isinstance(v, ast.Constant) else "{}" for v in n.values)
    return None


def repo_files():
    for p in sorted(ROOT.rglob("*.py")):
        rel = p.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in rel.parts) or p.name.startswith("test_"):
            continue
        yield p


def code_sites():
    """((file, function) of every string that writes calendar_events,
    (file, function) of every write whose table name is computed)."""
    literal, computed = set(), set()
    for p in repo_files():
        try:
            tree = ast.parse(p.read_text())
        except Exception:
            continue
        parents = {}
        for n in ast.walk(tree):
            for c in ast.iter_child_nodes(n):
                parents[c] = n
        for n in ast.walk(tree):
            t = _template(n) if isinstance(n, (ast.Constant, ast.JoinedStr)) else None
            if not t:
                continue
            hit_lit, hit_comp = WRITE.search(t), COMPUTED.search(t)
            if not (hit_lit or hit_comp):
                continue
            f, name = n, "<module>"
            while f in parents:
                f = parents[f]
                if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    name = f.name
                    break
            key = (str(p.relative_to(ROOT)), name)
            if hit_lit:
                literal.add(key)
            if hit_comp:
                computed.add(key)
    return literal, computed


def sql_writers():
    """.sql files (migrations, init-db.sql) that write calendar_events."""
    out = set()
    for p in list(sorted((ROOT / "migrations").glob("*.sql"))) + [ROOT / "init-db.sql"]:
        if p.exists() and WRITE.search(p.read_text()):
            out.add(str(p.relative_to(ROOT)))
    return out


def doc_rows():
    rows = {}
    for line in DOC.read_text().splitlines():
        m = re.match(r"\|\s*(T\d\d)\s*\|", line)
        if not m:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        rows[m.group(1)] = cells
    return rows


rows = doc_rows()
check("the map has rows, five columns each (id, from→to, code site, trigger, outcome)",
      len(rows) >= 20 and all(len(c) == 5 for c in rows.values()), repr({k: len(v) for k, v in rows.items()}))

doc_sites = set()
for tid, cells in rows.items():
    for f, fn in re.findall(r"`([\w/\.]+\.py)::(\w+)`", cells[2]):
        doc_sites.add((f, fn))
        check(f"{tid}: the code site {f}::{fn} exists",
              (ROOT / f).exists() and re.search(rf"(async\s+)?def {fn}\b", (ROOT / f).read_text()) is not None)

lit, comp = code_sites()
# A computed-table writer that can reach calendar_events is in the main table (T23); the rest
# are listed, with why they cannot, in the second table.
second = set()
for line in DOC.read_text().splitlines():
    m = re.match(r"\|\s*`([\w/\.]+\.py)::(\w+)`\s*\|\s*(yes|no)", line)
    if m:
        second.add((m.group(1), m.group(2)))
unmapped = sorted(lit - doc_sites)
check("every statement that writes calendar_events, anywhere in the repo but its tests, is in the map "
      "(a new one needs a row)", not unmapped, "not in the map: " + repr(unmapped))
unlisted = sorted(comp - doc_sites - second)
check("every write with a COMPUTED table name is listed, with whether it can reach calendar_events",
      not unlisted, "computed-table writers not in the doc: " + repr(unlisted))
stale = sorted(doc_sites - lit - comp)
check("every code site in the map still writes (a table that can be calendar_events)", not stale,
      "no longer a writer: " + repr(stale))
stale2 = sorted(second - comp)
check("every computed-table writer in the doc's second table still exists", not stale2, repr(stale2))
sql_doc = set(re.findall(r"`(migrations/[\w\.]+\.sql|init-db\.sql)`", DOC.read_text()))
check("every migration or schema file that writes calendar_events rows is in the map",
      sql_writers() <= sql_doc, "not in the map: " + repr(sorted(sql_writers() - sql_doc)))

test_src = (ROOT / "scripts" / "test_tools_before_first_turn.py").read_text()
m = re.search(r"SM_IDS = tuple\(f\"T\{i:02d\}\" for i in range\(1, (\d+)\)\)", test_src)
scenario_ids = {f"T{i:02d}" for i in range(1, int(m.group(1)))} if m else set()
check("the scenario list is readable", bool(scenario_ids))
check("every row in the map has a scenario, and every scenario a row",
      scenario_ids == set(rows), f"rows-only: {sorted(set(rows) - scenario_ids)}, scenarios-only: {sorted(scenario_ids - set(rows))}")
for tid in sorted(rows):
    check(f"{tid}: a scenario runs it (sm(\"{tid}\", ...) in case_state_machine)",
          f'sm("{tid}"' in test_src)

check("the real-database gate exists and the DB lane (smoke_test.sh) runs it",
      (ROOT / "scripts" / "test_saved_request_sql_real_db.py").exists()
      and "test_saved_request_sql_real_db.py" in (ROOT / "scripts" / "smoke_test.sh").read_text())

text = DOC.read_text()
check("the invariant is stated in the map",
      "never scheduled again" in text and "crash_unknown" in text and "held_for_person" in text)

if FAILS:
    print(f"\n[state-map] {len(FAILS)} FAILED")
    sys.exit(1)
print("\nsaved request state map: all passed")
