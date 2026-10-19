#!/usr/bin/env python3
"""find_tools as the one discovery door — runtime/discovery_index.py and the
DoorCatalog parts that carry it (add_discovery, add_core, the compact render,
find_tools(name=...), compact_schema).

What it has to guarantee:
  - a skill, an app, a capability card, a turn type and a harness are each
    found by a plain-words query, with the call that uses it (`next`);
  - an entry that is not a tool is never callable: read_schema_for and
    operate_refusal answer None for it, read_refusal refuses it;
  - its `next` is a call this turn may make (the door, the turn type, the
    validated gate): a read-only route gets describe_app, not open_app, and
    no store app;
  - a query that names a tool still returns that tool first;
  - a hit is compact (no descriptions, no schema); find_tools(name=X) gives
    X in full; the core list's own tools are findable by name, never read or
    operated;
  - one source that fails gives [] and the others still arrive.

Container-free, no pod, no DB:
  PYTHONPATH=brain:. python3 scripts/test_discovery_index.py
"""
import asyncio
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.environ.get("CSERIES_BRAIN", str(REPO / "brain")))
sys.path.insert(0, str(REPO))

from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS as P_REG  # noqa: E402
from mastercode.runtime import discovery_index as D  # noqa: E402
from mastercode.runtime import tool_doors as TD  # noqa: E402
from mastercode.runtime.tool_access import read_safe_ops  # noqa: E402
from mastercode.runtime.tool_lanes import lane_for  # noqa: E402

FAILS: list[str] = []


def check(what: str, cond, detail=None) -> None:
    print(("PASS " if cond else "FAIL ") + what + ("" if cond or detail is None else f"  {detail!r}"[:400]))
    if not cond:
        FAILS.append(what)


def _wire(n):
    return {"type": "function", "function": P_REG[n]["function"]}


CAPS = [_wire(n) for n in P_REG if not n.startswith("_meta_") and lane_for(n, P_REG[n]) != "retired"]
RST = {s["function"]["name"] for s in CAPS if lane_for(s["function"]["name"], s) == "read"}
RSO = {}
for _s in CAPS:
    _n = _s["function"]["name"]
    if _n not in RST and read_safe_ops(_n, _s):
        RSO[_n] = read_safe_ops(_n, _s)


def entry(kind, title, line, steps, words="", detail=None):
    return D._entry(kind, title, line, steps, words=words, detail=detail)


ENTRIES = [
    entry("skill", "desktop", "How to design the user's whole desktop as one HTML document.",
          [{"via": "context", "args": {"action": "read", "path": "default-skills/desktop"}, "verb": "read"}],
          words="wallpaper layout phone layout tokens", detail={"skill": "desktop",
                                                                "path": "default-skills/desktop"}),
    entry("app", "notes", "Notes (installed): write and keep notes.",
          [{"via": "open_app", "args": {"app_id": "notes"}, "verb": "operate"},
           {"via": "describe_app", "args": {"app_id": "notes"}, "verb": "read"}], words="notes notebook"),
    entry("app", "gridcraft", "Gridcraft (App Store, not installed): spreadsheets.",
          [{"via": "install_app", "args": {"app_id": "gridcraft"}, "verb": "operate"}],
          words="spreadsheet table cells"),
    entry("capability", "page:voice", "Voice: talk to your Host out loud.",
          [{"via": "capabilities", "args": {"op": "card", "id": "page:voice"}, "verb": "read"}],
          words="can I talk to my host by voice speak microphone"),
    entry("turn_type", "Gardening", "Turn type Gardening: tool groups research",
          [{"via": "turn_type", "args": {"op": "show", "type_id": "t-g"}, "verb": "read"}],
          words="garden plants tomatoes"),
    entry("harness", "codex", "Coding harness codex, connected: build and compare runs can use it.",
          [{"via": "build", "args": {"action": "run", "harness": "codex"}, "verb": "direct"}],
          words="coding engine build run compare codex"),
    entry("capability", "machine-runtime", "What this machine can run.",
          [{"via": "capability:machine-runtime", "args": {}, "verb": D.FIND}],
          words="machine runtime process static", detail={"text": "## What this machine can run\n..."}),
]


def catalog(**kw):
    cat = TD.DoorCatalog(CAPS, read_safe_tools=RST, read_safe_ops=RSO, **kw)
    cat.add_discovery(ENTRIES)
    return cat


CAT = catalog()

# ── found by plain words, with the call that uses it ────────────────────────
for q, want, nxt in (
        ("how do I design the desktop wallpaper", "skill:desktop",
         "read(tool='context', args={\"action\":\"read\",\"path\":\"default-skills/desktop\"})"),
        ("open my notes", "app:notes", "operate(intent, tool='open_app', args={\"app_id\":\"notes\"})"),
        ("a spreadsheet app", "app:gridcraft", "operate(intent, tool='install_app', args={\"app_id\":\"gridcraft\"})"),
        ("can I talk to my host by voice", "capability:page:voice",
         "read(tool='capabilities', args={\"op\":\"card\",\"id\":\"page:voice\"})"),
        ("gardening tomatoes turn", "turn_type:Gardening",
         "read(tool='turn_type', args={\"op\":\"show\",\"type_id\":\"t-g\"})"),
        ("which coding harness: codex", "harness:codex", 'build(action="run", harness="codex")'),
        ("what can this machine run", "capability:machine-runtime",
         "find_tools(name='capability:machine-runtime')")):
    hits = CAT.find({"query": q, "limit": 5})["result"]["tools"]
    hit = next((h for h in hits if h["name"] == want), None)
    check(f"found by plain words: {want!r} for {q!r}, with its next call",
          hit is not None and hit["next"] == nxt and hit["kind"] == want.split(":")[0]
          and set(hit) == {"name", "kind", "purpose", "next"}, hits)

