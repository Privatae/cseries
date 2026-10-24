#!/usr/bin/env python3
"""Search mode's doors.

`find_tools`, the `read` door, full-schema argument checks for
calls through read/operate, and the one resolver (tool_doors.effective_call)
that makes every name-keyed check judge read(tool=X) / operate(tool=X) as X —
except the ephemeral write block, which judges the door by its own name.

All of it sits behind a per-Host setting, behavioral_params.tool_surface
('groups' by default | 'search' | 'search_platform': search on the turns the
platform starts only). The first promise is that a groups-mode turn sees no
change at all.

Container-free: APP_ROOT=<repo> python3 scripts/test_tool_search_doors.py.
No pod, no DB, no model: sections 3, 5 and 6 run real turns through
EntityRuntime.send_message with a scripted model and a stub pool, the way
scripts/test_go_ahead_guard.py does.

  1. the setting and the resolver, pure
  2. the argument checks, pure — each rule refuses a bad call and passes a
     good one, every error in one result
  3. GROUPS MODE UNCHANGED, the real loop: the same turn with no setting,
     with 'groups' and with an unknown value sends byte-identical tool lists
     (session._bytes_sha) and runs operate exactly as before; 'search' sends
     its fixed core (scripts/test_tool_search_core.py): the six verbs,
     find_tools and read, and no group's tools. 'search_platform', with the
     real transient builder so the system prompt and its map are real: a
     person's turn (and a saved request) is byte-identical to no setting in
     tools and prompt; a platform-started turn is byte-identical to 'search'
  4. find_tools on the registry: every tool found by its own name, doors,
     limit and its maximum, the turn type, an unvalidated route, group lists,
     and no result the permission check would refuse
  5. the read door: read-safe targets (research, web_search, read_url, a
     read op of a mixed tool) run through the by-name read path; actions are
     refused with "this is an action — use operate"; nothing runs on a refusal
  6. the resolver, one case per name-keyed check, through the real loop:
     the unvalidated-model gate, the unseen lane, the woken-turn chain,
     tool_acts, read dedup, the search dedup and the trail — and the
     ephemeral write block still refusing every operate call
"""
import asyncio
import copy
import inspect
import json
import logging
import os
import re
import sys

APP_ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_ROOT)
sys.path.insert(0, os.path.join(APP_ROOT, "brain"))
logging.basicConfig(level=logging.ERROR)

FAILS: list[str] = []


def check(name, cond, extra=""):
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"[tool-doors] FAIL {name} {extra}")


from mastercode.runtime import tool_doors as TD  # noqa: E402
from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS as P_REG  # noqa: E402

# ── 1. the setting and the resolver ─────────────────────────────────────────
check("the tool-surface setting is gone: search is the only mode",
      not hasattr(TD, "tool_surface_of") and not hasattr(TD, "turn_tool_surface")
      and not hasattr(TD, "GROUPS") and TD.SEARCH == "search")

_ra = {"tool": "web_search", "args": {"query": "q"}}
_oa = {"intent": "x", "tool": "revoke_nac_rule", "args": {"kind": "allow", "id": 1}}
for _nm, _a in (("read", _ra), ("operate", _oa), ("web_search", {"query": "q"}), ("dispatch", {"task": "t"})):
    _n, _x = TD.effective_call(_nm, _a, search=False)
    check(f"resolver, groups mode: {_nm} is itself — the same objects", _n is _nm and _x is _a)
check("resolver, search mode: read(tool=X, args=A) is (X, A)",
      TD.effective_call("read", _ra, search=True) == ("web_search", {"query": "q"}))
check("resolver, search mode: operate(tool=X, args=A) is (X, A)",
      TD.effective_call("operate", _oa, search=True) == ("revoke_nac_rule", {"kind": "allow", "id": 1}))
check("resolver, search mode: any other call is itself",
      TD.effective_call("web_search", {"query": "q"}, search=True) == ("web_search", {"query": "q"})
      and TD.effective_call("find_tools", {"query": "q"}, search=True)[0] == "find_tools")
check("resolver, search mode: a door call with no tool stays the door",
      TD.effective_call("read", {}, search=True)[0] == "read"
      and TD.effective_call("operate", {"tool": "  "}, search=True)[0] == "operate"
      and TD.effective_call("read", ["x"], search=True)[0] == "read")
check("resolver, search mode: args that are not an object read as none",
      TD.effective_call("read", {"tool": "web_search", "args": "q"}, search=True) == ("web_search", {}))

# ── 2. the argument checks ──────────────────────────────────────────────────
SCH = {
    "type": "object", "required": ["op", "path"],
    "properties": {
        "op": {"type": "string", "enum": ["list", "read", "write"]},
        "path": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1},
        "ratio": {"type": "number"},
        "force": {"type": "boolean"},
        "tags": {"type": "array", "items": {"type": "string", "enum": ["a", "b"]}},
        "opts": {"type": "object", "required": ["mode"], "properties": {"mode": {"type": "string"}}},
        "free": {"type": "object"},
    },
}
GOOD = {"op": "read", "path": "a.txt", "limit": 5, "ratio": 0.5, "force": True,
        "tags": ["a"], "opts": {"mode": "x"}, "free": {"anything": 1}}
check("valid: a good call passes", TD.validate_args(SCH, GOOD) == [])
check("valid: an integral float is an integer (JSON has one number type)",
      TD.validate_args(SCH, {**GOOD, "limit": 5.0}) == [])
check("valid: null on an optional field reads as absent", TD.validate_args(SCH, {**GOOD, "limit": None}) == [])
check("valid: an object with no declared properties takes any field",
      TD.validate_args(SCH, {**GOOD, "free": {"x": [1]}}) == [])
_e = TD.validate_args(SCH, {"op": "read"})
check("required: a missing required field is refused", _e == ["missing required field `path`"], _e)
_e = TD.validate_args(SCH, {**GOOD, "path": None})
check("required: null on a required field is missing", _e == ["missing required field `path`"], _e)
_e = TD.validate_args(SCH, {**GOOD, "limit": "5"})
check("types: a string for an integer is refused", len(_e) == 1 and "`limit` must be an integer" in _e[0], _e)
_e = TD.validate_args(SCH, {**GOOD, "force": 1})
check("types: 1 is not a boolean", len(_e) == 1 and "`force` must be true or false" in _e[0], _e)
_e = TD.validate_args(SCH, {**GOOD, "limit": True})
check("types: a boolean is not an integer", len(_e) == 1 and "`limit`" in _e[0], _e)
_e = TD.validate_args(SCH, "not an object")
check("types: arguments that are not an object are refused",
      bool(_e) and "the arguments must be an object" in _e[0], _e)
_e = TD.validate_args(SCH, {**GOOD, "op": "delete"})
check("enums: a value outside the enum is refused, with the values",
      len(_e) == 1 and '["list", "read", "write"]' in _e[0], _e)
_e = TD.validate_args(SCH, {**GOOD, "tags": ["a", "c"]})
check("enums: inside an array too", len(_e) == 1 and "`tags[1]`" in _e[0], _e)
_e = TD.validate_args(SCH, {**GOOD, "pth": "x"})
check("unknown: an undeclared field is refused, naming the fields",
      len(_e) == 1 and "unknown field `pth`" in _e[0] and "path" in _e[0], _e)
