#!/usr/bin/env python3
"""The Host turn reads why the model stopped, and a cut is said out loud.

The real EntityRuntime.send_message with a scripted model and a stub pool (the
harness shape of scripts/test_go_ahead_guard.py). No pod, no DB, no model.

  1. the turn asks for the routed model's OWN output limit (claude-opus-5-5:
     128,000), not a fixed 16,384
  2. a tool call the output limit cut is NOT run — the model is told it was
     cut, at what limit, and to write it in parts; the history carries `{}`
     arguments, never the half-written string (it ran as `{}` before)
  3. arguments that are not JSON are not run either, and the model is told
  4. a reply the output limit cut is continued where it stopped
  5. three rounds of failing calls no longer stop the loop with "the tools are
     currently unavailable"; the runaway backstop is far above real work
  6. a failed call that may change something is re-run on a repeat, not
     served its cached failure
  7. the woken-turn chain and research rounds are high backstops, env-set;
     the person's answer reaches the turn whole; a round's agent reports are
     sized from the window

Exit 0 = all pass; exit 1 + FAIL lines otherwise.
"""

import asyncio
import json
import logging
import os
import sys
from turn_source import sync_turn_attr, turn_source  # session.py and the modules split out of it

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
        print(f"[turn-cut] FAIL {name} {extra}")


from mastercode.runtime import session as S  # noqa: E402
from mastercode import provider_gateway as PG  # noqa: E402
from mastercode import inference_config as IC  # noqa: E402
from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS as P_REG  # noqa: E402
from server.routes import inference as INF  # noqa: E402

INF._CATALOG_CACHE["data"] = {"openrouter_models": [], "total_openrouter": 0}
INF._CATALOG_CACHE["exp"] = 1e18   # cold, and not to be fetched: the static table answers


class Pool:
    def __init__(self):
        self.executed: list = []

    async def fetch(self, q, *a):
        return []

    async def fetchrow(self, q, *a):
        return None

    async def fetchval(self, q, *a):
        return None

    async def execute(self, q, *a):
        self.executed.append((q, a))
        return "OK"

    def acquire(self):
        pool = self

        class _C:
            async def __aenter__(self):
                return pool

            async def __aexit__(self, *e):
                return False
        return _C()


def _tc(name, args, i=1, raw=None):
    return {"id": f"call_{name}_{i}", "type": "function",
            "function": {"name": name, "arguments": raw if raw is not None else json.dumps(args)}}


class Harness:
    def __init__(self, script, meta_results=None, wb=False):
        self.wb = wb
        self.script = list(script)
        self.calls: list[list[dict]] = []
        self.kw: list[dict] = []
        self.meta_runs: list[tuple] = []
        self.meta_results = dict(meta_results or {})
        self.pool = Pool()

    async def model(self, messages=None, *a, **kw):
        msgs = messages if messages is not None else (a[0] if a else [])
        self.calls.append([dict(m) for m in msgs])
        self.kw.append({k: v for k, v in kw.items() if k != "stream_cb"})
        r = self.script.pop(0) if self.script else {"content": "(the script ran out)"}
        cb = kw.get("stream_cb")
        if cb and r.get("content"):
            await cb(r["content"])
        return {"tokens_used": 1, "prompt_tokens": 1, "completion_tokens": 1,
                "tool_calls": None, **r}

    async def run(self, message="Design the desktop."):
        S._pending_nudges.clear()
        S._turn_events.clear()
        S._task_holds.clear()
        saved = (S._call_entity_model, PG.call_model, IC.resolve_role_route,
                 S._AVAILABLE.get("ceigas_domain_controller"), S._run_close_turn,
                 S._execute_meta_tool, S._build_tool_schemas)

        async def route(pool, eid, role, thread_override=None, **kw):
            return {"provider": "anthropic", "model": "claude-opus-5-5"}

        async def run_close_turn(pool, sess, args, *, prompt_console=None):
            return {"status": "ok", "result": "recorded"}

        async def meta(name, args, **kw):
            self.meta_runs.append((name, dict(args or {})))
            seq = self.meta_results.get(name)
            if isinstance(seq, list) and seq:
                return seq.pop(0)
            return {"status": "ok", "result": {"task_id": 999, "state": "running"}}

        async def schemas(pool, eid, interaction_context=None, **kw):
            return [{"type": "function", "function": P_REG["build"]["function"]}]

        S._call_entity_model = self.model
        PG.call_model = self.model
        IC.resolve_role_route = route
        S._AVAILABLE["ceigas_domain_controller"] = None
        S._run_close_turn = run_close_turn
        S._execute_meta_tool = meta
        S._build_tool_schemas = schemas
        rt = S.EntityRuntime(self.pool)
        if getattr(self, "fake_dispatch", None) is not None:
            rt.agent_dispatch = self.fake_dispatch

        async def ctx(entity_id, **kw):
            return S.EntitySession(
                session_id="sess-cut", entity_id=entity_id,
                entity={"id": entity_id, "name": "Sage", "behavioral_params": {},
                        "permission_overrides": {"bot_capabilities": 255}},
                interaction_context="project:251" if self.wb else "app:circles")
        rt._build_chat_context_transient = ctx
        try:
            self.out = await rt.send_message(
                entity_id=9003, user_message=message,
                **({"project_id": 251, "app_id": "workbench"} if self.wb else {}))
            await asyncio.sleep(0.05)   # let scheduled background writes land
        finally:
            (S._call_entity_model, PG.call_model, IC.resolve_role_route,
             S._AVAILABLE["ceigas_domain_controller"], S._run_close_turn,
             S._execute_meta_tool, S._build_tool_schemas) = saved
        return self


