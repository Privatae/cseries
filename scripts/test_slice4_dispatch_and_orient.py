#!/usr/bin/env python3
"""The model cannot cap a dispatched agent's rounds, and `orient` is gone.

WHY THIS EXISTS
A Host dispatched agent 446 with
`max_rounds: 8`, which the dispatch verb let the model set. The agent made 64
tool calls in 37 s and stopped on "used the 8-round budget this dispatch set
(max_rounds)". Nobody authorized a cap on real work; the platform's own
runaway backstop is the only round bound an agent has now.

`orient` was the groups-mode drill. It is off every tool list, and its
registry entry and executor are deleted too (scoped_agent.py and the
capability checks included).

  (a) dispatch: the verb's schema has no round argument; a real turn whose
      model still sends max_rounds=8 hands AgentDispatch no round argument;
      AgentDispatch.dispatch takes none and runs the agent to the Host's
      backstop; the agent's exhaustion error names the backstop.
  (b) orient: not in the registry, the executor set, the caps table, the lane
      table or the turn-type primitives, and the executor has no branch for it.

Container-free: APP_ROOT=<repo> python3 scripts/test_slice4_dispatch_and_orient.py
No pod, no DB, no model: one real turn through EntityRuntime.send_message with
a scripted model and a stub pool (the harness scripts/test_tool_search_core.py
uses), and AgentDispatch with a stubbed agent run.
"""
import asyncio
import copy
import inspect
import json
import logging
import os
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
        print(f"[slice4-dispatch-orient] FAIL {name} {extra}")


from mastercode.runtime import session as S  # noqa: E402
from mastercode import provider_gateway as PG  # noqa: E402
from mastercode import inference_config as IC  # noqa: E402
from mastercode.runtime import turn_allowance as TA  # noqa: E402
from mastercode.runtime import tool_core as TC  # noqa: E402
from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS as P_REG  # noqa: E402

CLOSE = {"topic": "agent rounds", "handoff": {"next_step": "nothing"}}


