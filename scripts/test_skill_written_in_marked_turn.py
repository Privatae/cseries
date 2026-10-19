#!/usr/bin/env python3
"""Gate: a Host skill written in a turn that read untrusted content carries
that mark, and every turn that reads it is marked the same way.

THE FINDING. A Host turn that
reads content from outside (the web, a foreign repository, an MCP tool's
output, another Host's message, an agent's report, an upload) is marked for
the rest of the turn (runtime/turn_taint). A memory row, a turn row or a task
written while marked carries `untrusted`, and a later turn that reads it back
is marked too (turn_taint.stamp / mark_rows). A Host SKILL had no such mark:
host_skills recorded nothing on a version written in a marked turn, and no
path that reads a skill marked its turn. So instructions planted in a web page
could be written into a skill and reach every later turn unmarked, against the
decision that Hosts write their own skills only where malicious instructions
cannot get through.

What this ratchets (each FAILS on 0eee36b):
  W. host_skills.write by the Host in a marked turn stores the mark
     (host_skills.untrusted, migrations/v232) and says so; in a clean turn it
     stores none. The context tool's write is the same write.
  P. the person's save in the Skills app (author 'person') stores no mark,
     even of the same text as the marked active version (that is how the
     person clears it); the Host writing the same text again does not.
     A restore carries the restored version's mark.
  R. each read path marks the reading turn, and a clean skill leaves it clean:
       R1 recall's skills store (unified_search), whose line names the mark;
       R2 a context read of skills/<name>;
       R3 a turn type riding the skill (load_type_skill);
       R4 find_tools finding it (DoorCatalog over discovery_index entries);
       R5 list_skills and a context list naming it;
       R6 a context write whose diff carries a marked version's lines.
  O. the per-turn offer of the Host's own skills (turn_own_skills) never
     offers a marked skill, and offering nothing leaves the turn clean; the
     same skill unmarked is offered (the control).
  M. migrations/v232 adds the column idempotently; host_skills selects it.

Stand-in pools answer host_skills' own SQL from an in-memory table; the
platform's skills are the real default-skills/ folder. Container-free:
  python3 scripts/test_skill_written_in_marked_turn.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
for p in (str(ROOT), str(ROOT / "brain"), str(ROOT / "server")):
    if p not in sys.path:
        sys.path.insert(0, p)

fails = 0


def check(name, ok, detail=""):
    global fails
    print(("  ok    " if ok else "  FAIL  ") + name + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        fails += 1


from mastercode import memory_retrieval as mr  # noqa: E402
from mastercode.runtime import context_ops, host_skills as hs, skill_loader, turn_taint  # noqa: E402

E = 7
TOOLS = frozenset({"context", "operate", "get_project", "manage_site", "recall", "workspace"})
skill_loader.get_location = lambda: ROOT / "default-skills"
hs.registry_tool_names = lambda entity_id=None: TOOLS


async def _no_mcp(pool, entity_id):
    return {}
hs.active_mcp_servers = _no_mcp


def skill(name="port", step="Translate each module with `operate`, then run the tests."):
    return f"""---
name: {name}
description: Port a codebase to another language, module by module, and run its tests.
allowed-tools: context, operate
sources: conversation
---

## When to use

The person asks to port a library to another language.

## Steps

1. Read the project with `context` to map its modules.
2. {step}

## Checks

- The original tests pass against the port.

## When it fails