def tool_msgs(h, i):
    return [m for m in (h.calls[i] if len(h.calls) > i else []) if m.get("role") == "tool"]


def reply_of(h):
    o = h.out if isinstance(h.out, dict) else {}
    return str(o.get("content") or o.get("response") or o.get("reply") or "")


# 1 + 2: the cut tool call
CUT_ARGS = '{"action": "start", "brief": "<div class=\\"desk'
h = asyncio.run(Harness([
    {"content": "", "tool_calls": [_tc("build", None, raw=CUT_ARGS)],
     "raw_message": {"content": "", "tool_calls": [_tc("build", None, raw=CUT_ARGS)]},
     "stop_reason": "max_tokens", "truncated": True, "output_limit": 128000,
     "truncated_tool_call_ids": ["call_build_1"]},
    {"content": "I will write the design in parts."},
], wb=True).run())
check("1 the turn asks for claude-opus-5-5's own output limit (128,000), not 16,384",
      h.kw and h.kw[0].get("max_tokens") == 128000, [k.get("max_tokens") for k in h.kw])
check("2a the cut call is NOT run", not h.meta_runs, h.meta_runs)
_tm = tool_msgs(h, 1)
_t = _tm[-1]["content"] if _tm else ""
check("2b the model is told it was cut, at what limit, and to write it in parts",
      "cut off" in _t and "128000" in _t and "in parts" in _t, _t[:300])
check("2c it is never told the call was wrong ('requires') — the cut is not its mistake",
      "requires" not in _t and "not a mistake" in _t, _t[:300])
_asst = [m for m in h.calls[1] if m.get("role") == "assistant" and m.get("tool_calls")] if len(h.calls) > 1 else []
check("2d the history carries the cut call with `{}` arguments, not the half-written string",
      _asst and _asst[-1]["tool_calls"][0]["function"]["arguments"] == "{}", _asst[-1:] if _asst else None)

# 3: arguments that are not JSON, no cut
h = asyncio.run(Harness([
    {"content": "", "tool_calls": [_tc("build", None, raw="{not json")],
     "raw_message": {"content": "", "tool_calls": [_tc("build", None, raw="{not json")]}},
    {"content": "Fixed."},
], wb=True).run())
_tm = tool_msgs(h, 1)
_t = _tm[-1]["content"] if _tm else ""
check("3a a call whose arguments are not JSON is not run (it ran as {} before)", not h.meta_runs, h.meta_runs)
check("3b ...and the model is told the arguments were not valid JSON", "not valid JSON" in _t, _t[:200])