def tc(name, args, i):
    return {"id": f"call_{i}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def _wire(name):
    s = copy.deepcopy(P_REG[name])
    s.pop("_meta", None)
    return s


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


class FakeDispatch:
    """What session.py's dispatch branch hands the dispatcher."""

    def __init__(self):
        self.calls: list[dict] = []

    async def dispatch(self, **kw):
        self.calls.append(kw)
        return {"task_id": 446, "status": "running", "session_id": "agent:446"}

    async def project_has_plan(self, project_id):
        return True

    async def mark_round(self, ids):
        return None


def run_turn(script, disp):
    async def model(messages=None, *a, **kw):
        r = script.pop(0) if script else {"content": "Done.",
                                          "tool_calls": [tc("close_turn", CLOSE, 999)]}
        return {"tokens_used": 1, "prompt_tokens": 1, "completion_tokens": 1,
                "tool_calls": None, **r}

    async def route(pool, eid, role, thread_override=None, **k):
        return {"provider": "anthropic", "model": "fake-model"}

    async def run_close_turn(pool, sess, args, *, prompt_console=None):
        return {"status": "ok", "result": "recorded"}

    async def meta(name, args, **k):
        return {"status": "ok", "result": f"{name} ran"}

    async def schemas(pool, eid, interaction_context=None, **k):
        return [_wire(n) for n in ("web_search", "read_url", "workspace_file")]

    saved = (S._call_entity_model, PG.call_model, IC.resolve_role_route,
             S._AVAILABLE.get("ceigas_domain_controller"), S._run_close_turn,
             S._execute_meta_tool, S._build_tool_schemas)
    S._call_entity_model = model
    PG.call_model = model
    IC.resolve_role_route = route
    S._AVAILABLE["ceigas_domain_controller"] = None
    S._run_close_turn = run_close_turn
    S._execute_meta_tool = meta
    S._build_tool_schemas = schemas
    TC._USE_CACHE.clear()
    # The turn holds at its exit for the agent it started (session.HOLD_S,
    # 240 s); the fake dispatcher never reports, so hold briefly.
    S.HOLD_S = 0.3
    rt = S.EntityRuntime(Pool())
    rt.agent_dispatch = disp

    async def ctx(entity_id, **k):
        return S.EntitySession(
            session_id="sess-s4", entity_id=entity_id,
            entity={"id": entity_id, "name": "Host", "behavioral_params": {"tool_surface": "search"},
                    "permission_overrides": {"bot_capabilities": 255}},
            interaction_context="chat")
    rt._build_chat_context_transient = ctx
    try:
        return asyncio.run(rt.send_message(entity_id=9003, user_message="Look into the failing build for me."))
    finally:
        (S._call_entity_model, PG.call_model, IC.resolve_role_route,
         S._AVAILABLE["ceigas_domain_controller"], S._run_close_turn,
         S._execute_meta_tool, S._build_tool_schemas) = saved


def main() -> int:
    # ── (a) dispatch carries no round argument ──────────────────────────────
    print("(a) the model cannot set an agent's rounds")
    disp_schema = next(v for v in S._orchestration_verbs()
                       if (v.get("function") or {}).get("name") == "dispatch")
    props = (disp_schema["function"].get("parameters") or {}).get("properties") or {}
    check("the dispatch verb's schema has no max_rounds", "max_rounds" not in props, sorted(props))

    disp = FakeDispatch()
    run_turn([
        # A web read: the door admits only untrusted work
        # (a build log is the Host's own to read), and `work` is required.
        {"content": "Dispatching an agent to read the vendor's release notes.",
         "tool_calls": [tc("dispatch", {
             "intent": "I'm sending an agent to read the vendor's release notes.",
             "task": "Read the release notes page and report the breaking changes.",
             "tool_scope": ["read_url"], "grant": ["external_api"], "work": "web",
             "system_prompt": "You read one web page and report its breaking changes.",
             "max_rounds": 8}, 1)]},
    ], disp)
    check("a real turn's dispatch reaches the dispatcher", len(disp.calls) == 1, disp.calls)
    kw = disp.calls[0] if disp.calls else {}
    check("the model's max_rounds=8 does not reach the dispatcher (ignored like any argument "
          "dispatch does not have)",
          bool(disp.calls) and "max_rounds" not in kw and 8 not in kw.values(), sorted(kw))

    from mastercode.runtime import agent_dispatch as AD
    from mastercode.runtime import scoped_agent as SA
    check("AgentDispatch.dispatch and dispatch_sync take no round argument",
          "max_rounds" not in inspect.signature(AD.AgentDispatch.dispatch).parameters
          and "max_rounds" not in inspect.signature(AD.AgentDispatch.dispatch_sync).parameters)
    check("agent_round_budget takes no per-dispatch budget",
          list(inspect.signature(SA.agent_round_budget).parameters) == ["pool", "entity_id"])

    got: dict = {}

    async def fake_rsa(pool, **k):
        got.update(k)
        return {"result": "ok", "tool_actions": [], "tokens": 0, "tokens_in": 0,
                "tokens_out": 0, "rounds": 1, "model": "m", "lane": "p"}

    async def fake_create(self, eid, title, prompt):
        return 446, "agent:446"

    saved = (AD.run_scoped_agent, AD.AgentDispatch._create_agent_task)
    AD.run_scoped_agent = fake_rsa
    AD.AgentDispatch._create_agent_task = fake_create
    try:
        d = AD.AgentDispatch(Pool())

        async def once():
            h = await d.dispatch(entity_id=9003, title="t", task_prompt="p", tool_scope=[],
                                 grant=["read_context"])
            for t in list(d._running.values()):
                await t
            return h
        h = asyncio.run(once())
    finally:
        AD.run_scoped_agent, AD.AgentDispatch._create_agent_task = saved
    check("the agent runs to the Host's runaway backstop",
          got.get("max_rounds") == TA.BACKSTOP_TOOL_ROUNDS
          and h.get("round_backstop") == TA.BACKSTOP_TOOL_ROUNDS, f"{got.get('max_rounds')} {h}")
    try:
        msg = SA.rounds_exhausted_text(TA.BACKSTOP_TOOL_ROUNDS)
    except TypeError as e:          # main's two-argument form
        msg = f"TypeError: {e}"
    check("the exhaustion error names the backstop, never a budget the dispatch set",
          f"runaway backstop of {TA.BACKSTOP_TOOL_ROUNDS}" in msg and "this dispatch set" not in msg, msg)

    # ── (b) orient is gone ───────────────────────────────────────────────────
    print("(b) orient is gone")
    from mastercode.public_tool_registry import PUBLIC_META_TOOL_NAMES
    from mastercode.runtime.meta_tool_caps import META_TOOL_CAPS
    from mastercode.runtime import tool_lanes as TL
    from mastercode.runtime import tools as T
    from mastercode.runtime import turn_types as TT
    check("orient is not in the registry", "orient" not in P_REG)
    check("orient is not in the executor set (PUBLIC_META_TOOL_NAMES)", "orient" not in PUBLIC_META_TOOL_NAMES)
    check("orient has no caps row", "orient" not in META_TOOL_CAPS)
    check("orient has no lane", '"orient"' not in inspect.getsource(TL))
    check("the executor has no orient branch and no _exec_orient",
          'name == "orient"' not in inspect.getsource(T._execute_meta_tool)
          and not hasattr(T, "_exec_orient"))
    check("orient is not a turn-type primitive", "orient" not in TT.ALWAYS_ALLOWED)

    if FAILS:
        print(f"slice4-dispatch-orient: {len(FAILS)} FAIL(s): {FAILS}")
        return 1
    print("slice4-dispatch-orient: all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
