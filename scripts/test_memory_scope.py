#!/usr/bin/env python3
"""Gate: a memory rides only where it belongs.

What is relevant somewhere is a leak elsewhere. A preference said about
one project or app must never be volunteered to a turn somewhere else; a
preference about the person (global — no scope) rides wherever it is relevant.
No DB, no network.

  1. recall's one WHERE builder (_canonical_where) takes a turn scope: global
     rows, rows of exactly this scope, and the everywhere types pass; nothing
     else does. No scope known -> global only. None -> no clause (the recall
     tool and search route are unscoped by design).
  2. memory_scope — the Host sets what a preference is ABOUT (metadata.about;
     where it was said, source_context, is provenance and never changes): a
     ref must name something this Host has, every change is kept in
     scope_history, kind=list shows every preference with its id. Every
     reader resolves scope through one expression (EFFECTIVE_SCOPE), and the
     decision survives a restatement and a dropped-row link.
  3. memory_scope is registered everywhere a tool must be and runs in the
     operate lane (one deterministic write, no model).
The pre-fire's use of the scope is pinned in test_memory_integrity.py.

  APP_ROOT=<repo> python3 scripts/test_memory_scope.py
"""
import asyncio
import json
import os
import sys

APP_ROOT = os.environ.get("APP_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, APP_ROOT)
sys.path.insert(0, os.path.join(APP_ROOT, "brain"))
fails: list[str] = []


def check(name, ok, detail=""):
    print(("  ok    " if ok else "  FAIL  ") + name + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        fails.append(name)


from mastercode import memory_retrieval as mr  # noqa: E402

# ── 1. the WHERE clause ─────────────────────────────────────────────────────
w, p = mr._canonical_where(3)
check("no turn_scope: no scope clause (recall tool / search stay unscoped)",
      "source_context" not in w and p == [3], w)
w, p = mr._canonical_where(3, turn_scope={"kind": "project", "ref": 229},
                           scope_everywhere_types=["relationship"])
_E = mr.EFFECTIVE_SCOPE
check("turn scope: global OR exactly this scope OR an everywhere type",
      f"{_E} IS NULL" in w and f"{_E}->>'kind' = 'global'" in w
      and f"{_E}->>'kind' = $2" in w and f"{_E}->>'ref' = $3" in w
      and "memory_type = ANY($4::text[])" in w
      and p == [3, "project", "229", ["relationship"]], f"{w} {p}")
w, p = mr._canonical_where(3, turn_scope={})
check("no scope known: global rows only",
      w.endswith(f"AND ({mr.scope_global_sql()})") and p == [3], f"{w} {p}")
w, p = mr._canonical_where(3, memory_types=["preference"], turn_scope={"kind": "app", "ref": "finance"})
check("composes with the type filter (placeholders in order)",
      "memory_type = ANY($2::text[])" in w and "= $3" in w and "= $4" in w
      and p == [3, ["preference"], "app", "finance"], f"{w} {p}")

w, p = mr._canonical_where(3, turn_scope=[{"kind": "app", "ref": "circles"},
                                          {"kind": "project", "ref": "7"}])
check("a list of scopes: global OR any one of them",
      "= $2" in w and "= $3" in w and "= $4" in w and "= $5" in w
      and p == [3, "app", "circles", "project", "7"], f"{w} {p}")
import inspect  # noqa: E402
_fr = inspect.getsource(mr.federated_recall)
check("a type that reads named projects adds them to the turn's scope",
      '{"kind": "project", "ref": str(int(p))} for p in _project_ids' in _fr)
from mastercode import memory_engine as _me  # noqa: E402
check("the SQL clause is the only scope check (no second Python filter to disagree with it)",
      not hasattr(_me, "in_turn_scope"))

# ── 2. memory_scope ─────────────────────────────────────────────────────────
from mastercode.runtime import tools as t  # noqa: E402


class _Pool:
    def __init__(self, row, projects=(9224, 7), threads=("0b6f0c1e-2c4e-4d4f-9a53-5b0f0e0a1a11",),
                 apps=("circles", "finance"), prefs=()):
        self.row, self.updates = row, []
        self.projects, self.threads, self.apps, self.prefs = projects, threads, apps, prefs

    async def fetchrow(self, sql, *a):
        return self.row

    async def fetchval(self, sql, *a):
        if "FROM projects" in sql:
            return 1 if a[0] in self.projects else None
        if "FROM circles_threads" in sql:
            return 1 if a[0] in self.threads else None
        raise AssertionError(sql)

    async def fetch(self, sql, *a):
        if "DISTINCT" in sql:
            return [{"ref": x} for x in self.apps]
        if "FROM projects" in sql:
            return [{"id": 9224, "name": "Garden plan"}]
        if "FROM circles_threads" in sql:
            return []
        if sql.startswith("SELECT id, content FROM entity_memory"):
            # memory_engine.rules_holding: the live rule-marked preferences
            return [{"id": p["id"], "content": p["content"]} for p in self.prefs
                    if p.get("rule_kind") is not None]
        if "FROM entity_memory" in sql:
            return list(self.prefs)
        raise AssertionError(sql)

    async def execute(self, sql, *a):
        self.updates.append((sql, a))
        return "UPDATE 1"


import server.brain_service as bs  # noqa: E402
_saved = (bs.get_pool, getattr(bs, "_entity_id", None))


def _run(args, row, **kw):
    pool = _Pool(row, **kw)
    bs.get_pool = lambda: pool
    bs._entity_id = 9003
    try:
        return asyncio.run(t._exec_memory_scope(args)), pool
    finally:
        bs.get_pool, bs._entity_id = _saved[0], _saved[1]


pref = {"id": 5101, "memory_type": "preference", "status": "active", "t_valid_until": None,
        "metadata": json.dumps({"source_context": {"kind": "project", "ref": "9224"}})}
res, pool = _run({"memory_id": 5101, "kind": "global"}, pref)
sql, a = pool.updates[0] if pool.updates else ("", ())
patch = json.loads(a[2]) if pool.updates else {}
check("global: writes about={global}, records 'where said' as the prior, never touches provenance",
      res["status"] == "ok" and patch.get("about") == {"kind": "global"}
      and patch.get("scope_history", [{}])[-1].get("from") == "where said"
      and patch.get("scoped_by") == "host"
      and "source_context" not in sql and "project_id" not in sql and "source_context" not in patch,
      f"{res} {sql} {patch}")
res, pool = _run({"memory_id": 5101, "kind": "project", "ref": "007"}, pref)
patch = json.loads(pool.updates[0][1][2]) if pool.updates else {}
check("project: the ref is stored as turns carry it ('007' -> '7')",
      res["status"] == "ok" and patch.get("about") == {"kind": "project", "ref": "7"}, f"{res}")
_T = "0B6F0C1E-2C4E-4D4F-9A53-5B0F0E0A1A11"
res, pool = _run({"memory_id": 5101, "kind": "thread", "ref": _T}, pref,
                 threads=(_T.lower(),))
patch = json.loads(pool.updates[0][1][2]) if pool.updates else {}
check("thread: the ref is the canonical lower-case id",
      res["status"] == "ok" and patch.get("about") == {"kind": "thread", "ref": _T.lower()}, f"{res}")
_hist = {"source_context": {"kind": "app", "ref": "circles"}, "about": {"kind": "app", "ref": "circles"},
         "scope_history": [{"from": "where said", "at": "x"}]}
res, pool = _run({"memory_id": 5101, "kind": "global"}, dict(pref, metadata=json.dumps(_hist)))
patch = json.loads(pool.updates[0][1][2]) if pool.updates else {}
check("history grows: the earlier decision survives a second change",
      [h["from"] for h in patch.get("scope_history", [])]
      == ["where said", {"kind": "app", "ref": "circles"}], f"{patch}")
res, pool = _run({"memory_id": 5101, "kind": "global"},
                 dict(pref, metadata=json.dumps({"about": {"kind": "global"}})))
check("same decision again: nothing written",
      res["status"] == "ok" and "unchanged" in res["result"] and not pool.updates, f"{res}")
res, pool = _run({"memory_id": 5101, "kind": "global"},
                 dict(pref, metadata=json.dumps({"source_context": None})))
check("a JSON-null provenance can still be set global (the decision is its own key)",
      res["status"] == "ok" and pool.updates, f"{res}")
res, pool = _run({"memory_id": 5102, "kind": "app", "ref": "Finance"}, dict(pref, id=5102))
patch = json.loads(pool.updates[0][1][2]) if pool.updates else {}
check("app: matched without regard to case, stored as the turns carry it",
      res["status"] == "ok" and patch.get("about") == {"kind": "app", "ref": "finance"}, f"{res}")
for args, why in (({"memory_id": 1, "kind": "app", "ref": "finanse"}, "an app with no turns"),
                  ({"memory_id": 1, "kind": "project", "ref": "999"}, "a project this Host lacks"),
                  ({"memory_id": 1, "kind": "project", "ref": "abc"}, "a non-numeric project ref"),
                  ({"memory_id": 1, "kind": "thread", "ref": "original"}, "a thread ref that is no id"),
                  ({"memory_id": 1, "kind": "thread", "ref": "0b6f0c1e-2c4e-4d4f-9a53-5b0f0e0a1a12"},
                   "a thread this Host lacks (or deleted)")):
    res, pool = _run(args, pref)
    check(f"refused before any write: {why}", res["status"] == "error" and not pool.updates, f"{res}")
res, pool = _run({"memory_id": 1, "kind": "global"}, dict(pref, memory_type="fact"))
check("only preferences are scoped this way; nothing written otherwise",
      res["status"] == "error" and not pool.updates, f"{res}")
res, pool = _run({"memory_id": 1, "kind": "global"}, dict(pref, status="archived"))
check("only active rows", res["status"] == "error" and not pool.updates, f"{res}")
res, pool = _run({"memory_id": 1, "kind": "global"}, dict(pref, t_valid_until="2026-09-30"))
check("only live rows (a replaced one is refused)", res["status"] == "error" and not pool.updates, f"{res}")
res, pool = _run({"memory_id": 1, "kind": "project"}, pref)
check("a scoped kind needs its ref", res["status"] == "error" and not pool.updates, f"{res}")
res, pool = _run({"memory_id": 1, "kind": "everywhere"}, pref)
check("an unknown kind is refused", res["status"] == "error" and not pool.updates, f"{res}")
res, pool = _run({"memory_id": "x", "kind": "global"}, pref)
check("memory_id must be an integer", res["status"] == "error" and not pool.updates, f"{res}")
res, pool = _run({"memory_id": 9, "kind": "global"}, None)
check("a row that is not this Host's (or does not exist) is refused",
      res["status"] == "error" and not pool.updates, f"{res}")
res, pool = _run({"kind": "list"}, None, prefs=[
    {"id": 5101, "content": "prefers metric units", "about": json.dumps({"kind": "global"}), "sc": None,
     "rule_kind": None},
    {"id": 5102, "content": "seed orders in their own folder", "about": None,
     "sc": json.dumps({"kind": "project", "ref": "9224"}), "rule_kind": None},
    # Rules are preferences marked rule_kind. One nobody has
    # scoped is read everywhere ("Things You Were Taught"), wherever it was
    # said; one scoped sits with the rest of its scope, marked.
    {"id": 5103, "content": "check the logs before retrying", "about": None,
     "sc": json.dumps({"kind": "project", "ref": "9224"}), "rule_kind": "taught"},
    {"id": 5104, "content": "no emoji in replies", "about": json.dumps({"kind": "global"}), "sc": None,
     "rule_kind": "governance"},
    # A plain preference in a rule's own words (cased otherwise) is held by it.
    {"id": 7001, "content": "Check the logs  before retrying", "about": None,
     "sc": json.dumps({"kind": "project", "ref": "7"}), "rule_kind": None}])
bys = (res.get("result") or {}).get("by_scope") or {}
check("list: every preference with its #id, by scope; undecided ones marked 'where said'",
      res["status"] == "ok"
      and bys.get("global") == ["#5101 prefers metric units", "#5104 [rule] no emoji in replies"]
      and bys.get("project:9224 (Garden plan) [where said]") == ["#5102 seed orders in their own folder"]
      and not pool.updates, f"{res}")
check("list: a plain preference in a rule's words is marked held by that rule",
      bys.get("project:7 [where said]") == ["#7001 [held by rule #5103] Check the logs before retrying"],
      f"{res}")
check("list: a rule nobody scoped is listed as read everywhere, marked [rule], and counted",
      bys.get("everywhere [rules nobody has scoped]") == ["#5103 [rule] check the logs before retrying"]
      and (res.get("result") or {}).get("rules") == 2, f"{res}")
_rs = inspect.getsource(mr)
check("recall shows a preference's #id (what memory_scope takes)",
      'tags.insert(0, f"#{h[\'id\']}")' in _rs)
check("one scope expression: the decision first, then where it was said",
      mr.EFFECTIVE_SCOPE == "COALESCE(metadata->'about', metadata->'source_context')"
      and mr.EFFECTIVE_SCOPE in mr._canonical_where(3, turn_scope={"kind": "app", "ref": "x"})[0])
_me_src = inspect.getsource(_me.MemoryEngine.retrieve)
check("the standing list reads the same expression", "EFFECTIVE_SCOPE" in _me_src
      and "scope_match_sql(" in _me_src)
w, p = mr._canonical_where(3, project_ids=[7])
check("a project-reading type also reads rows the Host set to be about that project",
      "metadata->'about'->>'ref' = ANY($2::text[])" in w and p == [3, [7]], w)

# ── 2b. the decision survives what moves or restates a row ─────────────────
# (On a real database: test_memory_integrity_db.py §1b — restatement, thread
# delete, the merge pass, split. Here: which writers carry it.)
from mastercode.memory_consolidation import MemoryConsolidator  # noqa: E402


class _SupPool:
    def __init__(self, result):
        self.result, self.sql = result, []

    async def execute(self, sql, *a):
        self.sql.append((sql, a))
        return self.result

    async def fetch(self, sql, *a):
        self.reads = getattr(self, "reads", []) + [sql]
        return []


sp = _SupPool("UPDATE 1")
asyncio.run(MemoryConsolidator(sp)._supersede(3, 10, 11))
carry = [q for q in sp.sql if "'about', o.metadata->'about'" in q[0]]
check("supersede: the replacing row takes the decision, never provenance",
      len(carry) == 1 and carry[0][1] == ([10], [11], 3)
      and "source_context" not in carry[0][0] and "project_id" not in carry[0][0]
      and "n.memory_type = c.memory_type" in carry[0][0], str(sp.sql))
sp = _SupPool("UPDATE 1")
asyncio.run(MemoryConsolidator(sp)._supersede(3, 10, None))
check("a delete (no replacement) carries nothing", len(sp.sql) == 1)
sp = _SupPool("UPDATE 0")
asyncio.run(MemoryConsolidator(sp)._supersede(3, 10, 11))
check("a supersede that did not land carries nothing", len(sp.sql) == 1)
check("a dropped row's link carries it", "_carry_about(" in inspect.getsource(MemoryConsolidator._link))
_proj = open(os.path.join(APP_ROOT, "server/routes/projects.py"), encoding="utf-8").read()
check("adoption moves provenance and never needs to know about the decision",
      "scoped_by" not in _proj and "'about'" not in _proj)

from mastercode import memory_consolidation as _mcm  # noqa: E402
check("the clause's 'no scope' entry is the consolidator's own UNSCOPED",
      json.loads(mr.UNSCOPED_JSON) == _mcm.UNSCOPED)
_ct_src = open(os.path.join(APP_ROOT, "brain/mastercode/circles_threads.py"), encoding="utf-8").read()
_ow = _ct_src[_ct_src.index("async def _own_words"):]
_ow = _ow[:_ow.index("\nasync def ", 10)]
check("thread delete's own-words row keeps the Host's decision",
      '("about", "scoped_by", "scoped_at", "scope_history")' in _ow)
_syn = open(os.path.join(APP_ROOT, "server/synaptive_handler.py"), encoding="utf-8").read()
check("an external client's identity reads its memory through recent_identity_memories "
      "(run on a real database in test_memory_integrity_db.py)",
      "recent_active = await recent_identity_memories(pool, entity_id)" in _syn
      and "'fact'" not in _syn[_syn.index("async def recent_identity_memories"):
                               _syn.index("async def handle_synaptive_identity")])

_mc_src = open(os.path.join(APP_ROOT, "brain/mastercode/memory_consolidation.py"), encoding="utf-8").read()
_take = _mc_src[_mc_src.index('md["rehomed_from"] = owner'):]
check("a takeover of a deleted thread's late row drops a decision naming that thread "
      "(unless it is a rule the person taught somewhere still live)",
      _take.index('if same_scope(md.get("about"), owner) and not await _taught_elsewhere(')
      < _take.index("UPDATE entity_memory SET metadata = $3::jsonb, project_id = $4"))
_ts = open(os.path.join(APP_ROOT, "brain/mastercode/runtime/tools.py"), encoding="utf-8").read()
check("memory_scope writes a thread scope only under the thread's share lock",
      "AND deleted_at IS NULL FOR SHARE))" in _ts)

# ── 3. registration ─────────────────────────────────────────────────────────
from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS, PUBLIC_META_TOOL_NAMES  # noqa: E402
from mastercode.runtime.meta_tool_caps import META_TOOL_CAPS  # noqa: E402
from mastercode.runtime.tool_lanes import lane_for  # noqa: E402
from mastercode.runtime.direct_ops import DIRECT_OP_TOOLS  # noqa: E402
check("schema, public name, caps, direct op and operate lane",
      "memory_scope" in PUBLIC_TOOL_SCHEMAS and "memory_scope" in PUBLIC_META_TOOL_NAMES
      and "memory_scope" in META_TOOL_CAPS and "memory_scope" in DIRECT_OP_TOOLS
      and lane_for("memory_scope") == "operate")
src = open(os.path.join(APP_ROOT, "brain/mastercode/runtime/tools.py"), encoding="utf-8").read()
check("dispatched by name", 'elif name == "memory_scope":' in src)

print(f"\n{len(fails)} failure(s)" if fails else "\nall checks passed")
sys.exit(1 if fails else 0)