# A cut completion whose tool block DID close (so the gateway named no
# id) but whose arguments do not parse is still a cut — never "not valid JSON,
# send it again", which counted toward the breaker.
h = asyncio.run(Harness([
    {"content": "", "tool_calls": [_tc("build", None, raw=CUT_ARGS)],
     "raw_message": {"content": "", "tool_calls": [_tc("build", None, raw=CUT_ARGS)]},
     "stop_reason": "max_tokens", "truncated": True, "output_limit": 128000,
     "truncated_tool_call_ids": []},
    {"content": "Writing it in parts."},
], wb=True).run())
_tm = tool_msgs(h, 1)
_t = _tm[-1]["content"] if _tm else ""
check("N1 a truncated completion with unparseable arguments is a CUT whichever way the block closed",
      not h.meta_runs and "cut off" in _t and "not valid JSON" not in _t, _t[:200])

# The Host's own model stream is a sign of life — every call carries
# on_alive, and it reaches the turn registry with the turn's session.
import server.turn_registry as _TRG  # noqa: E402
_alive_sids: list = []
_na_saved = _TRG.note_alive
_TRG.note_alive = lambda sid: _alive_sids.append(sid)
try:
    h = asyncio.run(Harness([{"content": "Done."}]).run())
    _oa = h.kw[0].get("on_alive") if h.kw else None
    _alive_sids.clear()          # the loop's own round marks land here too
    if callable(_oa):
        _oa()
finally:
    _TRG.note_alive = _na_saved
check("N2 every model call of the turn carries on_alive, and it marks the turn alive",
      callable(_oa) and _alive_sids == ["sess-cut"], (_oa, _alive_sids))

# While the Host's tools run, the wall's window is the routed model's
# (claude-opus-5-5: 1,000,000) — TOOL_WALL_WINDOW, stood in here.
import contextvars as _cv  # noqa: E402
from mastercode.runtime import tool_result_wall as _TRW  # noqa: E402
_had_var, _saved_var = hasattr(_TRW, "TOOL_WALL_WINDOW"), getattr(_TRW, "TOOL_WALL_WINDOW", None)
_TRW.TOOL_WALL_WINDOW = _cv.ContextVar("tool_wall_window_turn_test", default=None)
_seen_wall: list = []
try:
    _hw = Harness([
        {"content": "", "tool_calls": [_tc("build", {"action": "start", "project_id": 251, "brief": "w"})],
         "raw_message": {"content": "", "tool_calls": [_tc("build", {"action": "start", "project_id": 251, "brief": "w"})]}},
        {"content": "Started."}],
        meta_results={"build": [{"status": "error", "result": "x"}]}, wb=True)
    _orig_meta_results = _hw.meta_results

    class _Peek(list):
        def pop(self, i=-1):
            _seen_wall.append(_TRW.TOOL_WALL_WINDOW.get())
            return super().pop(i)
    _hw.meta_results = {"build": _Peek(_orig_meta_results["build"])}
    asyncio.run(_hw.run())
finally:
    if _had_var:
        _TRW.TOOL_WALL_WINDOW = _saved_var
    else:
        delattr(_TRW, "TOOL_WALL_WINDOW")
check("H7 a Host tool runs with the wall's window set to the routed model's (1,000,000)",
      _seen_wall == [1_000_000], _seen_wall)

# 4: a cut reply continues
h = asyncio.run(Harness([
    {"content": "Part one of a very long answer, cut mid-", "stop_reason": "max_tokens",
     "truncated": True, "output_limit": 128000},
    {"content": "sentence and the rest of it."},
]).run())
check("4a a cut reply is continued with one more model call", len(h.calls) == 2, len(h.calls))
_last = h.calls[1][-1] if len(h.calls) > 1 else {}
check("4b the continuation names the cut and the limit",
      _last.get("role") == "user" and "cut off" in str(_last.get("content"))
      and "128000" in str(_last.get("content")), _last)
check("4c the model sees what it already wrote",
      len(h.calls) > 1 and h.calls[1][-2].get("role") == "assistant"
      and "Part one" in str(h.calls[1][-2].get("content")))
check("4d the stored reply joins the two parts EXACTLY at the cut — no blank line mid-word",
      reply_of(h) == "Part one of a very long answer, cut mid-sentence and the rest of it.",
      repr(reply_of(h)[:200]))

# 5: three all-failed rounds do not stop the loop with a false "unavailable"
ERR = {"status": "error", "result": "brief is missing a project"}
h = asyncio.run(Harness(
    [{"content": "", "tool_calls": [_tc("build", {"action": "start", "brief": f"b{i}"}, i)],
      "raw_message": {"content": "", "tool_calls": [_tc("build", {"action": "start", "brief": f"b{i}"}, i)]}}
     for i in range(1, 5)] + [{"content": "Stopping here: the build door keeps refusing."}],
    meta_results={"build": [dict(ERR) for _ in range(4)]}, wb=True).run())