# ── a tool the query names still comes first ────────────────────────────────
_miss = [n for n in CAT.searchable if CAT.entries[n].kind == "tool"
         and [x for x, _ in CAT.index().search(n, limit=1)] != [n]]
check("a tool's own name still finds that tool first, with every other entry indexed", not _miss, _miss[:5])
check("a query that is exactly an entry's own name finds it",
      CAT.find({"query": "desktop"})["result"]["tools"][0]["name"] == "skill:desktop")

# ── never callable ──────────────────────────────────────────────────────────
_rk = dict(read_safe_tools=RST, read_safe_ops=RSO, host_own_tools={"build"})
for key in ("skill:desktop", "app:notes", "capability:page:voice", "turn_type:Gardening", "harness:codex"):
    check(f"{key}: read_schema_for and operate_refusal are None (never a read or operate target)",
          CAT.read_schema_for(key) is None and CAT.operate_refusal(key, {}) is None)
    r = TD.read_refusal({"tool": key, "args": {}}, CAT, **_rk)
    check(f"{key}: the read door refuses it, as an unknown name, with the call that uses it",
          bool(r) and r["reason"] == "unknown_name" and CAT.entries[key].next in r["result"], r)
check("an entry is never in the catalog's schemas (operate's walls find no tool)",
      not [k for k in CAT.entries if CAT.entries[k].kind != "tool" and k in CAT.schemas])

# ── the turn's walls decide the next step ───────────────────────────────────
UCAT = catalog(withheld={"operate", "dispatch", "build"})
check("read-only route: an installed app's next is describe_app (a read), never open_app",
      UCAT.entries["app:notes"].next == "read(tool='describe_app', args={\"app_id\":\"notes\"})")
check("read-only route: a store app (install_app, an action) and a harness (build) are left out",
      "app:gridcraft" not in UCAT.entries and "harness:codex" not in UCAT.entries
      and "app:gridcraft" not in UCAT.searchable)
check("read-only route: no hit's next is an operate or a build call",
      not [e.next for e in UCAT.entries.values() if e.kind != "tool"
           and (e.next.startswith("operate(") or e.next.startswith("build("))])
NO_APPS = {"id": "t-r", "name": "Reading", "status": "active", "tool_groups": ["research", "memory"],
           "grant_names": [], "keys": [], "bastions": [], "context": {"memory": "all", "documents": True}}
TCAT = catalog(turn_type=NO_APPS)
check("turn type: an entry whose step the type does not hold is left out (open_app, describe_app, "
      "install_app are not in its groups)",
      "app:notes" not in TCAT.entries and "app:gridcraft" not in TCAT.entries)
check("turn type: a skill read through `context` (a group the type holds) stays",
      "skill:desktop" in TCAT.searchable)
CLOSED = dict(NO_APPS, status="closed")
check("closed type: discovery_entries gives nothing",
      asyncio.run(D.discovery_entries(None, 3, turn_type=CLOSED)) == [])

# ── compact and full ────────────────────────────────────────────────────────
check("compact_args: required marked *, an enum as its values, no descriptions",
      TD.compact_args(P_REG["turn_type"]["function"]["parameters"])
      == {"op*": "list|show|draft|change|retire|root", "request": "string", "type_id": "string",
          "name": "string", "spec": "object", "reason": "string"})
_hits = CAT.find({"query": "restart my notes app", "limit": 10})["result"]["tools"]
_old = [{"name": h["name"], "door": CAT.entries[h["name"]].door, "group": CAT.entries[h["name"]].group,
         "schema": CAT.entries[h["name"]].schema} for h in _hits if CAT.entries[h["name"]].kind == "tool"]
_new = [h for h in _hits if "door" in h]
check(f"compact: the tool hits are under a third of the old full-schema rendering "
      f"({len(json.dumps(_new))} vs {len(json.dumps(_old))} chars)",
      _new and len(json.dumps(_new)) * 3 < len(json.dumps(_old)))
check("compact: no hit carries a schema or a description",
      not [h for h in _hits if "schema" in h or "description" in json.dumps(h.get("args", {}))])
_f = CAT.find({"name": "skill:desktop"})["result"]["tools"][0]
check("find(name=) on a skill: its next call and the pointer to read it",
      _f["next"].startswith("read(tool='context'") and _f["path"] == "default-skills/desktop")