_e = TD.validate_args(SCH, {**GOOD, "opts": {"mode": "x", "extra": 1}})
check("unknown: nested objects too", len(_e) == 1 and "unknown field `opts.extra`" in _e[0], _e)
_e = TD.validate_args(SCH, {**GOOD, "opts": {}})
check("required: nested objects too", _e == ["missing required field `opts.mode`"], _e)
_e = TD.validate_args(SCH, {**GOOD, "limit": 0})
check("minimum: below the minimum is refused", len(_e) == 1 and "at least 1" in _e[0], _e)
_e = TD.validate_args(SCH, {"op": "delete", "limit": "x", "pth": 1})
check("all errors come back in ONE result (4 here)", len(_e) == 4, _e)
_r = TD.args_refusal("t", _e, SCH)
check("the refusal: status error, reason args_schema (the ledger's model-fault reason), every error, the schema",
      _r["status"] == "error" and _r["reason"] == "args_schema" and _r["errors"] == _e
      and _r["parameters"] == SCH and "Nothing ran" in _r["result"])
check("a keyword the checker does not know never refuses",
      TD.validate_args({"type": "object", "properties": {"x": {"anyOf": [{"type": "string"}]}}}, {"x": 1}) == [])
_bad_reg = []
for _n, _s in P_REG.items():
    _p = (_s.get("function") or {}).get("parameters") or {}
    _req = {k: "x" for k in (_p.get("required") or [])}
    try:
        TD.validate_args(_p, _req)
    except Exception as _ex:  # noqa: BLE001
        _bad_reg.append((_n, repr(_ex)))
check(f"the checker runs on every one of the {len(P_REG)} registry schemas without raising",
      not _bad_reg, _bad_reg[:3])

# ── 3. the real loop's harness ─────────────────────────────────────────────
from mastercode.runtime import session as S  # noqa: E402
from mastercode import provider_gateway as PG  # noqa: E402
from mastercode import inference_config as IC  # noqa: E402
from mastercode.runtime import direct_ops as DO  # noqa: E402
from mastercode.runtime import model_validation as MV  # noqa: E402
from mastercode.runtime import turn_types as TT  # noqa: E402
from mastercode.runtime import prompt_schema as PS  # noqa: E402


def _wire(name):
    s = copy.deepcopy(P_REG[name])
    s.pop("_meta", None)
    return s


CATALOG_NAMES = ("web_search", "read_url", "research", "local_grep", "workspace_file",
                 "contacts_manage", "system.restart_app", "revoke_nac_rule", "build")
CLOSE = {"topic": "tool search", "handoff": {"next_step": "nothing"}}


class Pool:
    async def fetch(self, q, *a):
        return []

    async def fetchrow(self, q, *a):
        return None

    async def fetchval(self, q, *a):
        return None

    async def execute(self, q, *a):
        return "OK"

    def acquire(self):
        pool = self

        class _C:
            async def __aenter__(self):
                return pool

            async def __aexit__(self, *e):
                return False
        return _C()


