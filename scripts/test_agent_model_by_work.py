#!/usr/bin/env python3
"""An agent's model comes from the Host's preference for the kind of untrusted
work, a banned model never starts an agent, and every model call an agent
makes names its task.

A Host needs an agent only to handle untrusted operations, so the Host should
hold a model preference for each class of that work. An agent that named no
model ran on the Host's own route; nothing let the person or the Host choose the model for
that kind of work, and its model calls were logged with no task id.

  A  one class set (mastercode/agent_work.WORK_CLASSES): the dispatch verb's
     `work` and the preference tool's `work` are that set.
  B  the preference tool (tools._exec_preference): op='prefer' stores the
     model for a kind of work with who set it (person or host), op='list'
     shows it, op='unprefer' removes it; an unknown kind and a banned model
     are refused.
  C  the dispatch door, through the real EntityRuntime.send_message loop:
     no model named -> the preferred model, and the choice rides to the
     dispatcher; unset -> the Host's own route; a named model wins; a banned
     model, named or preferred, is refused before the dispatcher is called;
     an unknown kind is refused.
  D  run_scoped_agent refuses a named banned model before any model call
     (the wall for every caller).
  E  the choice is seeded on the agent's task record and the Agents API row
     carries it (with its label).
  F  each model call an agent makes carries its task id: scoped_agent passes
     it, the gateway writes it into every inference_cost_log row of that call
     (success and failure) and no other, and background activity names the
     agent.

No pod, no DB, no model. Run: APP_ROOT=<repo> python3 scripts/test_agent_model_by_work.py
"""
import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

APP_ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_ROOT)
sys.path.insert(0, os.path.join(APP_ROOT, "brain"))
logging.disable(logging.CRITICAL)

FAILS: list[str] = []


def check(name, cond, extra=""):
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"[agent-model-by-work] FAIL {name} {str(extra)[:600]}")