- If a module has no counterpart, ask the person which library to use.
"""


class Table:
    """host_skills in memory, answering the statements host_skills.py runs."""

    def __init__(self):
        self.rows: list[dict] = []

    def _named(self, name):
        return [r for r in self.rows if r["name"] == name]

    async def fetchval(self, sql, *a):
        if "pg_advisory_xact_lock" in sql:
            return None
        if sql.lstrip().startswith("SELECT version FROM host_skills"):
            act = [r for r in self._named(a[1]) if r["status"] == "active"]
            return act[0]["version"] if act else None
        raise AssertionError(f"unexpected fetchval: {sql[:120]}")

    async def fetchrow(self, sql, *a):
        s = " ".join(sql.split())
        if s.startswith("INSERT INTO host_skills"):
            cols = s[s.index("(") + 1:s.index(")")].replace(" ", "").split(",")
            row = dict(zip([c for c in cols if c != "status"], a))
            row["status"] = "active"
            row.setdefault("untrusted", None)
            row.update({"id": len(self.rows) + 1, "created_at": None, "superseded_at": None})
            self.rows.append(row)
            return {"id": row["id"]}
        if "AND version = $3" in s:
            hit = [r for r in self._named(a[1]) if r["version"] == a[2]]
            return dict(hit[0]) if hit else None
        if "AND version < $3" in s:
            hit = sorted([r for r in self._named(a[1]) if r["version"] < a[2]], key=lambda r: -r["version"])
            return dict(hit[0]) if hit else None
        if "status = 'active'" in s and "ORDER BY" not in s:
            hit = [r for r in self._named(a[1]) if r["status"] == "active"]
            return dict(hit[0]) if hit else None
        if "ORDER BY (status = 'active') DESC" in s:
            hit = sorted(self._named(a[1]), key=lambda r: (r["status"] == "active", r["version"]), reverse=True)
            return dict(hit[0]) if hit else None
        if "ORDER BY version DESC LIMIT 1" in s:
            hit = sorted(self._named(a[1]), key=lambda r: r["version"], reverse=True)
            return dict(hit[0]) if hit else None
        raise AssertionError(f"unexpected fetchrow: {s[:140]}")

    async def fetch(self, sql, *a):
        s = " ".join(sql.split())
        if "DISTINCT ON (name)" in s:
            out = []
            for name in sorted({r["name"] for r in self.rows}):
                rs = sorted(self._named(name), key=lambda r: (r["status"] == "active", r["version"]),
                            reverse=True)
                out.append({**dict(rs[0]), "versions": len(rs)})
            return out
        if "FROM mcp_connections" in s or "FROM turn_types" in s or "FROM entity_context" in s:
            return []
        raise AssertionError(f"unexpected fetch: {s[:140]}")

    async def execute(self, sql, *a):
        s = " ".join(sql.split())
        if s.startswith("UPDATE host_skills SET status = 'retired'"):
            for r in self.rows:
                if r["id"] == a[0]:
                    r["status"] = "retired"
            return "UPDATE 1"
        raise AssertionError(f"unexpected execute: {s[:140]}")

    def acquire(self):
        table = self

        class _Tx:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *exc):
                return False

        class _Conn:
            fetchval = staticmethod(table.fetchval)
            fetchrow = staticmethod(table.fetchrow)
            fetch = staticmethod(table.fetch)
            execute = staticmethod(table.execute)

            def transaction(self):
                return _Tx()

        class _Acq:
            async def __aenter__(self):
                return _Conn()

            async def __aexit__(self, *exc):
                return False

        return _Acq()


def in_turn(coro_fn, *, marked: str | None = None):
    """Run coro_fn() inside a Host turn (turn_taint.begin_turn), marked by
    `marked` first when given. Returns (result, the turn's first source)."""
    async def go():
        tok = turn_taint.begin_turn()
        try:
            if marked:
                turn_taint.mark(marked, "https://evil.example/page")
            out = await coro_fn()
            st = turn_taint.current()
            return out, (st.source if st else None)
        finally:
            turn_taint.end_turn(tok)
    return asyncio.run(go())


def stored(t: Table, name="port"):
    act = [r for r in t.rows if r["name"] == name and r["status"] == "active"]
    return act[0] if act else None


def marked_table(name="port") -> Table:
    t = Table()
    in_turn(lambda: hs.write(t, E, name, skill(name), author="host"), marked=turn_taint.WEB)
    return t


def clean_table(name="port") -> Table:
    t = Table()
    in_turn(lambda: hs.write(t, E, name, skill(name), author="host"))
    return t


def _door_catalog():
    """The turn's DoorCatalog as session.py builds it, over the public
    registry (scripts/test_recall_finds_own_skills.py's construction)."""
    from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS as P_REG
    from mastercode.runtime import tool_doors as TD
    from mastercode.runtime.tool_access import read_safe_ops
    from mastercode.runtime.tool_lanes import lane_for
    caps = [{"type": "function", "function": P_REG[n]["function"]} for n in P_REG
            if not n.startswith("_meta_") and lane_for(n, P_REG[n]) != "retired"]
    rst = {s["function"]["name"] for s in caps if lane_for(s["function"]["name"], s) == "read"}
    rso = {}
    for s in caps:
        n = s["function"]["name"]
        if n not in rst and read_safe_ops(n, s):
            rso[n] = read_safe_ops(n, s)
    return TD.DoorCatalog(caps, read_safe_tools=rst, read_safe_ops=rso, turn_type=None)


def main():
    # ── M: the migration and the column ──
    mig = ROOT / "migrations" / "v232_host_skills_untrusted.sql"
    txt = mig.read_text() if mig.is_file() else ""
    check("M migrations/v232 adds host_skills.untrusted idempotently",
          "ALTER TABLE host_skills ADD COLUMN IF NOT EXISTS untrusted TEXT" in txt, str(mig))
    check("M host_skills reads the column with every version", "untrusted" in hs._COLS, hs._COLS)

    # ── W: the Host's write records the mark of its turn ──
    t = Table()
    r, _ = in_turn(lambda: hs.write(t, E, "port", skill(), author="host"), marked=turn_taint.WEB)
    row = stored(t)
    check("W a Host write in a marked turn stores what marked it",
          row is not None and row.get("untrusted") == turn_taint.WEB, str(row and row.get("untrusted")))
    check("W ... and the write's answer says so", r.get("untrusted") == turn_taint.WEB, str(r)[:300])
    t2 = clean_table()
    check("W a Host write in a clean turn stores no mark",
          stored(t2) is not None and not stored(t2).get("untrusted"), str(stored(t2)))
    t3 = Table()
    r3, _ = in_turn(lambda: context_ops.run_context_op(
        t3, E, {"action": "write", "path": "skills/port", "content": skill()}), marked=turn_taint.MCP)
    check("W the context tool's write is the same write: the mark is stored",
          r3.get("status") == "ok" and (stored(t3) or {}).get("untrusted") == turn_taint.MCP,
          f"{r3} {stored(t3)}")
    check("W ... and the Host is told what the mark does", "untrusted" in r3, str(r3)[:300])
    # A turn type naming the skill rides it every turn, unasked (R3 below):
    # what the Host and the person are told must not say otherwise.
    told = str(r3.get("untrusted") or "")
    check("W ... including that a turn type riding it is marked, never 'not offered unasked'",
          "turn type" in told and "unasked" not in told, told)
    app_js = (ROOT / "system-apps" / "skills" / "app.js").read_text(encoding="utf-8")
    check("W the Skills app tells the person a turn type using it is marked too, never 'not offered unasked'",
          "every turn of a turn type that uses it" in app_js and "offered to a turn unasked" not in app_js, "")

    # ── P: the person's save clears it; a restore carries it ──
    t = marked_table()
    rp, _ = in_turn(lambda: hs.write(t, E, "port", skill(), author="person"), marked=turn_taint.WEB)
    check("P the person's save of the same text is a new version with no mark",
          not rp.get("unchanged") and rp.get("version") == 2 and not (stored(t) or {}).get("untrusted"),
          f"{rp} {stored(t)}")
    t = marked_table()
    rh, _ = in_turn(lambda: hs.write(t, E, "port", skill(), author="host"))
    check("P the Host writing the same text again in a clean turn writes nothing and clears nothing",
          rh.get("unchanged") and (stored(t) or {}).get("untrusted") == turn_taint.WEB, f"{rh} {stored(t)}")
    t = marked_table()
    in_turn(lambda: hs.write(t, E, "port", skill(step="Port it by hand, then test it."), author="person"))
    rr, _ = in_turn(lambda: hs.restore(t, E, "port", 1, author="person"))
    check("P a restore of a marked version carries its mark",
          rr.get("status") == "ok" and (stored(t) or {}).get("untrusted") == turn_taint.WEB
          and stored(t)["version"] == 3, f"{rr} {stored(t)}")

    # ── R1: recall's skills store ──
    async def recall(t):
        return await mr.unified_search(t, E, "port this library to another language", stores=["skills"])
    _t = marked_table()
    out, src = in_turn(lambda: recall(_t))
    text = mr.format_unified_text(out)
    check("R1 recall returning a marked skill marks the turn", src == turn_taint.CARRIED, str(src))
    check("R1 ... and its line says what marked it",
          "- port (" in text and "untrusted content" in text, text[:400])
    _t = clean_table()
    _, src = in_turn(lambda: recall(_t))
    check("R1 recall returning a clean skill leaves the turn clean", src is None, str(src))

    # ── R2: a context read of skills/<name> ──
    def read(t):
        return context_ops.run_context_op(t, E, {"action": "read", "path": "skills/port"})
    _t = marked_table()
    rd, src = in_turn(lambda: read(_t))
    check("R2 a context read of a marked skill marks the turn",
          rd.get("status") == "ok" and src == turn_taint.CARRIED, f"{src} {str(rd)[:200]}")
    check("R2 ... and the answer names the mark", "untrusted" in rd, str(rd)[:300])
    _t = clean_table()
    _, src = in_turn(lambda: read(_t))
    check("R2 a context read of a clean skill leaves the turn clean", src is None, str(src))

    # ── R3: a turn type riding the skill ──
    _t = marked_table()
    ld, src = in_turn(lambda: hs.load_type_skill(_t, E, "port"))
    check("R3 a turn type riding a marked skill marks the turn",
          bool((ld or {}).get("body")) and src == turn_taint.CARRIED, f"{src} {ld}")
    _t = clean_table()
    ld, src = in_turn(lambda: hs.load_type_skill(_t, E, "port"))
    check("R3 riding a clean skill leaves the turn clean",
          bool((ld or {}).get("body")) and src is None, f"{src} {ld}")

    # ── R4: find_tools ──
    async def find(t):
        from mastercode.runtime import discovery_index as di
        cat = _door_catalog()
        cat.add_discovery(await di._skills(t, E))
        return cat.find({"query": "port a codebase to another language"})
    _t = marked_table()
    fr, src = in_turn(lambda: find(_t))
    names = [x.get("name") for x in (fr.get("result") or {}).get("tools") or []]
    check("R4 find_tools returning a marked skill marks the turn",
          "skill:port" in names and src == turn_taint.CARRIED, f"{src} {names}")
    _t = clean_table()
    fr, src = in_turn(lambda: find(_t))
    names = [x.get("name") for x in (fr.get("result") or {}).get("tools") or []]
    check("R4 find_tools returning a clean skill leaves the turn clean",
          "skill:port" in names and src is None, f"{src} {names}")

    # ── R5: list_skills and a context list ──
    from mastercode.runtime.tools import _exec_list_skills
    _t = marked_table()
    ls, src = in_turn(lambda: _exec_list_skills({}, entity_id=E, pool=_t))
    check("R5 list_skills listing a marked skill marks the turn",
          ls.get("status") == "ok" and src == turn_taint.CARRIED, f"{src} {str(ls)[:200]}")
    _t = clean_table()
    _, src = in_turn(lambda: _exec_list_skills({}, entity_id=E, pool=_t))
    check("R5 list_skills listing only clean skills leaves the turn clean", src is None, str(src))
    _t = marked_table()
    cl, src = in_turn(lambda: context_ops.run_context_op(_t, E,
                                                         {"action": "list", "prefix": "skills/"}))
    check("R5 a context list naming a marked skill marks the turn", src == turn_taint.CARRIED,
          f"{src} {str(cl)[:200]}")

    # ── R6: a write whose diff carries a marked version's lines ──
    t = marked_table()
    wr, src = in_turn(lambda: context_ops.run_context_op(
        t, E, {"action": "write", "path": "skills/port",
               "content": skill(step="Port it by hand, then test it.")}))
    check("R6 a clean write stores a clean version ...",
          stored(t) is not None and stored(t)["version"] == 2 and not stored(t).get("untrusted"),
          str(stored(t)))
    check("R6 ... and its diff against the marked version marks the turn",
          bool(wr.get("diff")) and src == turn_taint.CARRIED, f"{src} {str(wr)[:200]}")

    # ── O: the per-turn offer never carries a marked skill ──
    msg = "Port the Python library markdown-it-py to Go with the port skill."
    _t = marked_table()
    hits, src = in_turn(lambda: mr.turn_own_skills(_t, E, msg))
    check("O the per-turn offer never offers a marked skill", hits == [], str(hits))
    check("O ... and offering nothing leaves the turn clean", src is None, str(src))
    _t = clean_table()
    hits, src = in_turn(lambda: mr.turn_own_skills(_t, E, msg))
    check("O the same skill unmarked is offered (control)",
          [h.get("name") for h in hits] == ["port"] and src is None, f"{hits} {src}")

    print(f"\n{'FAILED' if fails else 'PASSED'}: {fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
