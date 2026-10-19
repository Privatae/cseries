"""A dispatched agent writes to its model's own output limit, is told when
that limit cuts it off, and runs to the Host's runaway backstop.

What was wrong, and what holds now:

  OUTPUT LIMIT  The agent loop sent a fixed max_tokens=16384 (scoped_agent.py:829),
       never read whether the model stopped on that limit, and ran a tool
       call whose arguments were cut off (or not JSON at all) with `{}`. The
       Host was then told "operate requires `tool`" and blamed
       itself. Now: max_tokens=None (the gateway sends the routed model's
       own published limit); a call the limit cut is NOT run and the agent
       is told it was cut at N tokens and to write in parts; arguments that
       are not JSON are NOT run and the agent is told why; a plain answer the
       limit cut is continued, never taken as the final report; the ledger
       books a cut call as the platform's limit (blocked / truncated), never
       as the model's bad JSON.
  ROUNDS  max_rounds was clamped to min(max_rounds or 24, 96) without
       a word (agent_dispatch.py:394, :558) — a per-task ceiling below the
       Host's own 200-round backstop. Now the agent gets the Host's backstop
       (turn_allowance.backstop_rounds). The dispatch has no round argument
       at all: the Host
       set max_rounds=8 on a real task and its agent died on it. The
       exhaustion error names the backstop.
  REAPER  The liveness reaper wrote error 'timed out'. It now says what
       happened: no sign of life for the window.

No pod, no DB, no model: call_model, the route, the schemas and the
executor are scripted; the pool is a stub.
Run: APP_ROOT=<repo> python3 scripts/test_agent_output_and_rounds.py
"""

import asyncio
import json
import os
import sys

FAILS = []