def tc(name, args, i):
    return {"id": f"call_{i}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


class Harness:
    """One turn through EntityRuntime.send_message with a scripted model.
    Records every tools= list the model was sent, every meta-tool and
    direct-op execution, and every tool result by call id."""

    def __init__(self, script, *, behavioral=None, interaction_context="app:circles",
                 app_id=None, unvalidated=False, turn_type=None, catalog=CATALOG_NAMES,
                 real_prompt=False):
        self.script = list(script)
        self.behavioral = behavioral if behavioral is not None else {}
        self.interaction_context = interaction_context
        self.app_id = app_id
        # True: the turn builds its session and system prompt (the map
        # included) with the real transient builder, from a stub entity row;
        # the interaction context then follows app_id, as in a pod.
        self.real_prompt = real_prompt
        self.unvalidated = unvalidated
        self.turn_type = turn_type
        self.catalog = [_wire(n) for n in catalog]
        self.tools: list[list[dict]] = []
        self.calls: list[list[dict]] = []
        self.meta: list[tuple[str, dict, dict]] = []
        self.direct: list[tuple[str, dict, dict]] = []
        self.out = None

    async def model(self, messages=None, *a, **kw):
        msgs = messages if messages is not None else (a[0] if a else [])
        self.calls.append([dict(m) for m in msgs])
        self.tools.append(copy.deepcopy(kw.get("tools") or []))
        r = self.script.pop(0) if self.script else {
            "content": "Done.", "tool_calls": [tc("close_turn", CLOSE, 999)]}
        return {"tokens_used": 1, "prompt_tokens": 1, "completion_tokens": 1, "tool_calls": None, **r}

    def results(self) -> dict:
        out = {}
        for msgs in self.calls:
            for m in msgs:
                if m.get("role") == "tool" and m.get("tool_call_id"):
                    try:
                        out[m["tool_call_id"]] = json.loads(m.get("content") or "null")
                    except (TypeError, ValueError):
                        out[m["tool_call_id"]] = m.get("content")
        return out

    def names(self, i=0) -> list[str]:
        return [(t.get("function") or {}).get("name") for t in self.tools[i]]

    def run(self, message="Look into it for me.", **kw):
        saved = (S._call_entity_model, PG.call_model, IC.resolve_role_route,
                 S._AVAILABLE.get("ceigas_domain_controller"), S._run_close_turn,
                 S._execute_meta_tool, S._build_tool_schemas, DO.run_direct_op,
                 MV.enforced, MV.route_validated, TT.resolve_session_type)

        async def route(pool, eid, role, thread_override=None, **k):
            return {"provider": "anthropic", "model": "fake-model"}

        async def run_close_turn(pool, sess, args, *, prompt_console=None):
            return {"status": "ok", "result": "recorded"}

        async def meta(name, args, **k):
            self.meta.append((name, copy.deepcopy(args), k))
            if name == "local_grep":
                return {"status": "ok", "result": {"matches": [
                    {"path": "a.py", "line": 1, "text": "x = 1"}], "total_hits_returned": 1}}
            return {"status": "ok", "result": f"{name} ran"}

        async def direct(name, args, **k):
            self.direct.append((name, copy.deepcopy(args), k))
            return {"status": "ok", "result": f"{name} operated"}

        async def schemas(pool, eid, interaction_context=None, **k):
            return copy.deepcopy(self.catalog)

        async def resolve_type(pool, eid, **k):
            return self.turn_type

        S._call_entity_model = self.model
        PG.call_model = self.model
        IC.resolve_role_route = route
        S._AVAILABLE["ceigas_domain_controller"] = None
        S._run_close_turn = run_close_turn
        S._execute_meta_tool = meta
        S._build_tool_schemas = schemas
        DO.run_direct_op = direct
        if self.unvalidated:
            MV.enforced = lambda: True
            MV.route_validated = lambda *a, **k: (False, ("anthropic", "fake-model"))
        if self.turn_type is not None:
            TT.resolve_session_type = resolve_type
        rt = S.EntityRuntime(Pool())

        async def ctx(entity_id, **k):
            return S.EntitySession(
                session_id="sess-doors", entity_id=entity_id,
                entity={"id": entity_id, "name": "Host", "behavioral_params": self.behavioral,
                        "permission_overrides": {"bot_capabilities": 255}},
                interaction_context=self.interaction_context)
        _repos = PS._granted_repos
        if self.real_prompt:
            rt.book = Book(self.behavioral)
            PS._granted_repos = _no_repos  # the map's one network read
        else:
            rt._build_chat_context_transient = ctx
        try:
            self.out = asyncio.run(rt.send_message(
                entity_id=7, user_message=message, app_id=self.app_id, **kw))
        finally:
            (S._call_entity_model, PG.call_model, IC.resolve_role_route,
             S._AVAILABLE["ceigas_domain_controller"], S._run_close_turn,
             S._execute_meta_tool, S._build_tool_schemas, DO.run_direct_op,
             MV.enforced, MV.route_validated, TT.resolve_session_type) = saved
            PS._granted_repos = _repos
        return self


class Book:
    """The entity row the real transient builder loads."""

    def __init__(self, behavioral):
        self.behavioral = behavioral

    async def get_entity(self, entity_id):
        return {"id": entity_id, "name": "Host", "display_name": "Host", "status": "active",
                "behavioral_params": copy.deepcopy(self.behavioral),
                "permission_overrides": {"bot_capabilities": 255}, "current_weights": {}}

    async def get_entity_permissions(self, entity_id):
        return None


async def _no_repos(eid):
    return None


# ── 4. find_tools on the registry ───────────────────────────────────────────
from mastercode.runtime.tool_lanes import lane_for  # noqa: E402
from mastercode.runtime.tool_access import read_safe_ops, read_only_variant  # noqa: E402


def session_sets(caps):
    """The held read set and per-op map, built the way session.py builds
    them from the authorized catalog."""
    rst = {s["function"]["name"] for s in caps if lane_for(s["function"]["name"], s) == "read"}
    rso = {}
    for s in caps:
        n = s["function"]["name"]
        if n in rst:
            continue
        o = read_safe_ops(n, s)
        if o:
            rso[n] = o
    return rst, rso


CAPS = [_wire(n) for n in P_REG if not n.startswith("_meta_")
        and lane_for(n, P_REG[n]) != "retired"]
RST, RSO = session_sets(CAPS)
CAT = TD.DoorCatalog(CAPS, read_safe_tools=RST, read_safe_ops=RSO)
check(f"catalog: {len(CAT.entries)} door tools from {len(CAPS)} held; no verb, build door or retired tool",
      all(lane_for(n, P_REG[n]) not in ("host", "verb", "retired") for n in CAT.entries)
      and "build" not in CAT.entries and "close_turn" not in CAT.entries)
_wrong = [n for n, e in CAT.entries.items()
          if e.door != ("read" if (n in RST or n in RSO) else
                        "operate" if lane_for(n, P_REG[n]) == "operate" else "dispatch")]
check("catalog: each tool's door is its lane (read; a mixed tool's read ops read; operate; dispatch)",
      not _wrong, _wrong[:5])
_variant_ok = all(CAT.entries[n].schema["parameters"] == read_only_variant(n, _wire(n))["function"]["parameters"]
                  for n in RSO if n in CAT.entries)
check("catalog: a mixed tool is shown as its read-only variant (the session's own function)", _variant_ok)
_wf = CAT.render("workspace_file")
check("catalog: ...with its other ops and the door they take",
      _wf["door"] == "read" and _wf["other_ops"] == {"operate": ["write", "delete"]}, _wf.get("other_ops"))
_miss = [n for n in CAT.searchable if [x for x, _ in CAT.index().search(n, limit=1)] != [n]]
check(f"recall: every one of the {len(CAT.searchable)} searchable tools is first for its own name",
      not _miss, _miss[:5])
_r = CAT.find({"query": "restart my notes app"})["result"]["tools"]
check("find: a plain ask finds the op, compact: door, purpose, argument types, no schema",
      bool(_r) and _r[0]["name"] == "system.restart_app" and _r[0]["door"] == "operate"
      and _r[0]["args"] == TD.compact_args(_wire("system.restart_app")["function"]["parameters"])
      and "schema" not in _r[0] and "group" not in _r[0], _r[:1])
_full = CAT.find({"name": "system.restart_app"})["result"]["tools"]
check("find(name=): the one tool in full — its whole schema, door, group",
      len(_full) == 1 and _full[0]["door"] == "operate" and _full[0]["group"] == "system"
      and _full[0]["schema"]["parameters"] == _wire("system.restart_app")["function"]["parameters"], _full)
check("find: 5 results by default", len(CAT.find({"query": "app"})["result"]["tools"]) == 5)
check("find: limit is respected", len(CAT.find({"query": "app", "limit": 2})["result"]["tools"]) == 2)
check("find: 10 at most", len(CAT.find({"query": "app", "limit": 50})["result"]["tools"]) == 10)
check("find: a limit below 1 returns one", len(CAT.find({"query": "app", "limit": 0})["result"]["tools"]) == 1)
_bad = CAT.find({"query": 5, "limt": 3})
check("find: its own arguments are checked (type, unknown field), nothing searched",
      _bad["status"] == "error" and _bad["reason"] == "args_schema" and len(_bad["errors"]) == 2, _bad)
check("find: neither query nor group is refused with the group list",
      CAT.find({})["status"] == "error" and bool(CAT.find({})["groups"]))
_g = CAT.find({"group": "research"})["result"]
check("find(group=): lists the group's tools by name, door and purpose only (no schemas, no doubled text)",
      _g["group"] == "research" and bool(_g["tools"])
      and all(set(t) == {"name", "door", "purpose"} for t in _g["tools"])
      and {"web_search", "read_url", "research"} <= {t["name"] for t in _g["tools"]}, _g)
check("find(group=): an unknown group is refused with the groups there are",
      CAT.find({"group": "nope"})["status"] == "error" and bool(CAT.find({"group": "nope"})["groups"]))
_gq = CAT.find({"query": "restart", "group": "research"})["result"]["tools"]
check("find(query, group): searches only that group",
      all(CAT.entries[t["name"]].group == "research" for t in _gq))
_none = CAT.find({"query": "zzqqxx"})["result"]
check("find: nothing found says so and lists the groups", _none["tools"] == [] and bool(_none["groups"]))

# turn type: only what the type holds; a closed type nothing.
FILES_TYPE = {"id": "t-files", "name": "Files", "status": "active", "tool_groups": ["files", "research"],
              "grant_names": [], "keys": [], "bastions": [], "context": {"memory": "all", "documents": True}}
TCAT = TD.DoorCatalog(CAPS, read_safe_tools=RST, read_safe_ops=RSO, turn_type=FILES_TYPE)


def group_refused(ttype, name) -> bool:
    """check_call's step 0/1 refusal — the tool alone, whatever its arguments
    (step 5 can refuse a call whose target it could not read; that is the
    call's arguments, decided at call time)."""
    d = TT.check_call(ttype, name, {}) or {}
    return d.get("kind") == "group" and str(d.get("outside") or "").startswith("tool ")


_out = [n for n in TCAT.searchable if group_refused(FILES_TYPE, n)]
check("turn type: no listed tool is one the type's gate refuses by group (turn_types.check_call)",
      not _out and bool(TCAT.searchable), _out[:5])
_hidden = [n for n in CAT.entries if n not in TCAT.searchable and not group_refused(FILES_TYPE, n)]
check("turn type: every tool the type's gate admits is listed", not _hidden, _hidden[:5])
check("turn type: the search never returns a tool outside the type",
      all(t["name"] in TCAT.searchable
          for q in ("restart my app", "revoke the rule", "search the web", "read a file")
          for t in TCAT.find({"query": q})["result"]["tools"])
      and "system.restart_app" not in [t["name"] for t in TCAT.find({"query": "system.restart_app"})["result"]["tools"]])
CLOSED = dict(FILES_TYPE, status="closed", closed_reason="unreadable")
check("turn type: a closed type lists nothing",
      not TD.DoorCatalog(CAPS, read_safe_tools=RST, read_safe_ops=RSO, turn_type=CLOSED).searchable)

# unvalidated route: only read-door tools.
UCAT = TD.DoorCatalog(CAPS, read_safe_tools=RST, read_safe_ops=RSO, withheld={"operate", "dispatch", "build"})
check("unvalidated: only read-door tools are searchable",
      bool(UCAT.searchable) and all(UCAT.entries[n].door == "read" for n in UCAT.searchable))
_u = UCAT.find({"query": "restart my notes app"})["result"]
check("unvalidated: an action is never returned, and the result says why",
      all(t["door"] == "read" for t in _u["tools"]) and "not validated" in _u["route"], _u)
check("unvalidated: a mixed tool's other ops are not offered",
      "other_ops" not in UCAT.render("workspace_file"))

# no search result fails the permission check.
_viol = []
for cat, ttype, wh in ((CAT, None, set()), (TCAT, FILES_TYPE, set()),
                       (UCAT, None, {"operate", "dispatch", "build"})):
    for q in ("restart", "web", "file", "network rule", "contacts", "memory", "project", "schedule a task"):
        for t in cat.find({"query": q, "limit": 10})["result"]["tools"]:
            n = t["name"]
            held = any(s["function"]["name"] == n for s in CAPS)
            door_ok = (t["door"] == "read" and (n in RST or n in RSO)) or (
                t["door"] == "operate" and DO.is_direct_op_tool(n)) or t["door"] == "dispatch"
            if not (held and door_ok and TD.type_holds(ttype, n, TT.category_map()) and t["door"] not in wh):
                _viol.append((q, n, t["door"]))
check("no search result fails the permission check (held, its door, the type, the validated gate)",
      not _viol, _viol[:5])

# ── 5. the read door ────────────────────────────────────────────────────────
_rk = dict(read_safe_tools=RST, read_safe_ops=RSO, host_own_tools={"build"})
for _t, _a in (("web_search", {"query": "q"}), ("read_url", {"url": "https://x"}),
               ("research", {"query": "q"}), ("workspace_file", {"op": "list"})):
    check(f"read: {_t} is admitted", TD.read_refusal({"tool": _t, "args": _a}, CAT, **_rk) is None)
_x = TD.read_refusal({"tool": "workspace_file", "args": {"op": "write", "path": "a", "content": "b"}}, CAT, **_rk)
check("read: a mixed tool's write op is refused — this is an action, use operate",
      bool(_x) and "this is an action — use operate" in _x["result"] and _x["use_instead"] == "operate", _x)
_x = TD.read_refusal({"tool": "system.restart_app", "args": {"app_id": "n"}}, CAT, **_rk)
check("read: an operate-lane tool is refused — this is an action, use operate",
      bool(_x) and "this is an action — use operate" in _x["result"], _x)
# The agent lane is untrusted work only (tool_lanes
# .UNTRUSTED_WORK_TOOLS = {github}), and github's reads are the Host's own, so
# its entry's door is `read`. A dispatch-DOOR entry is built here from github
# narrowed to the op that is an agent's (import), the one shape that lands on
# that door.
def _narrowed(name, keep):
    s = _wire(name)
    props = ((s.get("function") or {}).get("parameters") or {}).get("properties") or {}
    for k in ("op", "action"):
        if isinstance(props.get(k), dict) and props[k].get("enum"):
            props[k]["enum"] = [v for v in props[k]["enum"] if v in keep]
    return s


DCAPS = [c for c in CAPS if c["function"]["name"] != "github"] + [_narrowed("github", {"import"})]
DRST, DRSO = session_sets(DCAPS)
DCAT = TD.DoorCatalog(DCAPS, read_safe_tools=DRST, read_safe_ops=DRSO)
_drk = dict(read_safe_tools=DRST, read_safe_ops=DRSO, host_own_tools={"build"})
_disp = next((n for n, e in DCAT.entries.items() if e.door == "dispatch"), None)
_x = TD.read_refusal({"tool": _disp, "args": {}}, DCAT, **_drk) if _disp else None
# Its refusal names it as untrusted work outside the platform, the one kind an
# agent takes; a tool that is not an operate op is never told "dispatch an
# agent with it in tool_scope".
check("read: a dispatch-lane tool is refused as untrusted work, and it names dispatch",
      bool(_x) and "untrusted work outside the platform" in _x["result"]
      and _x["use_instead"] == "dispatch",
      (_disp, _x))
_x = TD.read_refusal({"tool": "build", "args": {}}, CAT, **_rk)
check("read: a build door is refused — call it by name", bool(_x) and "call `build` directly" in _x["result"], _x)
_x = TD.read_refusal({"tool": "no_such_tool"}, CAT, **_rk)
check("read: an unknown name is refused as unknown_name", bool(_x) and _x["reason"] == "unknown_name", _x)
# The refusal carries the tools the model most likely meant (seen on a live
# test run, entity 9164: gpt-4o-mini called read(tool='documents'), was told only that
# the name did not exist, and gave up). Candidates come from find_tools' own
# search, so every one is a tool this turn can call.
_x = TD.read_refusal({"tool": "documents", "args": {}}, CAT, **_rk)
_named = re.findall(r"tool='([^']+)'|call `([^`]+)` by its name", (_x or {}).get("result", ""))
_named = [a or b for a, b in _named]
check("read: an unknown name's refusal names real tools to call instead",
      bool(_x) and _x["reason"] == "unknown_name" and _named
      and all(n in CAT.searchable for n in _named) and "Call the one" in _x["result"], _x)
check("read: ...the same candidates find_tools(name=...) offers for that name",
      [t["name"] for t in CAT.near("documents")] == _named[:len(CAT.near("documents"))], (_named, CAT.near("documents")))
_x = CAT.operate_refusal("filesystem", {"file_name": "a.txt", "content": "x"},
                         intent="Create a file in the workspace")
check("operate: a name that is nothing at all is refused as unknown_name, not left to the op wall",
      bool(_x) and _x["reason"] == "unknown_name" and "`filesystem`" in _x["result"], _x)
check("operate: ...and its stated intent finds the tool that writes a workspace file",
      bool(_x) and "workspace_file" in _x["result"], _x)
check("operate: a code-running name is still left to its own wall",
      CAT.operate_refusal("codex.run", {}, intent="run it") is None)
check("operate: an MCP tool name is still left to the MCP gate",
      CAT.operate_refusal("mcp:acme/do_thing", {}, intent="do it") is None)
# Each candidate is called the way find_tools' own description says: a
# dispatch-door tool goes in a dispatch's tool_scope. dispatch has no `tool`
# argument, so `dispatch(tool='X', ...)` sends the model to a call the dispatch
# door refuses (reachout_channel, and every mixed tool whose other ops
# dispatch).
_lines = {n: TD._near_line(CAT.render(n)) for n in sorted(CAT.searchable)}
_dispatched = sorted(n for n in CAT.searchable if CAT.entries[n].kind == "tool"
                     and (CAT.entries[n].door == "dispatch"
                          or "dispatch" in (CAT.render(n).get("other_ops") or {})))
check("near: negative control — the catalog has tools that act through dispatch", bool(_dispatched))
check("near: no candidate line says dispatch(tool=...), a call dispatch refuses",
      not [n for n, ln in _lines.items() if "dispatch(tool=" in ln],
      [ln for ln in _lines.values() if "dispatch(tool=" in ln][:3])
check("near: every tool that acts through dispatch is named in its tool_scope",
      all(f"tool_scope=['{n}']" in _lines[n] for n in _dispatched),
      [_lines[n] for n in _dispatched if f"tool_scope=['{n}']" not in _lines[n]][:3])
_d1 = _disp   # the dispatch-door entry built above (DCAT)
_d1_line = TD._near_line(DCAT.render(_d1)) if _d1 else ""
check("near: a dispatch-door tool's line lists no tool arguments (the agent writes them)",
      _d1 is not None and " with " not in _d1_line.split(" — ")[0], _d1_line)
check("near: an entry with no purpose ends on its call, not a dangling dash",
      not TD._near_line({"name": "x", "kind": "skill", "next": "read(tool='skill', args={})",
                         "purpose": ""}).endswith("—"))
_x = TD.read_refusal({"tool": "zzqx"}, CAT, **_rk)
check("read: a name like nothing still refuses, pointing at find_tools",
      bool(_x) and _x["reason"] == "unknown_name"
      and ("find_tools" in _x["result"] or "Call the one" in _x["result"]), _x)
_x = TD.read_refusal({"args": {}}, CAT, **_rk)
check("read: no tool is refused against read's own schema", bool(_x) and _x["reason"] == "args_schema", _x)
_x = TD.read_refusal({"tool": "web_search", "args": {"q": "x"}}, CAT, **_rk)
check("read: the target's arguments are checked (missing query, unknown q), every error at once",
      bool(_x) and _x["reason"] == "args_schema" and len(_x["errors"]) == 2, _x)
_x = TD.read_refusal({"tool": "workspace_file", "args": {"op": "list", "encoding": "utf-16"}}, CAT, **_rk)
check("read: ...against the schema find_tools showed (the variant's enums)",
      bool(_x) and _x["reason"] == "args_schema", _x)

READ_SCRIPT = [
    {"content": "", "tool_calls": [
        tc("find_tools", {"query": "search the web"}, 1),
        tc("read", {"tool": "web_search", "args": {"query": "pgvector halfvec"}}, 2),
        tc("read", {"tool": "read_url", "args": {"url": "https://example.org"}}, 3),
        tc("read", {"tool": "research", "args": {"query": "bm25"}}, 4),
        tc("read", {"tool": "workspace_file", "args": {"op": "list"}}, 5),
        tc("read", {"tool": "workspace_file", "args": {"op": "write", "path": "a", "content": "b"}}, 6),
        tc("read", {"tool": "system.restart_app", "args": {"app_id": "notes"}}, 7),
        tc("read", {"tool": "web_search", "args": {"query": 5}}, 8),
    ]},
    {"content": "Done.", "tool_calls": [tc("close_turn", CLOSE, 9)]},
]
h = Harness(READ_SCRIPT, behavioral={"tool_surface": "search"}).run()
res = h.results()
check("loop: find_tools answers from the catalog",
      (res.get("call_1") or {}).get("status") == "ok"
      and res["call_1"]["result"]["tools"][0]["name"] == "web_search", res.get("call_1"))
check("loop: read ran web_search, read_url, research and a read op of a mixed tool — through the held-read path",
      [n for n, _, _ in h.meta] == ["web_search", "read_url", "research", "workspace_file"]
      and all(k.get("interaction_context") is None and "turn_type" in k for _, _, k in h.meta), h.meta)
check("loop: the read door passes the TARGET's own arguments",
      len(h.meta) >= 4 and h.meta[0][1] == {"query": "pgvector halfvec"} and h.meta[3][1] == {"op": "list"})
check("loop: a write op and an operate-lane tool are refused, nothing run",
      "this is an action — use operate" in str(res.get("call_6"))
      and "this is an action — use operate" in str(res.get("call_7")) and not h.direct)
check("loop: bad target arguments are refused before anything runs",
      (res.get("call_8") or {}).get("reason") == "args_schema" and len(h.meta) == 4)

# A typed turn: the doors hand the TARGET and the type to the gate.
h = Harness([{"content": "", "tool_calls": [
    tc("find_tools", {"query": "restart my notes app"}, 1),
    tc("read", {"tool": "web_search", "args": {"query": "x"}}, 2),
    tc("operate", {"intent": "Restarting.", "tool": "system.restart_app", "args": {"app_id": "notes"}}, 3),
    tc("find_tools", {"query": "search the web, read a page, list files"}, 5)]},
    {"content": "Done.", "tool_calls": [tc("close_turn", CLOSE, 4)]}],
    behavioral={"tool_surface": "search"}, turn_type=FILES_TYPE).run()
res = h.results()
_found = [t["name"] for t in ((res.get("call_1") or {}).get("result") or {}).get("tools") or []]
_found5 = [t["name"] for t in ((res.get("call_5") or {}).get("result") or {}).get("tools") or []]
# Tool hits only: a skill/app/capability entry (`kind`) and the turn's own
# list ('direct') are not tools the type's groups decide.
_tool_hits = [t["name"] for c in ("call_1", "call_5")
              for t in ((res.get(c) or {}).get("result") or {}).get("tools") or []
              if not t.get("kind") and t.get("door") != "direct"]
check("typed loop: find_tools lists nothing outside the type",
      "system.restart_app" not in _found and bool(_found5)
      and not [n for n in _tool_hits if group_refused(FILES_TYPE, n)], (res.get("call_1"), _found5))
check("typed loop: read hands the gate the target with the turn type (the wall is in _execute_meta_tool)",
      bool(h.meta) and h.meta[0][0] == "web_search"
      and (h.meta[0][2].get("turn_type") or {}).get("name") == "Files", h.meta)
check("typed loop: operate hands run_direct_op the target with the turn type",
      bool(h.direct) and h.direct[0][0] == "system.restart_app"
      and (h.direct[0][2].get("turn_type") or {}).get("name") == "Files", h.direct)

# ── 6. the resolver, one case per name-keyed check ─────────────────────────
# (a) the unvalidated-model gate
h = Harness([{"content": "", "tool_calls": [
    tc("read", {"tool": "web_search", "args": {"query": "x"}}, 1),
    tc("operate", {"intent": "Restarting.", "tool": "system.restart_app", "args": {"app_id": "n"}}, 2),
    tc("read", {"tool": "build", "args": {}}, 3),
    tc("find_tools", {"query": "restart my notes app"}, 4)]},
    {"content": "Done.", "tool_calls": [tc("close_turn", CLOSE, 5)]}],
    behavioral={"tool_surface": "search"}, unvalidated=True, interaction_context="project:9").run()
res = h.results()
check("unvalidated: the list keeps read and find_tools, without operate and dispatch",
      {"read", "find_tools"} <= set(h.names(0)) and not {"operate", "dispatch"} & set(h.names(0)), h.names(0))
check("unvalidated: read(tool=X) runs (an unvalidated route keeps its reads)",
      [n for n, _, _ in h.meta] == ["web_search"], h.meta)
check("unvalidated: operate(tool=X) is refused by the gate, as `operate`",
      (res.get("call_2") or {}).get("reason") == "not_advertised"
      and "`operate` is not available" in res["call_2"]["result"] and not h.direct, res.get("call_2"))
check("unvalidated: read(tool=<withheld build door>) is judged as that door",
      (res.get("call_3") or {}).get("reason") == "not_advertised"
      and "`build` is not available" in res["call_3"]["result"], res.get("call_3"))
# A tool hit carries a door; a skill/app/capability hit carries `next`
# instead (runtime/discovery_index); the turn's own list rides as 'direct'.
check("unvalidated: find_tools returns no operate or dispatch tool",
      all(t.get("door") in (None, "read", "direct") for t in res["call_4"]["result"]["tools"])
      and not any("operate(" in str(t.get("next") or "") for t in res["call_4"]["result"]["tools"]),
      res.get("call_4"))

# (b) the unseen lane (a replay variant)
LANE_CALLS = [
    tc("read", {"tool": "web_search", "args": {"query": "x"}}, 1),
    tc("find_tools", {"query": "web"}, 2),
    tc("operate", {"intent": "Listing.", "tool": "workspace_file", "args": {"op": "list"}}, 3),
    tc("operate", {"intent": "Restarting.", "tool": "system.restart_app", "args": {"app_id": "n"}}, 4)]
h = Harness([{"content": "", "tool_calls": LANE_CALLS},
             {"content": "Done.", "tool_calls": [tc("close_turn", CLOSE, 5)]}],
            behavioral={"tool_surface": "search"}, app_id="circles-replay").run()
res = h.results()
_lane = "a lane whose reply only an evaluation"
check("unseen lane: read(tool=<a read>) and find_tools run",
      [n for n, _, _ in h.meta] == ["web_search"] and (res.get("call_2") or {}).get("status") == "ok",
      (h.meta, res.get("call_2")))
check("unseen lane: operate(tool=<a read op>) is judged as that read and may run",
      [n for n, _, _ in h.direct] == ["workspace_file"], h.direct)
check("unseen lane: operate(tool=<an action>) is refused", _lane in str(res.get("call_4")), res.get("call_4"))
hg = Harness([{"content": "", "tool_calls": [LANE_CALLS[2]]},
              {"content": "Done.", "tool_calls": [tc("close_turn", CLOSE, 5)]}], app_id="circles-replay").run()
check("unseen lane, a Host with no setting: judged as the target too (one mode now)",
      [n for n, _, _ in hg.direct] == ["workspace_file"], hg.direct)

# (c) the woken-turn chain (a system turn past the chain: may_start_work=False)
h = Harness([{"content": "", "tool_calls": LANE_CALLS},
             {"content": "Done.", "tool_calls": [tc("close_turn", CLOSE, 5)]}],
            behavioral={"tool_surface": "search"}).run(system_turn=True, may_start_work=False)
res = h.results()
check("woken chain: reads (through read, find_tools, operate on a read op) run; the action does not",
      [n for n, _, _ in h.meta] == ["web_search"] and (res.get("call_2") or {}).get("status") == "ok"
      and [n for n, _, _ in h.direct] == ["workspace_file"]
      and "starts no new work" in str(res.get("call_4")), (h.meta, h.direct, res.get("call_4")))

# (d) the ephemeral write block — the exception: the door judged by its own name
h = Harness([{"content": "", "tool_calls": [
    tc("operate", {"intent": "Listing.", "tool": "workspace_file", "args": {"op": "list"}}, 1),
    tc("operate", {"intent": "Restarting.", "tool": "system.restart_app", "args": {"app_id": "n"}}, 2),
    tc("read", {"tool": "web_search", "args": {"query": "x"}}, 3),
    tc("read", {"tool": "workspace_file", "args": {"op": "write", "path": "a", "content": "b"}}, 4)]},
    {"content": "Done.", "tool_calls": [tc("close_turn", CLOSE, 5)]}],
    behavioral={"tool_surface": "search"}, interaction_context="test").run()
res = h.results()
check("ephemeral: EVERY operate call is refused as a write — even one naming a read op",
      not h.direct and "Write tool 'operate' disabled" in str(res.get("call_1"))
      and "Write tool 'operate' disabled" in str(res.get("call_2")), (res.get("call_1"), res.get("call_2")))
check("ephemeral: read(tool=<read-safe>) passes", [n for n, _, _ in h.meta] == ["web_search"], h.meta)
check("ephemeral: read(tool=<not read-safe>) is blocked as the write it names",
      (res.get("call_4") or {}).get("status") == "blocked"
      and "Write tool 'workspace_file'" in res["call_4"]["result"], res.get("call_4"))


# (e) tool_acts — the identity hold's "this turn acted". A person's turn that
# took over a held request carries a Takeover in session._TAKEOVER; the stub
# below is handed over the way EntityRuntime._take_over_held hands one over.
class _Takeover:
    def __init__(self):
        self.acted = False
        self.model_saw = False

    async def settle(self, outcome, why=""):
        return None


def acted_after(script, behavioral):
    tk = _Takeover()
    h = Harness(script, behavioral=behavioral)
    _orig = S.EntityRuntime._take_over_held

    async def take_over(self, entity_id, **k):
        S._TAKEOVER.set(tk)
        return []
    S.EntityRuntime._take_over_held = take_over
    try:
        h.run()
    finally:
        S.EntityRuntime._take_over_held = _orig
    return tk.acted


_end = {"content": "Done.", "tool_calls": [tc("close_turn", CLOSE, 9)]}
_restart = tc("operate", {"intent": "Restarting.", "tool": "system.restart_app", "args": {"app_id": "n"}}, 1)
check("tool_acts: read(tool=<a read>) and find_tools do not act",
      acted_after([{"content": "", "tool_calls": [
          tc("read", {"tool": "web_search", "args": {"query": "x"}}, 1),
          tc("find_tools", {"query": "x"}, 2)]}, _end], {"tool_surface": "search"}) is False)
check("tool_acts: operate(tool=<an action>) acts",
      acted_after([{"content": "", "tool_calls": [_restart]}, _end], {"tool_surface": "search"}) is True)
check("tool_acts, groups mode: a read by name does not act; operate does (as on main)",
      acted_after([{"content": "", "tool_calls": [tc("web_search", {"query": "x"}, 1)]}, _end], {}) is False
      and acted_after([{"content": "", "tool_calls": [_restart]}, _end], {}) is True)

# (f) read dedup: a read re-runs every time; an action's repeat is cached, then refused
_same = {"tool": "web_search", "args": {"query": "same"}}
_op = {"intent": "Restarting.", "tool": "system.restart_app", "args": {"app_id": "n"}}
h = Harness([{"content": "", "tool_calls": [tc("read", _same, 1)]},
             {"content": "", "tool_calls": [tc("read", _same, 2)]},
             {"content": "", "tool_calls": [tc("read", _same, 3)]},
             {"content": "", "tool_calls": [tc("find_tools", {"query": "web"}, 4)]},
             {"content": "", "tool_calls": [tc("find_tools", {"query": "web"}, 5)]},
             {"content": "", "tool_calls": [tc("find_tools", {"query": "web"}, 6)]},
             {"content": "", "tool_calls": [tc("operate", _op, 7)]},
             {"content": "", "tool_calls": [tc("operate", _op, 8)]},
             {"content": "", "tool_calls": [tc("operate", _op, 9)]},
             {"content": "Done.", "tool_calls": [tc("close_turn", CLOSE, 10)]}],
            behavioral={"tool_surface": "search"}).run()
res = h.results()
check("read dedup: read(tool=<a read>) re-runs each time, judged as the read",
      [n for n, _, _ in h.meta] == ["web_search"] * 3 and "unchanged_since_round" in json.dumps(res.get("call_3")),
      (h.meta, res.get("call_3")))
check("read dedup: find_tools re-runs each time (a read of the index), never refused",
      "dedup" not in json.dumps([res.get(f"call_{i}") for i in (4, 5, 6)])
      and "unchanged_since_round" in json.dumps(res.get("call_6")), res.get("call_6"))
check("read dedup: operate(tool=<an action>) runs once, its repeat is served cached, a third is refused",
      len(h.direct) == 1 and (res.get("call_8") or {}).get("cached") is True
      and (res.get("call_9") or {}).get("reason") == "dedup", (h.direct, res.get("call_8"), res.get("call_9")))

# (g) the search dedup: read(tool='local_grep') is local_grep's hits
_g1 = {"tool": "local_grep", "args": {"pattern": "x"}}
_g2 = {"tool": "local_grep", "args": {"pattern": "x = 1"}}
h = Harness([{"content": "", "tool_calls": [tc("read", _g1, 1)]},
             {"content": "", "tool_calls": [tc("read", _g2, 2)]},
             {"content": "Done.", "tool_calls": [tc("close_turn", CLOSE, 3)]}],
            behavioral={"tool_surface": "search"}).run()
check("search dedup: a hit already shown this turn is not sent again through read either",
      "already_shown" in json.dumps(h.results().get("call_2")), h.results().get("call_2"))

# (h) the trail: a read chip names the tool it read
from mastercode.runtime.prefire_tools import trail_tool_names  # noqa: E402
h = Harness([{"content": "", "tool_calls": [tc("read", {"tool": "workspace_file", "args": {"op": "list"}}, 1)]},
             {"content": "Done.", "tool_calls": [tc("close_turn", CLOSE, 2)]}],
            behavioral={"tool_surface": "search"}).run()
_chips = [c for c in ((h.out or {}).get("tool_actions") or []) if isinstance(c, dict)]
check("trail: a read chip carries read_tool and its op",
      any(c.get("tool") == "read" and c.get("read_tool") == "workspace_file" and c.get("op") == "list"
          for c in _chips), _chips)
check("trail: the pick reads a read chip as the tool it read, and an operate chip as before",
      trail_tool_names([{"tool_calls": [{"tool": "read", "read_tool": "workspace_file"},
                                        {"tool": "operate", "operate_tool": "revoke_nac_rule"},
                                        {"tool": "web_search"}]}], max_turns=3)
      == ["workspace_file", "revoke_nac_rule", "web_search"])

# (i) the go-ahead guard's list of operations: names the op's target; reads
# start no work. Structural: the append is in the operate branch only, on
# `_ga_op` (operate's target, which is what the resolver returns for it, or
# for a watch the server's tool it called), and the read door's branch never
# touches _ga_firings.
# The turn's code: its core, then the tool loop (a method of its own).
_src = (inspect.getsource(S.EntityRuntime._send_message_core)
        + inspect.getsource(S.EntityRuntime._run_tool_loop))
_ops_at = _src.find('_ga_firings[-1].setdefault("ops", []).append(_ga_op[:80])')
_op_branch = _src.find('elif func_name == "operate":')
_next_branch = _src.find('elif func_name in getattr(sess, "_host_own_tools", set()):')
_door = _src[_src.find('elif _search_mode and func_name == "read":'):_op_branch]
check("go-ahead ops: recorded once, inside the operate branch, on the op's target",
      _src.count('setdefault("ops", [])') == 1 and 0 < _op_branch < _ops_at < _next_branch
      and "_ga_op: str | None = _op_tool" in _src[_op_branch:_ops_at])
check("go-ahead ops: the read door records nothing there", bool(_door) and "_ga_firings" not in _door)
check("resolver: operate's target is what operate's branch reads as `_op_tool`",
      TD.effective_call("operate", {"tool": " system.restart_app ", "args": {}}, search=True)[0]
      == " system.restart_app ".strip())

# ── 7. the trail, the argument check and refusal faults ─────────────────────
# (a) The trail is what the NEXT turn reads: the persisted chip
# (session.tool_call_chip), read back by memory_engine.recall_tool_trail and
# tool_trail.trail_from_rows — not this turn's in-memory tool_actions. The
# chip's whitelist dropped `read_tool`, so the pick read every door read as
# `read`, a tool in no group, and the trail text never said what was read.
from mastercode.runtime import tool_trail as TTR  # noqa: E402
h = Harness([{"content": "", "tool_calls": [tc("read", {"tool": "workspace_file", "args": {"op": "list"}}, 1)]},
             {"content": "Done.", "tool_calls": [tc("close_turn", CLOSE, 2)]}],
            behavioral={"tool_surface": "search"}).run()
_persisted = [S.tool_call_chip(c) for c in ((h.out or {}).get("tool_actions") or []) if isinstance(c, dict)]
_trail = TTR.trail_from_rows([{"created_at": None, "metadata": {"tool_calls": _persisted}}])
check("trail, persisted: the chip on the turn row keeps read_tool",
      any(c.get("tool") == "read" and c.get("read_tool") == "workspace_file" for c in _persisted), _persisted)
check("trail, persisted: the next turn's pick reads that chip as the tool it read",
      trail_tool_names(_trail, max_turns=3) == ["workspace_file"], trail_tool_names(_trail, max_turns=3))
check("trail, persisted: the trail text names the tool read",
      any("read_tool=workspace_file" in TTR._fmt_call(c) for c in _persisted), [TTR._fmt_call(c) for c in _persisted])
check("trail: two door reads of different tools are not the same target",
      TTR._same_target({"tool": "read", "read_tool": "a"}, {"tool": "read", "read_tool": "b"}) is False
      and TTR._same_target({"tool": "read", "read_tool": "a"}, {"tool": "read", "read_tool": "a"}) is True)

# (b) The argument check answers only a call the walls would let run. A call
# the code-exec wall, the direct-op list, the per-op wall or the turn type
# refuses gets THAT refusal, never "fix these arguments and call again" with
# the schema of a tool the turn may not use.
check("operate check: a code-exec name is left to the code-exec wall",
      CAT.operate_refusal("build", {"bogus": 1}) is None, CAT.operate_refusal("build", {"bogus": 1}))
# DCAT (above) carries the one dispatch-door entry the agent lane leaves;
# the per-op wall's only tool is bug_review (read/verdict are the
# validator agent's), a platform schema, so it rides in a catalog of its own.
_disp2 = next(n for n, e in DCAT.entries.items()
              if e.door == "dispatch" and (e.schema["parameters"].get("properties") or {}))
check("operate check: a tool that is not a registered op is left to the direct-op list",
      DCAT.operate_refusal(_disp2, {"bogus": 1}) is None, (_disp2, DCAT.operate_refusal(_disp2, {"bogus": 1})))
from mastercode.public_tool_registry import OPERATOR_PLATFORM_TOOL_SCHEMAS as _M_REG  # noqa: E402
_br = next(copy.deepcopy(v) for v in _M_REG.values() if (v.get("function") or {}).get("name") == "bug_review")
_br.pop("_meta", None)
_BCAPS = CAPS + [_br]
_BRST, _BRSO = session_sets(_BCAPS)
_BCAT = TD.DoorCatalog(_BCAPS, read_safe_tools=_BRST, read_safe_ops=_BRSO)
check("operate check: an op outside the lane is left to the per-op wall",
      _BCAT.operate_refusal("bug_review", {"op": "verdict", "bogus": 1}) is None,
      _BCAT.operate_refusal("bug_review", {"op": "verdict", "bogus": 1}))
check("operate check: a tool outside the turn type is left to the type's wall (no schema shown)",
      TCAT.operate_refusal("system.restart_app", {"app_id": 5}) is None)
check("operate check: an admitted call is still checked",
      (CAT.operate_refusal("system.restart_app", {"app_id": 5}) or {}).get("reason") == "args_schema")
FILES_ONLY = dict(FILES_TYPE, tool_groups=["files"])
_FCAT = TD.DoorCatalog(CAPS, read_safe_tools=RST, read_safe_ops=RSO, turn_type=FILES_ONLY)
check("read check: a read outside the turn type is left to the type's wall (no schema shown)",
      TD.read_refusal({"tool": "web_search", "args": {"q": 1}}, _FCAT, **_rk) is None)
check("read check: inside the type it is still checked",
      (TD.read_refusal({"tool": "web_search", "args": {"q": 1}}, TCAT, **_rk) or {}).get("reason") == "args_schema")
h = Harness([{"content": "", "tool_calls": [
    tc("operate", {"intent": "Restarting.", "tool": "system.restart_app", "args": {"app_id": 5}}, 1)]},
    {"content": "Done.", "tool_calls": [tc("close_turn", CLOSE, 2)]}],
    behavioral={"tool_surface": "search"}, turn_type=FILES_TYPE).run()
check("typed loop: operate on a tool outside the type reaches the type's wall, not an argument refusal",
      (h.results().get("call_1") or {}).get("reason") != "args_schema"
      and [(n, (k.get("turn_type") or {}).get("name")) for n, _, k in h.direct] == [("system.restart_app", "Files")],
      (h.results().get("call_1"), h.direct))

# (c) The read door reads a mixed tool's op from every selector key the tool
# may declare (direct_ops.excluded_op_refusal reads them the same way), and
# every one it is given must be a read op. kernel_op's read op lives in
# `method`: find_tools lists it on the read door, so the read door must run
# it — and an op key the tool does not read must not make a write a read.
check("read: kernel_op method=GET is a read (its op key is `method`)",
      TD.read_refusal({"tool": "kernel_op", "args": {"method": "GET", "path": "/api/x"}}, CAT, **_rk) is None,
      TD.read_refusal({"tool": "kernel_op", "args": {"method": "GET", "path": "/api/x"}}, CAT, **_rk))
_x = TD.read_refusal({"tool": "kernel_op", "args": {"action": "get", "method": "POST", "path": "/api/x"}},
                     None, **_rk)
check("read: kernel_op action=get method=POST is an action, even with no catalog to check the schema",
      bool(_x) and _x.get("reason") == "not_advertised", _x)
check("read: a second op key carrying a write op makes the call an action",
      TD.read_admits("entity_tasks", {"op": "list", "action": "cancel"}, RST, RSO) is False
      and TD.read_admits("entity_tasks", {"op": "list"}, RST, RSO) is True)
check("read: an op's case does not matter, and no op is no read",
      TD.read_admits("workspace_file", {"op": "LIST"}, RST, RSO) is True
      and TD.read_admits("workspace_file", {}, RST, RSO) is False)
h = Harness([{"content": "", "tool_calls": [
    tc("read", {"tool": "kernel_op", "args": {"method": "GET", "path": "/api/x"}}, 1),
    tc("read", {"tool": "kernel_op", "args": {"action": "get", "method": "POST", "path": "/api/x"}}, 2)]},
    {"content": "Done.", "tool_calls": [tc("close_turn", CLOSE, 3)]}],
    behavioral={"tool_surface": "search"}, catalog=CATALOG_NAMES + ("kernel_op",)).run()
check("loop: read(kernel_op, GET) runs through the held-read path; the POST does not run",
      [(n, a.get("method")) for n, a, _ in h.meta] == [("kernel_op", "GET")], h.meta)
# (c2) The same rule BY NAME, in groups mode too (the by-name branch once
# read one key, so kernel_op action='get' method='POST' ran as
# a read — no intent, past the ephemeral block and the replay lane — while an
# honest kernel_op method='GET' was refused). One admission function judges a
# read on every path now; this is the one deliberate groups-mode change.
h = Harness([{"content": "", "tool_calls": [
    tc("kernel_op", {"method": "GET", "path": "/api/x"}, 1),
    tc("kernel_op", {"action": "get", "method": "POST", "path": "/api/x"}, 2)]},
    {"content": "Done.", "tool_calls": [tc("close_turn", CLOSE, 3)]}],
    catalog=CATALOG_NAMES + ("kernel_op",)).run()
check("groups mode, by name: kernel_op method=GET runs as a read; action=get method=POST does not run",
      [(n, a.get("method")) for n, a, _ in h.meta] == [("kernel_op", "GET")], h.meta)
from mastercode.runtime.session import unseen_lane_may_run as _ulmr  # noqa: E402
check("unseen lanes: kernel_op action=get method=POST is not a read; method=GET is",
      _ulmr("kernel_op", {"action": "get", "method": "POST", "path": "/api/x"}, RST, RSO) is False
      and _ulmr("kernel_op", {"method": "GET", "path": "/api/x"}, RST, RSO) is True
      and _ulmr("entity_tasks", {"op": "list", "action": "cancel"}, RST, RSO) is False
      and _ulmr("entity_tasks", {"op": "list"}, RST, RSO) is True)

# (d) The ledger reads a refusal's stamp: an unstamped error is ERROR, the
# one PLATFORM fault (the ship gate's error baselines count those and can block
# a roll). A group the model named wrong is the model's argument error.
from mastercode.runtime.tool_call_ledger import classify as _tcl_classify, fault_class as _tcl_fault  # noqa: E402
_ng = CAT.find({"group": "nope"})
check("ledger: find_tools on a group that is not there is the model's argument error, not a platform fault",
      _tcl_classify(_ng) == "args_schema" and _tcl_fault(_tcl_classify(_ng), _ng) == "model",
      (_tcl_classify(_ng), _ng.get("reason")))
_doors_res = [CAT.find({}), CAT.find({"query": 5}),
              TD.read_refusal({"tool": "workspace_file", "args": {"op": "write"}}, CAT, **_rk),
              TD.read_refusal({"tool": "system.restart_app", "args": {}}, CAT, **_rk),
              TD.read_refusal({"tool": "no_such_tool"}, CAT, **_rk), TD.read_refusal({}, CAT, **_rk),
              TD.read_refusal({"tool": "build"}, CAT, **_rk)]
check("ledger: every refusal the doors make for a model's call is a model fault",
      all(_tcl_fault(_tcl_classify(r), r) == "model" for r in _doors_res),
      [(r.get("reason"), _tcl_classify(r)) for r in _doors_res])

if FAILS:
    print(f"\ntool-doors: {len(FAILS)} FAIL(s): {FAILS}")
    sys.exit(1)
print("\ntool-doors: all passed")