_all = " ".join(str(m.get("content")) for c in h.calls for m in c)
check("5a four rounds where every call failed still reach the model with its tools",
      len(h.meta_runs) == 4, (len(h.meta_runs), len(h.calls)))
check("5b nothing tells the model the tools are 'currently unavailable'",
      "currently unavailable" not in _all)
check("5c the breaker is a runaway backstop, env-set (>= 20 rounds)", S.ALL_FAIL_BACKSTOP_ROUNDS >= 20,
      getattr(S, "ALL_FAIL_BACKSTOP_ROUNDS", None))

# 6: a failed call that may change something is re-run on an identical repeat
h = asyncio.run(Harness(
    [{"content": "", "tool_calls": [_tc("build", {"action": "start", "brief": "same"}, i)],
      "raw_message": {"content": "", "tool_calls": [_tc("build", {"action": "start", "brief": "same"}, i)]}}
     for i in (1, 2)] + [{"content": "The second try failed differently; reporting both."}],
    meta_results={"build": [dict(ERR), {"status": "error", "result": "quota reached for today"}]},
    wb=True).run())
_t2 = tool_msgs(h, 2)
check("6a a repeat after a failure runs again (a transient error is not served from cache)",
      len(h.meta_runs) == 2, h.meta_runs)
check("6b ...and the model reads the new outcome, not the cached first failure",
      _t2 and "quota reached" in _t2[-1]["content"]
      and "Cached from this turn" not in _t2[-1]["content"], _t2[-1:] if _t2 else None)

# 6c-6e: a READ repeated with the same args re-runs; it is never
# served the stale first answer. agent_status is read-safe
# (tool_access.is_read_safe_tool) and its state changes between polls.
class _FakeDispatch:
    def __init__(self, states):
        self.states = list(states)
        self.reads = 0

    async def list_agents(self, eid, limit=10):
        self.reads += 1
        st = self.states.pop(0) if self.states else "completed"
        return [{"task_id": 7, "status": st, "title": "Research 7"}]

    async def model_stats(self, eid):
        return {}

_poll = [{"content": "", "tool_calls": [_tc("agent_status", {"task_id": 7}, i)],
          "raw_message": {"content": "", "tool_calls": [_tc("agent_status", {"task_id": 7}, i)]}}
         for i in (1, 2, 3)]
_hp = Harness(_poll + [{"content": "Agent 7 finished."}], wb=True)
_hp.fake_dispatch = _FakeDispatch(["running", "completed", "completed"])
asyncio.run(_hp.run())
_t_r2 = (tool_msgs(_hp, 2) or [{}])[-1].get("content") or ""
_t_r3 = (tool_msgs(_hp, 3) or [{}])[-1].get("content") or ""
check("6c a repeated poll re-runs the read every time (3 identical agent_status calls, 3 reads)",
      _hp.fake_dispatch.reads == 3, _hp.fake_dispatch.reads)
check("6d ...the second call sees the NEW state ('completed'), not the cached 'running'",
      '"completed"' in _t_r2 and "Cached from this turn" not in _t_r2, _t_r2[:300])
check("6e ...and a read that returns exactly what it returned before says nothing changed",
      "unchanged_since_round" in _t_r3 and "nothing it reads has changed" in _t_r3, _t_r3[:300])

# H3 (PL-18): a failing tool call in the loop lands an error_events row — the
# recorder sat inside `if _prompt_console:`, which nothing turns on.
_he = Harness([{"content": "", "tool_calls": [_tc("build", {"action": "start", "project_id": 251, "brief": "x"}, 1)],
                "raw_message": {"content": "", "tool_calls": [_tc("build", {"action": "start", "project_id": 251, "brief": "x"}, 1)]}},
               {"content": "The build door refused it."}],
              meta_results={"build": [{"status": "error", "result": "the build pod did not start: upstream 503"}]}, wb=True)