def check(name, ok, detail=""):
    print(f"[agent-output-rounds] {'ok  ' if ok else 'FAIL'} {name}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


class _Pool:
    def __init__(self, bp=None):
        self.bp = bp
        self.executed = []

    async def execute(self, *a, **k):
        self.executed.append(a)
        return "UPDATE 1"

    async def fetch(self, *a, **k):
        return []

    async def fetchval(self, *a, **k):
        return None

    async def fetchrow(self, sql, *a, **k):
        if "behavioral_params" in sql and self.bp is not None:
            return {"behavioral_params": json.dumps(self.bp)}
        return None


def _schema(name):
    return {"type": "function", "function": {
        "name": name, "description": "x",
        "parameters": {"type": "object", "properties": {}}}}


def _tc(name, cid, args):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": args}}


async def _run(sa, script, *, max_rounds=4, ledger=None):
    """Run the agent against a scripted model. `script` is a list of results,
    one per model call. Returns (out, calls_seen, executed)."""
    seen, executed = [], []
    it = iter(script)

    async def fake_call_model(**kw):
        seen.append({"max_tokens": kw.get("max_tokens"), "source": kw.get("_source"),
                     "messages": [dict(m) for m in kw.get("messages") or []]})
        try:
            r = next(it)
        except StopIteration:
            r = {"content": "done"}
        base = {"tool_calls": None, "raw_message": {}, "tokens_used": 0,
                "prompt_tokens": 0, "completion_tokens": 0}
        return {**base, **r}

    async def fake_exec_meta(name, args, **kw):
        executed.append((name, args))
        return {"status": "ok", "result": f"{name} ran"}

    async def fake_schemas(pool, eid, **kw):
        return [_schema("workspace_file"), _schema("inbox_send")]

    async def fake_route(pool, eid, role):
        return {"provider": "openrtr", "model": "test/model", "base_url": None,
                "fallback_provider": None, "fallback_model": None}

    sa.call_model = fake_call_model
    sa._execute_meta_tool = fake_exec_meta
    sa._build_tool_schemas = fake_schemas
    sa.resolve_role_route = fake_route
    sa._META_TOOL_NAMES = {"workspace_file", "inbox_send"}
    sa._BRIDGE_TOOL_NAMES = set()
    if ledger is not None:
        import mastercode.runtime.tool_call_ledger as tcl

        def _rec(pool, **kw):
            ledger.append(kw)
            return None
        tcl.record_later = _rec
    out = await sa.run_scoped_agent(
        _Pool(), entity_id=1, task_prompt="t",
        tool_scope=["workspace_file", "inbox_send"], grant=["read_context", "write_context", "write_memory"],
        session_id=None, max_rounds=max_rounds)
    return out, seen, executed


def _tool_msgs(seen_call):
    return [m for m in seen_call["messages"] if m.get("role") == "tool"]


def main() -> int:
    root = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, root)
    sys.path.insert(0, os.path.join(root, "brain"))
    os.environ.pop("CSERIES_MAX_TOOL_ROUNDS", None)
    import mastercode.runtime.scoped_agent as sa

    # ── (e) no fixed 16384 on the agent's model call ─────────────────────────
    out, seen, _ = asyncio.run(_run(sa, [{"content": "all done"}]))
    check("the agent's model call sends no fixed output limit (max_tokens=None: the model's own)",
          seen and seen[0]["max_tokens"] is None, f"max_tokens={seen and seen[0]['max_tokens']}")
    check("source: no `16384` left in scoped_agent.py",
          "16384" not in open(os.path.join(root, "brain/mastercode/runtime/scoped_agent.py")).read())
    # Agent 446's 9 calls were logged source_module
    # 'unknown' and background activity printed "Background · unknown".
    from server.routes.inference import _activity_label
    check("the agent's model calls are logged as agent work (source_module 'scoped_agent', "
          "labelled Agents in background activity), never 'unknown'",
          seen and seen[0].get("source") == "scoped_agent"
          and _activity_label("scoped_agent")[0] == "Agents", seen and seen[0].get("source"))

    # ── (a) a tool call the output limit cut is not run, and the agent is told
    ledger = []
    cut_args = '{"path": "desktop/index.html", "content": "<html><body>' + "x" * 300
    out, seen, executed = asyncio.run(_run(sa, [
        {"content": "", "tool_calls": [_tc("workspace_file", "cut1", cut_args)],
         "truncated": True, "stop_reason": "max_tokens", "output_limit": 128000,
         "truncated_tool_call_ids": ["cut1"]},
        {"content": "wrote it in parts"},
    ], ledger=ledger))
    check("a call the output limit cut is NOT run", executed == [], f"executed={executed}")
    tm = _tool_msgs(seen[1]) if len(seen) > 1 else []
    body = tm[0]["content"] if tm else ""
    check("the agent is told it was cut at the model's output limit, nothing ran, write in parts",
          "NOT RUN" in body and "128,000 tokens" in body and "in parts" in body, body[:300])
    hist = [m for m in seen[1]["messages"] if m.get("tool_calls")] if len(seen) > 1 else []
    check("the cut call rides the history with `{}` arguments, not the broken text",
          hist and hist[-1]["tool_calls"][0]["function"]["arguments"] == "{}",
          f"{hist[-1]['tool_calls'][0]['function']['arguments'][:60] if hist else None}")
    rows = [r for r in ledger if r.get("tool") == "workspace_file"]
    check("ledger: the cut call is the platform's limit (blocked / truncated), never bad_args_json",
          rows and rows[0]["reason"] == "blocked" and rows[0]["status"] == "truncated", f"{rows}")
    acts = out.get("tool_actions") or []
    check("tool_actions records the call as truncated",
          acts and acts[0].get("result_status") == "truncated", f"{acts}")

    # A cut result whose gateway named no ids: the LAST call is the cut one.
    out, seen, executed = asyncio.run(_run(sa, [
        {"content": "", "tool_calls": [_tc("inbox_send", "ok1", '{"to": "a"}'),
                                       _tc("workspace_file", "cut2", '{"path": "a", "content": "{}"}')],
         "truncated": True, "output_limit": 64000},
        {"content": "done"},
    ]))
    check("cut with no id list: the earlier complete call runs, the last (in progress) does not",
          [e[0] for e in executed] == ["inbox_send"], f"executed={executed}")

    # ── (b) arguments that are not JSON are not run with {} ──────────────────
    ledger = []
    out, seen, executed = asyncio.run(_run(sa, [
        {"content": "", "tool_calls": [_tc("workspace_file", "bad1", '{"path": "a", oops')]},
        {"content": "done"},
    ], ledger=ledger))
    check("unparseable arguments: the tool is NOT run with {}", executed == [], f"executed={executed}")
    body = _tool_msgs(seen[1])[0]["content"] if len(seen) > 1 and _tool_msgs(seen[1]) else ""
    check("the agent is told the arguments were not valid JSON and nothing ran",
          "NOT RUN" in body and "not valid JSON" in body, body[:200])
    rows = [r for r in ledger if r.get("tool") == "workspace_file"]
    check("ledger: bad JSON (no cut) is the model's miss — bad_args_json",
          rows and rows[0]["reason"] == "bad_args_json", f"{rows}")

    # ── (c) a plain answer the limit cut is continued, not the report ────────
    out, seen, _ = asyncio.run(_run(sa, [
        {"content": "The report, part one; ", "truncated": True, "stop_reason": "max_tokens",
         "output_limit": 128000},
        {"content": "part two."},
    ]))
    check("a cut answer is continued: the final result is both parts",
          out.get("result") == "The report, part one; part two.", repr(out.get("result")))
    cont = seen[1]["messages"][-2:] if len(seen) > 1 else []
    check("the continuation call carries the partial answer and says where it stopped",
          len(cont) == 2 and cont[0].get("role") == "assistant"
          and cont[0].get("content") == "The report, part one; "
          and cont[1].get("role") == "user" and "output limit" in cont[1].get("content", "")
          and "128,000 tokens" in cont[1].get("content", ""), f"{cont}")
    check("a continued answer is not an error", not out.get("error"), f"{out.get('error')}")

    # ── (d) the Host's backstop, no silent clamp ─────────────────────────────
    from mastercode.runtime import turn_allowance as ta
    rb = asyncio.run(sa.agent_round_budget(_Pool(), 1))
    check("the agent gets the Host's runaway backstop",
          rb == {"rounds": ta.BACKSTOP_TOOL_ROUNDS, "backstop": ta.BACKSTOP_TOOL_ROUNDS}, f"{rb}")
    rb = asyncio.run(sa.agent_round_budget(_Pool({"max_tool_rounds": 400}), 1))
    check("the host's own max_tool_rounds setting moves the agent's backstop",
          rb["rounds"] == 400 and rb["backstop"] == 400, f"{rb}")
    rb = asyncio.run(sa.agent_round_budget(_Pool({"max_tool_rounds": 0}), 1))
    check("max_tool_rounds <= 0 turns the backstop off for agents too",
          rb["rounds"] >= 1_000_000, f"{rb}")

    import mastercode.runtime.agent_dispatch as ad
    got = {}

    async def fake_rsa(pool, **kw):
        got.update(kw)
        return {"result": "ok", "tool_actions": [], "tokens": 0, "tokens_in": 0,
                "tokens_out": 0, "rounds": 1, "model": "m", "lane": "p"}

    async def fake_create(self, eid, title, prompt):
        return 77, "agent:77"

    ad.run_scoped_agent = fake_rsa
    ad.AgentDispatch._create_agent_task = fake_create
    d = ad.AgentDispatch(_Pool())
    asyncio.run(d.dispatch_sync(entity_id=1, title="t", task_prompt="p", tool_scope=["inbox_send"]))
    check("dispatch_sync hands the agent the backstop (was 24)",
          got.get("max_rounds") == ta.BACKSTOP_TOOL_ROUNDS, f"{got.get('max_rounds')}")
    import inspect as _inspect
    check("dispatch and dispatch_sync take no round argument",
          "max_rounds" not in _inspect.signature(ad.AgentDispatch.dispatch).parameters
          and "max_rounds" not in _inspect.signature(ad.AgentDispatch.dispatch_sync).parameters)

    async def _dispatch_once():
        h = await d.dispatch(entity_id=1, title="t", task_prompt="p", tool_scope=["inbox_send"])
        for t in list(d._running.values()):
            await t
        return h
    got.clear()
    h = asyncio.run(_dispatch_once())
    check("dispatch (background) runs the agent to the backstop and the handle names it",
          h.get("round_backstop") == ta.BACKSTOP_TOOL_ROUNDS
          and got.get("max_rounds") == ta.BACKSTOP_TOOL_ROUNDS, f"{h} {got.get('max_rounds')}")
    src = open(os.path.join(root, "brain/mastercode/runtime/agent_dispatch.py")).read()
    check("source: the silent `min(..., 96)` clamp is gone", ", 96))" not in src)

    # Exhaustion names which bound was hit.
    out, _, _ = asyncio.run(_run(sa, [
        {"content": "", "tool_calls": [_tc("inbox_send", f"r{i}", json.dumps({"n": i}))]}
        for i in range(5)], max_rounds=2))
    check("rounds used up: the error names the backstop, never a budget the dispatch set",
          "runaway backstop of 2 tool rounds" in (out.get("error") or "")
          and "this dispatch set" not in (out.get("error") or ""), f"{out.get('error')!r}")
    msg = sa.rounds_exhausted_text(200)
    check("rounds used up at the backstop: the error says it was the runaway backstop",
          "runaway backstop of 200" in msg, msg)

    # ── the reaper says what happened ────────────────────────────────────────
    txt = ad.no_sign_of_life_text(1800)
    check("reaper text: 'no sign of life for 30 minutes', not 'timed out'",
          "no sign of life for 30 minutes" in txt and "timed out" not in txt, txt)

    class _MonPool(_Pool):
        async def fetch(self, sql, *a, **k):
            if "last_heartbeat_at" in sql:
                return [{"task_id": 5}]
            return []

    mp = _MonPool()
    md = ad.AgentDispatch(mp)
    md.runtime = object()

    async def _no_deliver(tid):
        return None
    md._deliver = _no_deliver
    asyncio.run(md._monitor_tick(1800))
    upd = [a for a in mp.executed if "status='failed'" in a[0]]
    check("the reaper's failed row carries the no-sign-of-life text",
          upd and any("no sign of life for 30 minutes" in str(x) for x in upd[0][1:]),
          f"{upd}")

    if FAILS:
        print(f"[agent-output-rounds] FAIL: {len(FAILS)} check(s)")
        return 1
    print("[agent-output-rounds] PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