_m = CAT.find({"name": "capability:machine-runtime"})["result"]["tools"][0]
check("find(name=) on machine-runtime returns the whole text", _m.get("text", "").startswith("## What"))
_x = CAT.find({"name": "no_such_thing"})
check("find(name=) on a name that is not there is the model's argument error",
      _x["status"] == "error" and _x["reason"] == "args_schema")
check("find(group=kind) lists one kind", {t["name"] for t in CAT.find({"group": "app"})["result"]["tools"]}
      == {"app:notes", "app:gridcraft"})

# ── the core list's own tools ───────────────────────────────────────────────
CCAT = catalog()
_core = [_wire("build"), {"type": "function", "function": {
    "name": "close_turn", "description": "End the turn. Says why.",
    "parameters": {"type": "object", "properties": {"reason": {"type": "string",
                                                               "description": "Why."}}}}}]
check("add_core: the turn's own tools join as `direct` entries", CCAT.add_core(_core) == 2
      and CCAT.entries["build"].door == TD.DIRECT and "build" in CCAT.searchable)
_b = CCAT.find({"name": "build"})["result"]["tools"][0]
check("find(name=build): the full schema the list carries compact",
      _b["door"] == "direct" and _b["schema"]["parameters"] == P_REG["build"]["function"]["parameters"])
check("a direct tool is never a read or operate target",
      CCAT.read_schema_for("build") is None and CCAT.operate_refusal("build", {"action": "run"}) is None)
_rb = TD.read_refusal({"tool": "build", "args": {}}, CCAT, **_rk)
check("read_refusal still refuses a build door: call it by name",
      bool(_rb) and "call `build` directly" in _rb["result"])
check("a verb is found by name, never as a query hit (its whole schema is on the list)",
      CCAT.find({"name": "close_turn"})["result"]["tools"][0]["door"] == "direct"
      and not any(h["name"] == "close_turn"
                  for h in CCAT.find({"query": "end the turn"})["result"]["tools"]))
check("a build door is found by query, compact",
      any(h["name"] == "build" and h["door"] == "direct" and "schema" not in h
          for h in CCAT.find({"query": "build code run a build"})["result"]["tools"]))
check("add_core keeps an existing door entry's door", CCAT.add_core([_wire("web_search")]) == 0
      and CCAT.entries["web_search"].door == "read")

def _desc_strings(node, in_props=False) -> int:
    """How many `description` strings a schema node holds, at every depth (a
    property NAMED description is a name, not one)."""
    if isinstance(node, list):
        return sum(_desc_strings(n) for n in node)
    if not isinstance(node, dict):
        return 0
    n = 0
    for k, v in node.items():
        if in_props:
            n += _desc_strings(v)
        elif k == "description" and isinstance(v, str):
            n += 1
        else:
            n += _desc_strings(v, in_props=(k == "properties"))
    return n


_cs = TD.compact_schema(_wire("build"))
check("compact_schema: the first sentence, no description inside the parameters, all else kept",
      _cs["function"]["description"] == "The only way to build."
      and _desc_strings(_wire("build")["function"]["parameters"]) > 0
      and _desc_strings(_cs["function"]["parameters"]) == 0
      and _cs["function"]["parameters"]["required"] == P_REG["build"]["function"]["parameters"]["required"]
      and _cs["function"]["parameters"]["properties"]["variants"]["items"]["required"] == ["harness"]
      and P_REG["build"]["function"]["description"] != "The only way to build.")
_named = {"type": "function", "function": {"name": "x", "description": "A. B.", "parameters": {
    "type": "object", "properties": {"description": {"type": "string", "description": "d"}}}}}
check("compact_schema: a parameter NAMED description stays",
      TD.compact_schema(_named)["function"]["parameters"]["properties"] == {"description": {"type": "string"}})
check("first_sentence: e.g. does not end a sentence; a long one is cut with an ellipsis",
      TD.first_sentence("Open an app (e.g. notes) now. Then more.") == "Open an app (e.g. notes) now."
      and TD.first_sentence("word " * 80).endswith("…") and len(TD.first_sentence("word " * 80)) <= 161)


# ── one source fails, the others arrive ─────────────────────────────────────
async def _boom(*a, **k):
    raise RuntimeError("down")


def _broken_cards():
    raise OSError("no file")


_saved = (D._skills, D._apps, D._capability_cards, D._turn_types, D._harnesses, D._machine_runtime)
D._skills = _boom
D._apps = lambda pool, eid: asyncio.sleep(0, result=[ENTRIES[1]])
D._capability_cards = _broken_cards
D._turn_types = lambda pool, eid: asyncio.sleep(0, result=[ENTRIES[4]])
D._harnesses = _boom
D._machine_runtime = lambda: []
try:
    _got = asyncio.run(D.discovery_entries(None, 3))
finally:
    (D._skills, D._apps, D._capability_cards, D._turn_types, D._harnesses, D._machine_runtime) = _saved
check("a failing source gives [] and the others still arrive",
      [e["name"] for e in _got] == ["app:notes", "turn_type:Gardening"], [e["name"] for e in _got])

if FAILS:
    print(f"\ndiscovery-index: {len(FAILS)} FAIL(s)")
    sys.exit(1)
print("\ndiscovery-index: all PASS")