asyncio.run(_he.run())
_ev_rows = [a for q, a in _he.pool.executed if "INSERT INTO error_events" in q]
check("H3 a failing tool call in the loop writes an error_events row (the error lane is live)",
      _ev_rows and any("build" in str(a) for a in _ev_rows), _he.pool.executed[-3:])


def _faults(rows):
    out = []
    for a in rows:
        try:
            out.append(json.loads(a[-1]).get("fault"))
        except Exception:  # noqa: BLE001
            out.append(None)
    return out

check("H3b a tool that ran and failed is a PLATFORM fault on its row",
      _faults(_ev_rows) == ["platform"], _faults(_ev_rows))
# A model-side outcome: arguments that are not JSON (not run).
_hm = Harness([{"content": "", "tool_calls": [_tc("build", None, raw="{bad")],
                "raw_message": {"content": "", "tool_calls": [_tc("build", None, raw="{bad")]}},
               {"content": "Resent."}], wb=True)
asyncio.run(_hm.run())
_mrows = [a for q, a in _hm.pool.executed if "INSERT INTO error_events" in q]
check("H3c bad arguments land as a MODEL fault — never a platform error that blocks a roll",
      _mrows and set(_faults(_mrows)) == {"model"}, _faults(_mrows))
# A tool that answers a NON-failure status other than "ok" —
# dispatch_agent's "dispatched", schedule_task's "scheduled" — writes NO error
# row, with or without the error recorder in the tree. The end-to-end test
# runs dispatch before it checks for errors.
for _st_name, _st_res in (("dispatched", {"status": "dispatched", "task_id": 7,
                                           "note": "running in the background"}),
                          ("scheduled", {"status": "scheduled"}),
                          ("not_found", {"status": "not_found", "result": "No state"})):
    _hd = Harness([
        {"content": "", "tool_calls": [_tc("build", {"action": "start", "brief": "b"})],
         "raw_message": {"content": "", "tool_calls": [_tc("build", {"action": "start", "brief": "b"})]}},
        {"content": "Started."}], meta_results={"build": [dict(_st_res)]}, wb=True)
    asyncio.run(_hd.run())
    _d_rows = [a for q, a in _hd.pool.executed if "INSERT INTO error_events" in q]
    check(f"H3g a '{_st_name}' outcome writes no error row (it was a platform fault without the recorder)",
          not _d_rows, _d_rows)
from mastercode.runtime.tool_call_ledger import fault_class as _fc  # noqa: E402
check("H3d the classes: only a call that ran and failed is the platform's",
      _fc("error") == "platform" and all(_fc(r) == "model" for r in (
          "dedup", "bad_args_json", "args_schema", "unknown_name", "not_advertised",
          "surface_denied", "capability_denied", "out_of_grant", "policy_denied", "blocked")))


# The read side: ?fault= filters, and no ?fault keeps every row.
class _QPool:
    def __init__(self):
        self.q = None
        self.args = None

    async def fetch(self, q, *a):
        self.q, self.args = q, a
        return []

from server.routes import system as _SYS  # noqa: E402
from server import brain_service as _BS  # noqa: E402
_qp = _QPool()
_gp_saved = _BS.get_pool
_BS.get_pool = lambda: _qp
try:
    asyncio.run(_SYS.errors_recent(limit=200, fault="platform"))
    _q_plat, _a_plat = _qp.q, _qp.args
    asyncio.run(_SYS.errors_recent(limit=200))
    _q_all = _qp.q
finally:
    _BS.get_pool = _gp_saved
check("H3e GET /api/system/errors/recent?fault=platform filters by class (an unclassed row reads as platform)",
      "COALESCE(context->>'fault', 'platform') =" in (_q_plat or "") and "platform" in (_a_plat or ()),
      (_q_plat, _a_plat))
check("H3f ...and without ?fault every row is returned, as before", "fault" not in (_q_all or ""), _q_all)

# H2b: recall always sends limit and offset — a first page
# (the model gave no offset) is a paged call too, so the engine reports the
# total and offers the next offset instead of "narrow the query".
import mastercode.memory_retrieval as _MR  # noqa: E402
_rcalls: list = []


async def _fake_recall(pool, eid, query, *, scope="all", limit=10, offset=None, context=None, **kw):
    _rcalls.append({"limit": limit, "offset": offset})
    return {"stores": [], "total": 0}

