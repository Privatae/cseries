#!/usr/bin/env python3
"""Gate: `recall` finds the Host's own skills — memory_retrieval's skills store.

THE BUG (measured on a benchmark). A Host wrote its own skill
`skills/port` (host_skills, migrations/v207) after a porting job. On the next
job of that kind it called `recall` with a query about the work and never
learned the skill existed: recall's stores were memory, documents, projects,
journal and code, and skills were reachable only through find_tools.

What this ratchets:
  A. recall(query="port this Python library to Go") over every store returns
     the Host's own `port` skill first in a `skills` section, with who wrote
     it and the exact call that opens it, and never the skill's text.
  B. scope='skills' runs the skills store alone; the other stores are not
     called.
  C. a retired skill (no active version) is not returned.
  D. a typed turn whose type narrows memory or projects (its context names
     no skills) does not read the store, and is told so under `narrowed`; a
     closed type neither. A type that narrows only the documents store still
     reads it (the control: the rule is not "every typed turn").
  E. the store is lexical and deterministic (the same query, the same list),
     and the word "skill" in a query does not list every skill; a query that
     is nothing but that word ("skills") lists skills, never "no matches",
     and a skill named `skills` comes first for it.
  F. a source that cannot be read is named, never a silent shorter list; both
     failing makes the store unavailable, and recall says it did NOT run.
  G. the recall schema's scope enum is exactly 'all' + SEARCH_STORES, and its
     description names skills.
  H. a turn that could not open a skill (its type's groups do not hold
     `context`) is not shown it, as find_tools does not show it
     (DoorCatalog.admitted_step); the text counts what was left out. A type
     holding `context`, and an untyped turn, still get `port` first.

The Host's skills come from a stand-in pool that answers host_skills.list_skills'
own query with the rows Postgres would return (one per name: the active version,
else the newest retired one); the platform's are the real default-skills/
folder. The other stores are stubbed: they are not what this tests.

Container-free:
  python3 scripts/test_recall_finds_own_skills.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from turn_source import turn_source  # session.py and the modules split out of it

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
from mastercode.runtime import skill_loader  # noqa: E402
from mastercode.runtime.turn_types import context_filter  # noqa: E402

E = 7
BODY_MARK = "BODY-TEXT-NEVER-IN-RECALL"
PORT_OPEN = 'read(tool=\'context\', args={"action":"read","path":"skills/port"})'


def _row(name, version, status, description, *, author="host", tools=("context", "operate")):
    return {"id": version, "entity_id": E, "name": name, "version": version,
            "body": f"## Steps\n{BODY_MARK} {name}", "description": description,
            "allowed_tools": list(tools), "mcp_server": None, "sources": "[]", "author": author,
            "thread_id": None, "session_id": None, "edited_from": None, "status": status,
            "created_at": None, "superseded_at": None, "versions": version}


# What `SELECT DISTINCT ON (name) ...` returns for this Host: one row per name.
HOST_ROWS = [
    _row("port", 2, "active",
         "Port a codebase to another language: map its modules, translate them one by one, "
         "and run the original tests against the port."),
    _row("translate-docs", 3, "retired",
         "Port the documentation of a library to another language and check every example."),
]


class Pool:
    def __init__(self, rows=None, raise_=None):
        self.rows, self.raise_, self.asked = rows if rows is not None else HOST_ROWS, raise_, []

    async def fetch(self, sql, *args):
        self.asked.append((sql, args))
        if self.raise_:
            raise self.raise_
        if "FROM host_skills" not in sql:
            raise AssertionError(f"unexpected query: {sql[:120]}")
        return [dict(r) for r in self.rows]


calls: dict[str, list] = {}


def _install_stubs(*, platform_dir=ROOT / "default-skills"):
    calls.clear()
    calls.update({"fed": [], "projects": [], "journal": [], "code": []})

    async def fed(pool, entity_id, query, *, scope="all", limit=10, **kw):
        calls["fed"].append(scope)
        out = {}
        if scope in ("all", "memory"):
            out["memory"] = {"hits": [], "arms": ["lexical"]}
        if scope in ("all", "documents"):
            out["documents"] = {"results": [], "arms": ["lexical"]}
        return out

    async def projects(pool, entity_id, query, limit):
        calls["projects"].append(query)
        return {"results": [], "semantic": False}

    async def journal(pool, entity_id, query, limit):
        calls["journal"].append(query)
        return {"results": []}

    async def code(query, limit):
        calls["code"].append(query)
        return {"results": [], "projects": []}

    mr.federated_recall = fed
    mr._search_projects_store = projects
    mr._search_journal_store = journal
    mr._search_code_store = code
    # The platform's skills: the image's real default-skills/ folder (the one
    # entrypoint.sh seeds into skill_loader's location).
    skill_loader.get_location = platform_dir if callable(platform_dir) else (lambda: platform_dir)
    from mastercode.runtime import discovery_index as _di
    _di._file_cache.pop("skills", None)


async def _search(pool, query, **kw):
    try:
        return await mr.unified_search(pool, E, query, **kw), None
    except Exception as e:  # noqa: BLE001 — origin/main refuses the store name
        return None, e


def _skills(r):
    return ((r or {}).get("skills") or {}).get("results") or []


def _door_catalog(turn_type):
    """The turn's DoorCatalog as session.py builds it, over the public
    registry (the construction scripts/test_discovery_index.py uses)."""
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
    return TD.DoorCatalog(caps, read_safe_tools=rst, read_safe_ops=rso, turn_type=turn_type)


async def _run():
    # A + C: every store, the Host's own skill first.
    _install_stubs()
    pool = Pool()
    r, err = await _search(pool, "port this Python library to Go", limit=10)
    hits = _skills(r)
    check("A. scope all searches a `skills` store", r is not None and "skills" in (r.get("stores") or []),
          repr(err or (r or {}).get("stores")))
    check("A. ...which reads THIS Host's own skills (host_skills, its entity id)",
          any("FROM host_skills" in sql and args == (E,) for sql, args in pool.asked), repr(pool.asked[:1]))
    check("A. the Host's own `port` skill is the first skills hit",
          bool(hits) and hits[0].get("name") == "port", repr([h.get("name") for h in hits[:5]]))
    port = hits[0] if hits else {}
    check("A. the hit says who wrote it and the exact call that opens it (find_tools' own `next`)",
          port.get("author") == "host" and port.get("open") == PORT_OPEN, repr(port))
    check("A. the hit carries the skill's one line", str(port.get("description") or "").startswith(
        "Port a codebase to another language"), repr(port.get("description")))
    text = mr.format_unified_text(r) if r else ""
    check("A. recall's text shows the skill, who wrote it and the call that opens it",
          "## Skills" in text and "- port (yours, written by you): Port a codebase" in text
          and PORT_OPEN in text, text[-600:])
    check("A. ...and never the skill's text (the answer stays flat however long a skill is)",
          BODY_MARK not in text and not any("body" in h for h in hits), text[-300:])
    plat, _ = await _search(Pool(), "publish a site from a project", stores=["skills"])
    check("A. platform skills are in the same store, marked as the platform's",
          ((r or {}).get("skills") or {}).get("searched", 0) > 1
          and any(h.get("author") == "platform" for h in _skills(plat)), repr(_skills(plat)[:3]))
    docs, _ = await _search(Pool(), "port the documentation of a library", stores=["skills"])
    check("C. a retired skill (no active version) is not returned, though its words match",
          not any(h.get("name") == "translate-docs" for h in hits + _skills(docs)),
          repr([h.get("name") for h in hits + _skills(docs)]))
    live, _ = await _search(Pool([HOST_ROWS[0], {**HOST_ROWS[1], "status": "active"}]),
                            "port the documentation of a library", stores=["skills"])
    check("C. control: the same row, active, IS returned (the check above is not vacuous)",
          any(h.get("name") == "translate-docs" for h in _skills(live)), repr(_skills(live)[:3]))

    # B: scope='skills' alone.
    _install_stubs()
    r, err = await _search(Pool(), "port this Python library to Go", stores=["skills"], limit=10)
    check("B. scope='skills' runs the skills store alone",
          r is not None and r.get("stores") == ["skills"] and not any(calls.values())
          and not any(k in r for k in ("memory", "documents", "projects", "journal", "code")),
          repr(err or (r, calls)))
    check("B. ...and finds `port` first", bool(_skills(r)) and _skills(r)[0]["name"] == "port",
          repr([h.get("name") for h in _skills(r)[:5]]))
    btext = mr.format_unified_text(r) if r else ""
    check("B. its text holds the skills section alone",
          btext.startswith("## Skills (") and "## Projects" not in btext, btext[:200])

    # D: typed turns.
    for label, tt in (("a type that reads only project 5", {"status": "active", "context": {"projects": [5]}}),
                      ("a type that reads no memory", {"status": "active", "context": {"memory": "none"}}),
                      ("a closed type", {"status": "closed", "context": {}})):
        _install_stubs()
        r, err = await _search(Pool(), "port this Python library to Go", context=context_filter(tt))
        check(f"D. {label}: the skills store does not run and `narrowed` names it",
              r is not None and "skills" not in r and "skills" not in r.get("stores", [])
              and any(n.startswith("skills") for n in r.get("narrowed") or []), repr(err or r))
    _install_stubs()
    r, err = await _search(Pool(), "port this Python library to Go",
                           context=context_filter({"status": "active", "context": {"documents": False}}))
    check("D. control: a type that narrows only the documents store still reads skills",
          r is not None and bool(_skills(r)) and _skills(r)[0]["name"] == "port", repr(err or r))

    # E: deterministic; the kind word lists nothing extra.
    _install_stubs()
    a = _skills((await _search(Pool(), "port this Python library to Go", stores=["skills"]))[0])
    b = _skills((await _search(Pool(), "port this Python library to Go", stores=["skills"]))[0])
    check("E. the same query gives the same list", bool(a) and a == b)
    plain = [h["name"] for h in _skills((await _search(Pool(), "port", stores=["skills"]))[0])]
    worded = [h["name"] for h in _skills((await _search(Pool(), "my port skills", stores=["skills"]))[0])]
    check("E. 'skill'/'skills' in the query adds no hits (in this store it names every entry)",
          bool(plain) and worded == plain, repr((plain, worded)))
    # A query that is NOTHING but the kind word asks for the skills: taken out,
    # it left no word to search and the store said "no matches" over every
    # skill the Host has.
    for q in ("skills", "what skills do I have"):
        r, err = await _search(Pool(), q, stores=["skills"], limit=5)
        got = _skills(r)
        sec = (r or {}).get("skills") or {}
        etext = mr.format_unified_text(r) if r else ""
        check(f"E. {q!r} alone lists skills (a full page), never 'no matches'",
              len(got) == 5 and sec.get("searched", 0) > 5 and "no matches" not in etext,
              repr(err or ([h.get("name") for h in got], sec.get("searched"), etext[:200])))
    named = [h["name"] for h in _skills((await _search(
        Pool([*HOST_ROWS, _row("skills", 1, "active", "What I keep after a job and how I write it down.")]),
        "skills", stores=["skills"]))[0])]
    check("E. a skill literally named `skills` is the first hit for the query 'skills'",
          bool(named) and named[0] == "skills", repr(named[:5]))

    # F: honest about a source that cannot be read.
    _install_stubs()
    r, err = await _search(Pool(raise_=RuntimeError("host_skills unreadable (gate)")),
                           "publish a site", stores=["skills"])
    sec = (r or {}).get("skills") or {}
    check("F. the Host's own skills unreadable: named under `failed`, the platform's still answer",
          bool(sec.get("failed")) and "own: host_skills unreadable (gate)" in sec["failed"][0]
          and bool(sec.get("results")) and "skills" not in r.get("unavailable", []), repr(err or sec))
    check("F. ...and recall's text says what was not searched",
          "Not searched: own: host_skills unreadable (gate)" in (mr.format_unified_text(r) if r else ""))

    def _no_folder():
        raise OSError("skills folder unreadable (gate)")
    _install_stubs(platform_dir=_no_folder)
    r, err = await _search(Pool(raise_=RuntimeError("host_skills unreadable (gate)")),
                           "publish a site", stores=["skills"])
    ftext = mr.format_unified_text(r) if r else ""
    check("F. both sources unreadable: the store is unavailable and recall says it did NOT run",
          r is not None and r.get("unavailable") == ["skills"] and "## Skills — UNAVAILABLE" in ftext
          and "did NOT run" in ftext, repr(err or r))

    # H: a turn that cannot open a skill is not shown it — find_tools' own
    # wall (tool_doors.DoorCatalog.admitted_step, the step add_discovery keeps
    # an entry for). An empty `context` narrows nothing (context_filter is
    # None), so before this the store listed every skill with an `open:` the
    # turn's tool groups refuse.
    def _tt(groups):
        return {"id": "t-h", "name": "Web only", "status": "active", "tool_groups": groups,
                "grant_names": [], "keys": [], "bastions": [], "context": {}}
    for label, tt, want_port in (("a type whose groups do not hold `context`", _tt(["research"]), False),
                                 ("control: the same type holding `memory` (context's group)",
                                  _tt(["research", "memory"]), True),
                                 ("control: an untyped turn's catalog", None, True)):
        _install_stubs()
        cat = _door_catalog(tt)
        pick = getattr(cat, "admitted_step", None)
        r, err = await _search(Pool(), "port this Python library to Go", stores=["skills"],
                               context=context_filter(tt), skill_step=pick) if pick else (None, "no admitted_step")
        sec = (r or {}).get("skills") or {}
        htext = mr.format_unified_text(r) if r else ""
        if want_port:
            check(f"H. {label}: `port` is first, its open the call the turn may make",
                  bool(_skills(r)) and _skills(r)[0]["name"] == "port" and _skills(r)[0]["open"] == PORT_OPEN
                  and not sec.get("left_out"), repr(err or sec))
        else:
            check(f"H. {label}: no skill is shown, none with an `open:` it would be refused",
                  r is not None and not _skills(r) and "read(tool='context'" not in htext, repr(err or sec))
            check(f"H. {label}: ...and the text says how many were left out, never 'no matches'",
                  int(sec.get("left_out") or 0) > 1 and sec.get("searched") == 0
                  and "none this turn can open" in htext and "Left out:" in htext and "no matches" not in htext,
                  repr(htext[:300]))
    _src = turn_source(ROOT)
    _recall_src = _src.split('elif func_name == "recall":', 1)[-1].split("elif func_name ==", 1)[0]
    check("H. the recall handler hands unified_search its turn's catalog (skill_step=_door_catalog.admitted_step)",
          "skill_step=(_door_catalog.admitted_step" in _recall_src)

    # Blank query: the store's empty shape, no source read.
    _install_stubs()
    p = Pool()
    r, err = await _search(p, "   ", stores=["skills"])
    check("a blank query answers the skills store empty and reads nothing",
          r is not None and r.get("skills") == {"results": []} and not p.asked, repr(err or r))


asyncio.run(_run())

# G: the recall schema names the store.
from mastercode.runtime.session import _build_host_orchestration_tools  # noqa: E402

_recall = next(t for t in _build_host_orchestration_tools() if t["function"]["name"] == "recall")
_scope = _recall["function"]["parameters"]["properties"]["scope"]
check("G. recall's scope enum is exactly 'all' + SEARCH_STORES (a store recall searches can be asked for alone)",
      _scope.get("enum") == ["all", *mr.SEARCH_STORES], repr(_scope.get("enum")))
check("G. recall's description names skills", "skills" in _recall["function"]["description"],
      _recall["function"]["description"])

print("\nPASS recall_finds_own_skills" if not fails else f"\nFAIL recall_finds_own_skills: {fails} check(s)")
sys.exit(1 if fails else 0)