def tc(name, args, i):
    return {"id": f"call_{i}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


class Pool:
    def __init__(self):
        self.executed: list = []
        self.rows: list = []

    async def fetch(self, q, *a):
        return list(self.rows) if "inference_cost_log" in q else []

    async def fetchrow(self, q, *a):
        return None

    async def fetchval(self, q, *a):
        return 0 if "COUNT" in q else None

    async def execute(self, q, *a):
        self.executed.append((q, a))
        return "UPDATE 1"

    def acquire(self):
        pool = self

        class _C:
            async def __aenter__(self):
                return pool

            async def __aexit__(self, *e):
                return False
        return _C()


class FakeDispatch:
    def __init__(self):
        self.calls: list[dict] = []

    async def dispatch(self, **kw):
        self.calls.append(kw)
        return {"task_id": 501, "status": "running", "session_id": "agent:501"}

    async def project_has_plan(self, project_id):
        return True

    async def mark_round(self, ids):
        return None


def _dispatch_args(**extra):
    # A web read: the scope reaches untrusted content, which the dispatch door
    # requires since 2026-10-12 (an empty scope was accepted before).
    a = {"intent": "I'm sending an agent to read that page.",
         "task": "Read the page and report its title.",
         "tool_scope": ["read_url"], "grant": ["external_api"],
         "system_prompt": "You read one web page and report its title."}
    a.update(extra)
    return a


def run_turn(dispatch_args):
    """One real turn whose model dispatches once, then closes. Returns
    (dispatcher calls, the dispatch call's tool result as the model saw it)."""
    from mastercode.runtime import session as S
    from mastercode.runtime import scoped_agent as SA
    from mastercode.runtime import tool_core as TC
    from mastercode import provider_gateway as PG
    from mastercode import inference_config as IC
    seen: list = []
    script = [{"content": "Sending an agent.", "tool_calls": [tc("dispatch", dispatch_args, 1)]}]

    async def model(messages=None, *a, **kw):
        seen.append([dict(m) for m in (messages or [])])
        r = script.pop(0) if script else {
            "content": "Done.",
            "tool_calls": [tc("close_turn", {"topic": "agent", "handoff": {"next_step": "none"}}, 9)]}
        return {"tokens_used": 1, "prompt_tokens": 1, "completion_tokens": 1, "tool_calls": None, **r}

    async def route(pool, eid, role, thread_override=None, **k):
        return {"provider": "anthropic", "model": "fake-model"}

    async def run_close_turn(pool, sess, args, *, prompt_console=None):
        return {"status": "ok", "result": "recorded"}

    async def meta(name, args, **k):
        return {"status": "ok", "result": f"{name} ran"}

    async def schemas(pool, eid, interaction_context=None, **k):
        return []

    saved = (S._call_entity_model, PG.call_model, IC.resolve_role_route, SA.resolve_role_route,
             S._AVAILABLE.get("ceigas_domain_controller"), S._run_close_turn,
             S._execute_meta_tool, S._build_tool_schemas, SA._build_tool_schemas)
    S._call_entity_model = model
    PG.call_model = model
    IC.resolve_role_route = route
    SA.resolve_role_route = route
    S._AVAILABLE["ceigas_domain_controller"] = None
    S._run_close_turn = run_close_turn
    S._execute_meta_tool = meta
    S._build_tool_schemas = schemas
    SA._build_tool_schemas = schemas
    TC._USE_CACHE.clear()
    # The turn holds at its exit for the agent it started (session.HOLD_S);
    # the fake dispatcher never reports, so hold briefly.
    saved_hold, S.HOLD_S = S.HOLD_S, 0.3
    disp = FakeDispatch()
    rt = S.EntityRuntime(Pool())
    rt.agent_dispatch = disp

    async def ctx(entity_id, **k):
        return S.EntitySession(
            session_id="sess-work", entity_id=entity_id,
            entity={"id": entity_id, "name": "Host", "behavioral_params": {"tool_surface": "search"},
                    "permission_overrides": {"bot_capabilities": 255}},
            interaction_context="chat")
    rt._build_chat_context_transient = ctx
    try:
        asyncio.run(rt.send_message(entity_id=9003, user_message="What is that page called?"))
    finally:
        (S._call_entity_model, PG.call_model, IC.resolve_role_route, SA.resolve_role_route,
         S._AVAILABLE["ceigas_domain_controller"], S._run_close_turn,
         S._execute_meta_tool, S._build_tool_schemas, SA._build_tool_schemas) = saved
        S.HOLD_S = saved_hold
    result = None
    for msgs in seen[1:]:
        for m in msgs:
            if m.get("role") == "tool" and m.get("tool_call_id") == "call_1":
                try:
                    result = json.loads(m["content"])
                except (TypeError, ValueError):
                    result = {"raw": m["content"]}
                break
        if result is not None:
            break
    return disp.calls, result or {}


def main() -> int:
    try:
        from mastercode import agent_work as AW
    except Exception as e:  # noqa: BLE001 — before: no class set at all
        AW = None
        check("mastercode/agent_work.py exists (one class set)", False, e)
    from server.routes import _preference_profile as pp
    from mastercode.runtime import session as S
    from mastercode.runtime import tools as T
    from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS as P_REG

    tmp = Path(tempfile.mkdtemp(prefix="work-pref-"))
    pp.PROFILES_ROOT = tmp

    def reset():
        for f in tmp.glob("*.json"):
            f.unlink()

    try:
        # ── A0: the import order the image build uses ──────────────────────
        print("A0. the registry imported first still builds the Host's verbs")
        try:
            import subprocess
            r = subprocess.run(
                [sys.executable, "-c",
                 "import mastercode.public_tool_registry\n"
                 "from mastercode.runtime import session as S\n"
                 "print('verbs', len(S._orchestration_verbs()))"],
                cwd=os.path.join(APP_ROOT, "brain"), capture_output=True, text=True, timeout=240,
                env={**os.environ, "PYTHONPATH": f"{APP_ROOT}:{os.path.join(APP_ROOT, 'brain')}"})
            check("A0 importing public_tool_registry before the runtime package leaves close_turn "
                  "in the Host's tool table (generate_capabilities' order)",
                  r.returncode == 0 and "verbs" in r.stdout, (r.returncode, r.stderr[-400:]))
        except Exception as e:  # noqa: BLE001 — a part that cannot run fails
            check("A0 ran", False, repr(e))

        # ── A ────────────────────────────────────────────────────────────────
        try:
            print("A. one class set")
            disp_schema = next(v for v in S._orchestration_verbs()
                               if (v.get("function") or {}).get("name") == "dispatch")
            dprops = disp_schema["function"]["parameters"]["properties"]
            # Required (the Host calls an agent only for untrusted work): a
            # dispatch names the content it reads.
            check("A1 the dispatch verb's `work` is the class set, and required",
                  (dprops.get("work") or {}).get("enum") == list(AW.WORK_CLASSES)
                  and "work" in disp_schema["function"]["parameters"].get("required", []),
                  dprops.get("work"))
            check("A1b the dispatch verb no longer offers agents 'for parallel work'",
                  "parallel" not in disp_schema["function"]["description"],
                  disp_schema["function"]["description"])
            check("A1c agent_work refuses a missing `work`, and accepts each class",
                  AW.work_refusal(None) is not None and AW.work_refusal("") is not None
                  and all(AW.work_refusal(k) is None for k in AW.WORK_CLASSES),
                  AW.work_refusal(None))
            pprops = P_REG["preference"]["function"]["parameters"]["properties"]
            check("A2 the preference tool takes prefer/unprefer, and its `work` is the same set",
                  {"prefer", "unprefer"} <= set(pprops["op"]["enum"])
                  and (pprops.get("work") or {}).get("enum") == list(AW.WORK_CLASSES)
                  and (pprops.get("by") or {}).get("enum") == ["person", "host"], sorted(pprops))
        except Exception as e:  # noqa: BLE001 — a part that cannot run fails
            check("A ran", False, repr(e))

        # ── B ────────────────────────────────────────────────────────────────
        try:
            print("B. the preference tool stores it, in the open")
            reset()
            r1 = asyncio.run(T._exec_preference({"op": "prefer", "work": "web",
                                                 "target": "acme/web-reader", "by": "person",
                                                 "text": "use the cheap reader for web pages"}))
            r2 = asyncio.run(T._exec_preference({"op": "prefer", "work": "message",
                                                 "target": "acme/msg-reader"}))
            lst = asyncio.run(T._exec_preference({"op": "list"}))
            wm = ((lst.get("result") or {}).get("work_models") or {})
            check("B1 op='prefer' by the person is stored with their words; op='list' shows it",
                  r1.get("status") == "ok" and wm.get("web", {}).get("model") == "acme/web-reader"
                  and wm.get("web", {}).get("by") == "person"
                  and wm.get("web", {}).get("text") == "use the cheap reader for web pages", (r1, wm))
            check("B2 set by the Host (the default) says so",
                  r2.get("status") == "ok" and wm.get("message", {}).get("by") == "host", (r2, wm))
            bad = asyncio.run(T._exec_preference({"op": "prefer", "work": "email",
                                                  "target": "acme/x"}))
            asyncio.run(T._exec_preference({"op": "ban", "target": "nova", "subject": "model"}))
            banned = asyncio.run(T._exec_preference({"op": "prefer", "work": "foreign_repo",
                                                     "target": "amazon/nova-pro"}))
            check("B3 an unknown kind of work and a banned model are refused",
                  bad.get("status") == "error" and "web" in str(bad.get("result"))
                  and banned.get("status") == "error" and "banned" in str(banned.get("result")),
                  (bad, banned))
            un = asyncio.run(T._exec_preference({"op": "unprefer", "work": "message"}))
            wm2 = ((asyncio.run(T._exec_preference({"op": "list"})).get("result") or {})
                   .get("work_models") or {})
            check("B4 op='unprefer' removes it", un.get("status") == "ok" and "message" not in wm2
                  and "web" in wm2, (un, wm2))
        except Exception as e:  # noqa: BLE001 — a part that cannot run fails
            check("B ran", False, repr(e))

        # ── C0: a ban at the door, with no preference involved ──────────────
        print("C0. a banned model named on a dispatch (a real turn)")
        try:
            reset()
            pp.record_stated(None, polarity="ban", target="acme/banned-reader", subject="model")
            calls, res = run_turn(_dispatch_args(model="acme/banned-reader", work="web"))
            check("C0 the `preference` tool's promise ('ban' refuses a model at dispatch) holds for "
                  "agents: no dispatch, and the reason names the ban",
                  not calls and res.get("status") == "error" and "banned" in str(res.get("result")),
                  (calls, res))
        except Exception as e:  # noqa: BLE001 — a part that cannot run fails
            check("C0 ran", False, repr(e))

        # ── C ────────────────────────────────────────────────────────────────
        try:
            print("C. the dispatch door (a real turn)")
            reset()
            pp.set_work_model("web", "acme/web-reader", by="person", text="cheap reader")
            calls, res = run_turn(_dispatch_args(work="web"))
            kw = calls[0] if calls else {}
            mc = kw.get("model_choice") or {}
            check("C1 no model named: the agent runs on the model preferred for the work, and the "
                  "choice (which preference, set by whom) rides to the dispatcher",
                  len(calls) == 1 and kw.get("model") == "acme/web-reader"
                  and mc.get("from") == "preference" and mc.get("work") == "web"
                  and (mc.get("preference") or {}).get("by") == "person"
                  and mc.get("label") == "chosen by your preference for web work", (calls, res))
            calls, res = run_turn(_dispatch_args(work="foreign_repo"))
            kw = calls[0] if calls else {}
            check("C2 no preference for the work: the Host's own route (model None), and it says so",
                  len(calls) == 1 and kw.get("model") is None
                  and (kw.get("model_choice") or {}).get("from") == "own_route", (calls, res))
            calls, res = run_turn(_dispatch_args(work="web", model="acme/named-model"))
            kw = calls[0] if calls else {}
            check("C3 a model the dispatch names wins over the preference",
                  len(calls) == 1 and kw.get("model") == "acme/named-model"
                  and (kw.get("model_choice") or {}).get("from") == "dispatch", (calls, res))
            pp.record_stated(None, polarity="ban", target="web-reader", subject="model",
                             text="that reader leaks")
            calls, res = run_turn(_dispatch_args(work="web"))
            check("C4 a banned PREFERRED model is refused at the door: no dispatch, a plain reason "
                  "that names the ban and the preference",
                  not calls and res.get("status") == "error" and res.get("reason") == "policy_denied"
                  and "banned" in str(res.get("result")) and "unprefer" in str(res.get("result")),
                  (calls, res))
            calls, res = run_turn(_dispatch_args(model="acme/web-reader", work="web"))
            check("C5 a banned NAMED model is refused at the door",
                  not calls and res.get("status") == "error" and "banned" in str(res.get("result")),
                  (calls, res))
            calls, res = run_turn(_dispatch_args(work="email"))
            check("C6 a kind of work outside the set is refused, naming the set",
                  not calls and res.get("status") == "error" and "foreign_repo" in str(res.get("result")),
                  (calls, res))
            calls, res = run_turn(_dispatch_args())
            check("C7 a dispatch with no `work` is refused at the door: no agent, the reason names "
                  "the kinds of untrusted work",
                  not calls and res.get("status") == "error" and "`work`" in str(res.get("result"))
                  and "foreign_repo" in str(res.get("result")), (calls, res))
            calls, res = run_turn(_dispatch_args(work="web", tool_scope=["code_grep"],
                                                 grant=["read_context"]))
            check("C8 a dispatch scoped to the platform source is refused whatever `work` says: "
                  "the Host reads it itself",
                  not calls and res.get("status") == "error"
                  and "read(tool='code_grep'" in str(res.get("result")), (calls, res))
            calls, res = run_turn(_dispatch_args(work="message", tool_scope=["context"],
                                                 grant=["read_context", "read_memory", "write_context"]))
            check("C9 a dispatch whose scope reads nothing untrusted is refused (its own context "
                  "files are the Host's work)",
                  not calls and res.get("status") == "error", (calls, res))
        except Exception as e:  # noqa: BLE001 — a part that cannot run fails
            check("C ran", False, repr(e))

        # ── D ────────────────────────────────────────────────────────────────
        try:
            print("D. run_scoped_agent refuses a named banned model (every caller)")
            from mastercode.runtime import scoped_agent as SA
            model_calls: list = []

            async def no_model(**kw):
                model_calls.append(kw)
                return {"content": "ran"}

            async def one_schema(pool, eid, **kw):
                return [{"type": "function", "function": {
                    "name": "workspace_file", "description": "x",
                    "parameters": {"type": "object", "properties": {}}}}]
            saved = (SA.call_model, SA._build_tool_schemas, SA._META_TOOL_NAMES)
            SA.call_model, SA._build_tool_schemas = no_model, one_schema
            SA._META_TOOL_NAMES = {"workspace_file"}
            try:
                out = asyncio.run(SA.run_scoped_agent(
                    Pool(), entity_id=9003, task_prompt="t", tool_scope=["workspace_file"],
                    grant=["read_context", "write_context"], model="acme/web-reader",
                    session_id="agent:77", max_rounds=2))
            finally:
                SA.call_model, SA._build_tool_schemas, SA._META_TOOL_NAMES = saved
            check("D1 no model call; the run ends unavailable with the ban's reason",
                  not model_calls and out.get("lane_unavailable") is True
                  and "banned" in str(out.get("error")), (model_calls, out))

            # D2: the failover. The Host names its own chat model, so the agent
            # runs on the Host's route, fallback included; call_model moves an
            # errored round to that fallback. 'web-reader' is banned (C4).
            fb_calls: list = []

            async def rec_fb(**kw):
                fb_calls.append(kw)
                return {"content": "done", "tool_calls": None, "raw_message": {},
                        "tokens_used": 0, "prompt_tokens": 0, "completion_tokens": 0}

            async def fb_route(pool, eid, role, **k):
                return {"provider": "openrouter", "model": "acme/host-model",
                        "fallback_provider": "openrouter", "fallback_model": "acme/web-reader"}
            saved = (SA.call_model, SA._build_tool_schemas, SA._META_TOOL_NAMES, SA.resolve_role_route)
            SA.call_model, SA._build_tool_schemas = rec_fb, one_schema
            SA._META_TOOL_NAMES, SA.resolve_role_route = {"workspace_file"}, fb_route
            try:
                asyncio.run(SA.run_scoped_agent(
                    Pool(), entity_id=9003, task_prompt="t", tool_scope=["workspace_file"],
                    grant=["read_context", "write_context"], model="acme/host-model",
                    session_id="agent:78", max_rounds=2))
            finally:
                SA.call_model, SA._build_tool_schemas, SA._META_TOOL_NAMES, SA.resolve_role_route = saved
            check("D2 a banned fallback is never handed to call_model's failover (the named "
                  "model runs; an errored round cannot move to the banned one)",
                  fb_calls and all(not (k.get("fallback_model") or "").endswith("web-reader")
                                   for k in fb_calls), [k.get("fallback_model") for k in fb_calls])
        except Exception as e:  # noqa: BLE001 — a part that cannot run fails
            check("D ran", False, repr(e))

        # ── E ────────────────────────────────────────────────────────────────
        try:
            print("E. the choice is on the agent's task record")
            from mastercode.runtime import agent_dispatch as AD
            pool = Pool()
            d = AD.AgentDispatch(pool)

            async def fake_create(eid, title, prompt):
                return 88, "agent:88"

            async def fake_run(**kw):
                return None
            d._create_agent_task = fake_create
            d._run_and_record = fake_run
            choice = AW.choose_agent_model(None, "web", pp=pp)["choice"]
            asyncio.run(d.dispatch(entity_id=9003, title="t", task_prompt="p", tool_scope=[],
                                   model=None, model_choice=choice))
            seeded = {}
            for q, a in pool.executed:
                if "SET result" in q and a:
                    try:
                        seeded = json.loads(a[0])
                    except (TypeError, ValueError):
                        pass
                    break
            check("E1 dispatch seeds model_choice onto the record",
                  (seeded.get("model_choice") or {}).get("from") == choice.get("from")
                  and seeded["model_choice"].get("label") == choice.get("label"), seeded)
            row = {"task_id": 88, "title": "t", "description": "p", "task_type": "agent",
                   "status": "in_progress", "result": json.dumps({"model_choice": choice}),
                   "error": None, "created_at": None, "started_at": None, "completed_at": None}
            agent = AD.AgentDispatch._row_to_agent(row)
            check("E2 the Agents API row and the agent_status summary carry it",
                  agent.get("model_choice") == choice
                  and AD.AgentDispatch.summarize_agent(agent).get("model_choice") == choice, agent)
        except Exception as e:  # noqa: BLE001 — a part that cannot run fails
            check("E ran", False, repr(e))

        # ── F ────────────────────────────────────────────────────────────────
        try:
            print("F. an agent's model calls name its task")
            kws: list = []

            async def rec_model(**kw):
                kws.append(kw)
                return {"content": "the title is X", "tool_calls": None, "raw_message": {},
                        "tokens_used": 0, "prompt_tokens": 0, "completion_tokens": 0}

            async def route(pool, eid, role, **k):
                return {"provider": "openrouter", "model": "acme/host-model"}
            saved = (SA.call_model, SA._build_tool_schemas, SA._META_TOOL_NAMES, SA.resolve_role_route)
            SA.call_model, SA._build_tool_schemas = rec_model, one_schema
            SA._META_TOOL_NAMES, SA.resolve_role_route = {"workspace_file"}, route
            try:
                asyncio.run(SA.run_scoped_agent(
                    Pool(), entity_id=9003, task_prompt="t", tool_scope=["workspace_file"],
                    grant=["read_context", "write_context"], session_id="agent:446", max_rounds=2))
            finally:
                SA.call_model, SA._build_tool_schemas, SA._META_TOOL_NAMES, SA.resolve_role_route = saved
            check("F1 scoped_agent passes its task id with every model call",
                  kws and all(k.get("_log_meta") == {"task_id": 446} for k in kws)
                  and all(k.get("_source") == "scoped_agent" for k in kws), kws)

            from mastercode import provider_gateway as PG
            ins: list = []

            class LogPool:
                async def execute(self, q, *a):
                    if "INSERT INTO inference_cost_log" in q:
                        ins.append({"source": a[1], "purpose": a[4], "meta": json.loads(a[10])})
                    return "INSERT 1"

            async def fake_remote(**kw):
                if kw.get("model") == "acme/fails":
                    return {"error": "boom", "content": ""}
                await PG._log_inference(source=kw.get("_source"), model=kw.get("model"),
                                        provider="openrouter", total_tokens=5, prompt_tokens=3,
                                        completion_tokens=2, entity_id=9003)
                return {"content": "ok"}
            saved_pg = (PG._inference_pool, PG._call_remote)
            PG._inference_pool, PG._call_remote = LogPool(), fake_remote
            try:
                asyncio.run(PG.call_model([{"role": "user", "content": "hi"}], max_tokens=50,
                                          provider="openrouter", provider_model="acme/ok",
                                          _source="scoped_agent", _log_meta={"task_id": 446}))
                asyncio.run(PG.call_model([{"role": "user", "content": "hi"}], max_tokens=50,
                                          provider="openrouter", provider_model="acme/fails",
                                          _source="scoped_agent", _log_meta={"task_id": 446}))
                asyncio.run(PG.call_model([{"role": "user", "content": "hi"}], max_tokens=50,
                                          provider="openrouter", provider_model="acme/ok",
                                          _source="entity_runtime"))
            finally:
                PG._inference_pool, PG._call_remote = saved_pg
            agent_rows = [r for r in ins if r["source"] == "scoped_agent"]
            other = [r for r in ins if r["source"] == "entity_runtime"]
            check("F2 the gateway writes the task id into every row of that call, success and failure",
                  len(agent_rows) == 2 and all(r["meta"].get("task_id") == 446 for r in agent_rows)
                  and {r["purpose"] for r in agent_rows} == {"inference", "inference_error"}, ins)
            check("F3 ...and into no other call's row", other and "task_id" not in other[0]["meta"], ins)

            import datetime as _dt
            from server.routes import inference as INF
            from server import brain_service as BS
            apool = Pool()
            now = _dt.datetime.now(_dt.timezone.utc)
            apool.rows = [
                {"id": 1, "source_module": "scoped_agent", "model_id": "m", "provider": "p",
                 "purpose": "inference", "tokens_in": 1, "tokens_out": 1, "latency_ms": 1,
                 "metadata": json.dumps({"task_id": 446}), "created_at": now},
                {"id": 2, "source_module": "web_search", "model_id": "m", "provider": "p",
                 "purpose": "inference", "tokens_in": 1, "tokens_out": 1, "latency_ms": 1,
                 "metadata": json.dumps({"task_id": 9}), "created_at": now}]
            saved_bs = BS.get_pool
            BS.get_pool = lambda: apool
            try:
                feed = asyncio.run(INF.get_background_activity(limit=5, hours=24))
            finally:
                BS.get_pool = saved_bs
            acts = feed.get("activity") or []
            check("F4 background activity names the agent by its task; other lanes are unchanged",
                  len(acts) == 2 and acts[0].get("detail") == "Agent 446, at work"
                  and acts[0].get("task_id") == 446 and acts[1].get("task_id") is None
                  and acts[1].get("detail") == "Searching the web", acts)
        except Exception as e:  # noqa: BLE001 — a part that cannot run fails
            check("F ran", False, repr(e))

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # The dispatch verb's `work` field is written out as a literal (smoke's
    # narration gate reads the verb schemas as literals); it must stay equal
    # to the one definition in agent_work.
    from mastercode.runtime.session import _build_host_orchestration_tools
    from mastercode import agent_work as _aw
    _disp = [t for t in _build_host_orchestration_tools()
             if (t.get("function") or {}).get("name") == "dispatch"]
    _work = ((((_disp[0]["function"].get("parameters") or {}).get("properties") or {}).get("work"))
             if _disp else None)
    check("dispatch's work field equals agent_work.DISPATCH_WORK_PARAM",
          _work == _aw.DISPATCH_WORK_PARAM, repr(_work))

    if FAILS:
        print(f"agent-model-by-work: {len(FAILS)} FAIL(s): {FAILS}")
        return 1
    print("agent-model-by-work: all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