_mr_saved = (_MR.federated_recall, _MR.format_recall_text)
_MR.federated_recall = _fake_recall
_MR.format_recall_text = lambda fed: "no hits"
try:
    asyncio.run(Harness([
        {"content": "", "tool_calls": [_tc("recall", {"query": "desktop palette"}, 1)],
         "raw_message": {"content": "", "tool_calls": [_tc("recall", {"query": "desktop palette"}, 1)]}},
        {"content": "", "tool_calls": [_tc("recall", {"query": "desktop palette", "offset": 10, "limit": 5}, 2)],
         "raw_message": {"content": "", "tool_calls": [_tc("recall", {"query": "desktop palette", "offset": 10, "limit": 5}, 2)]}},
        {"content": "Found nothing more."}], wb=True).run())
finally:
    _MR.federated_recall, _MR.format_recall_text = _mr_saved
check("H2b a first recall (no offset given) is sent as page one: offset=0 and the limit",
      _rcalls[:1] == [{"limit": 10, "offset": 0}], _rcalls)
check("H2c ...and a later page carries the model's offset and limit",
      len(_rcalls) > 1 and _rcalls[1] == {"limit": 5, "offset": 10}, _rcalls)

# 7: the backstops and the person's words
check("7a the woken-turn chain backstop sits far above the longest real chain (3)",
      S.WOKEN_TURNS_MAX >= 30, S.WOKEN_TURNS_MAX)
check("7b research rounds: a high backstop (>= 3x the normal three)", S.RESEARCH_ROUNDS_MAX >= 9,
      S.RESEARCH_ROUNDS_MAX)
check("7c both are env-set and <= 0 disables (the round backstop's rule)",
      S._backstop_env("X_UNSET_FOR_TEST", 7) == 7 and (
          os.environ.__setitem__("X_CAP_TEST", "0") or S._backstop_env("X_CAP_TEST", 7) >= 10**6))
S._pending_nudges.clear()
S._live_turns = getattr(S, "_live_turns", None)
sync_turn_attr(S, "_live_turns")
_long = "x" * 5000
_orig_live = S.turn_is_live
S.turn_is_live = lambda sid: True
sync_turn_attr(S, "turn_is_live")
try:
    S.answer_into_turn("sess-ans", 7, _long)
    _got = [t for t, _ts in S.consume_nudges_timed("sess-ans")]
finally:
    S.turn_is_live = _orig_live
    sync_turn_attr(S, "turn_is_live")
check("7d an answer into a running turn arrives whole (it was cut at 2,000)",
      _got == [_long], [len(x) for x in _got])
check("7e the 2,000-character answer cut is gone", not hasattr(S, "ANSWER_MAX_CHARS"))
_tk = open(os.path.join(APP_ROOT, "server/routes/tickets.py")).read()
check("7f the ticket decide sends the person's note whole", "ANSWER_MAX_CHARS" not in _tk)
_br = open(os.path.join(APP_ROOT, "server/routes/brain.py")).read()
# The route's whole body, to the next route (its docstring grew when the
# server began holding the words).
_bn = _br.split("async def brain_turn_nudge")[1].split("\n@router.")[0]
check("7f2 the Workbench nudge route sends the person's words whole (it cut them at 2,000)",
      "[:2000]" not in _bn and '.get("text") or "").strip()' in _bn)
_mtl = turn_source(APP_ROOT)
_mtl = _mtl[_mtl.index("def mark_turn_live"):][:900]
check("7f3 each round of the tool loop is a sign of life to the turn registry (T-9)",
      "note_alive(session_id)" in _mtl)


class _RPool(Pool):
    pass


async def _report_chars():
    rt = S.EntityRuntime(Pool())
    saved = IC.resolve_role_route

    async def route(pool, eid, role, **kw):
        return {"provider": "anthropic", "model": "claude-opus-5-5"}
    IC.resolve_role_route = route
    try:
        return await rt._round_report_chars(3, 2)
    finally:
        IC.resolve_role_route = saved
_rc = asyncio.run(_report_chars()) if hasattr(S.EntityRuntime, "_round_report_chars") else 16000
check("7g a round's agent reports are sized from the window (1M window, 2 reports: 500,000 chars each)",
      _rc == 500000, _rc)

if FAILS:
    print(f"\n{len(FAILS)} FAIL")
    sys.exit(1)
print("\nall pass")
