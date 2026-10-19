#!/usr/bin/env python3
"""Gate: what the cap fixes do TOGETHER.

Each fix was written on its own branch, where the other half of these paths
did not exist yet: one sets the model's window for the tool-result wall
(context_budget.set_tool_wall_window / wall_kwargs), another sizes the wall and
every reader's page from it (tool_result_wall.TOOL_WALL_WINDOW); a third waits
on a machine's coding run for as long as the machine reports it, and the first
also stops a turn that shows no sign of life. This pins the joined paths:

  W1  wall_kwargs takes the REAL path: the wall in the tree takes
      `context_window` (no stand-in function).
  W2  through a real agent run (run_scoped_agent, the real workspace_file
      reader, the real wall): an agent on a 1M-token model and one on a
      200k-token model each get a page sized to THEIR window, uncut by the
      wall — whatever window the Host turn that dispatched them held.
  W3  an agent whose model has no known window gets the legacy wall and page,
      never the dispatching Host's window (it ran in a copy of the Host's
      context, so an unset window inherited a 1M Host's ~1 MB pages).
  W4  through a real Host turn (EntityRuntime.send_message, scripted model):
      the turn's window sizes the page, uncut, for 1M and 200k.
  H1  a Host tool call waiting on a machine's coding run that keeps
      reporting (HarnessRelay.dispatch, the box's 8 s frames) keeps the turn
      alive past the idle window; a silent box still ends it.

No pod, no DB, no network: the model, the pool and the machine are stubbed;
the workspace is a temp dir under TMPDIR.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

APP_ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_ROOT)
sys.path.insert(0, os.path.join(APP_ROOT, "brain"))

FAILS: list[str] = []


def check(name: str, ok, detail="") -> None:
    print(f"[caps-batch] {'ok  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


from mastercode import context_budget as CB  # noqa: E402
from mastercode.runtime import tool_result_wall as W  # noqa: E402
from mastercode.runtime import tools as T  # noqa: E402
from mastercode.runtime import scoped_agent as SA  # noqa: E402
from server.capabilities import file_access as FA  # noqa: E402
from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS as P_REG  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="caps-batch-", dir=os.environ.get("TMPDIR") or None))
_BASE = _TMP / "ws"
_BASE.mkdir()
_SIZE = 3_000_000
(_BASE / "big.txt").write_bytes(b"a" * _SIZE)
FA.WORKSPACE = _BASE

LEGACY_PAGE = W.read_page_cap("bytes")            # no window set in this context


def _page_of(content: str):
    """(the reader page, the wall's envelope) of one tool message."""
    env = json.loads(content)
    if env.get("truncated"):
        return None, env
    page = env.get("result") if isinstance(env.get("result"), dict) else env
    return page, env


# ── W1 ─────────────────────────────────────────────────────────────────────
check("W1: wall_kwargs passes the window to the wall in this tree (the real _bound_tool_result)",
      CB.wall_kwargs(1_000_000) == {"context_window": 1_000_000}
      and CB.wall_kwargs(200_000) == {"context_window": 200_000},
      (CB.wall_kwargs(1_000_000), CB.wall_kwargs(200_000)))


# ── W2 / W3: a real agent run ──────────────────────────────────────────────
class _Pool:
    async def execute(self, *a, **k): return None
    async def fetch(self, *a, **k): return []
    async def fetchval(self, *a, **k): return None
    async def fetchrow(self, *a, **k): return None


async def _agent_page(agent_window, host_window):
    """One agent run, dispatched the way AgentDispatch.dispatch does it — a
    task created from the Host turn's context, which holds `host_window` —
    whose model reads big.txt once, asking for all of it (a page is
    the most one result carries: the ask is clamped to the window's page). Returns the tool message it saw."""
    seen: dict = {}
    calls = {"n": 0}

    async def fake_call_model(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"content": "", "raw_message": {}, "tokens_used": 0, "prompt_tokens": 0,
                    "completion_tokens": 0, "tool_calls": [
                        {"id": "c1", "function": {"name": "workspace_file", "arguments": json.dumps(
                            {"op": "read", "path": "big.txt", "length": _SIZE})}}]}
        msgs = kw.get("messages") or []
        seen["tool"] = next((m.get("content") for m in msgs if m.get("role") == "tool"), None)
        return {"content": "done", "tool_calls": None, "raw_message": {}, "tokens_used": 0,
                "prompt_tokens": 0, "completion_tokens": 0}

    async def fake_exec_meta(name, args, **kw):
        if name == "workspace_file":
            return await T._exec_workspace_file(args)          # the real reader
        return {"status": "error", "result": "not in this test"}

    async def fake_schemas(pool, eid, **kw):
        return [{"type": "function", "function": P_REG["workspace_file"]["function"]}]

    async def fake_route(pool, eid, role):
        return {"provider": "openrtr", "model": "test/model", "base_url": None,
                "fallback_provider": None, "fallback_model": None}

    async def fake_window(model, provider=None):
        return agent_window

    saved = (SA.call_model, SA._execute_meta_tool, SA._build_tool_schemas, SA.resolve_role_route,
             SA._META_TOOL_NAMES, SA._BRIDGE_TOOL_NAMES, CB.model_context_window)
    SA.call_model = fake_call_model
    SA._execute_meta_tool = fake_exec_meta
    SA._build_tool_schemas = fake_schemas
    SA.resolve_role_route = fake_route
    SA._META_TOOL_NAMES = {"workspace_file"}
    SA._BRIDGE_TOOL_NAMES = set()
    CB.model_context_window = fake_window
    try:
        async def host_turn():
            W.TOOL_WALL_WINDOW.set(host_window)      # what the Host turn set at its start
            agent = asyncio.create_task(SA.run_scoped_agent(
                _Pool(), entity_id=1, task_prompt="read big.txt", tool_scope=["workspace_file"],
                grant=["read_context", "write_context", "execute_tool"], session_id=None,
                max_rounds=3))
            out = await agent
            return out, W.TOOL_WALL_WINDOW.get()
        out, host_after = await host_turn()
    finally:
        (SA.call_model, SA._execute_meta_tool, SA._build_tool_schemas, SA.resolve_role_route,
         SA._META_TOOL_NAMES, SA._BRIDGE_TOOL_NAMES, CB.model_context_window) = saved
    return seen.get("tool"), out, host_after


for agent_w, host_w in ((1_000_000, 200_000), (200_000, 1_000_000)):
    content, out, host_after = asyncio.run(_agent_page(agent_w, host_w))
    page, env = _page_of(content) if isinstance(content, str) else (None, {"no tool message": out})
    n = len((page or {}).get("content") or "")
    cap = W.result_wall_bytes(agent_w) - 4096          # read_page_cap at this window
    check(f"W2: an agent on a {agent_w:,}-token model (dispatched from a {host_w:,} Host) gets a page "
          f"sized to its own window, uncut by the wall",
          page is not None and LEGACY_PAGE < n <= cap and page.get("next_offset") == n
          and page.get("size") == _SIZE,
          {"page_chars": n, "cap": cap, "legacy": LEGACY_PAGE,
           "env": {k: v for k, v in (env or {}).items() if k != "result"}})
    check(f"W2: ...and the dispatching Host's window is untouched by it ({host_w:,})",
          host_after == host_w, host_after)

content, out, host_after = asyncio.run(_agent_page(None, 1_000_000))
page, env = _page_of(content) if isinstance(content, str) else (None, {"no tool message": out})
n = len((page or {}).get("content") or "")
check("W3: an agent whose model has no known window gets the legacy page and wall, not the 1M Host's",
      page is not None and 0 < n <= LEGACY_PAGE and len(content) <= W.result_wall_bytes(None),
      {"page_chars": n, "legacy": LEGACY_PAGE, "tool_message_bytes": len(content or "")})


# ── W4: a real Host turn ───────────────────────────────────────────────────
from mastercode.runtime import session as S  # noqa: E402
from mastercode import provider_gateway as PG  # noqa: E402
from mastercode import inference_config as IC  # noqa: E402


class _TurnPool(_Pool):
    async def execute(self, *a, **k): return "OK"

    def acquire(self):
        pool = self

        class _C:
            async def __aenter__(self):
                return pool

            async def __aexit__(self, *e):
                return False
        return _C()


async def _host_page(window):
    calls: list = []
    script = [{"content": None, "tool_calls": [{"id": "call_ws_1", "type": "function", "function": {
                  "name": "workspace_file", "arguments": json.dumps({"op": "read", "path": "big.txt", "length": _SIZE})}}]},
              {"content": "Read it."}]

    async def model(messages=None, *a, **kw):
        msgs = messages if messages is not None else (a[0] if a else [])
        calls.append([dict(m) for m in msgs])
        r = script.pop(0) if script else {"content": "(the script ran out)"}
        return {"tokens_used": 1, "prompt_tokens": 1, "completion_tokens": 1, "tool_calls": None, **r}

    async def route(pool, eid, role, thread_override=None, **kw):
        return {"provider": "anthropic", "model": "fake-model"}

    async def meta(name, args, **kw):
        if name == "workspace_file":
            return await T._exec_workspace_file(args)
        return {"status": "ok", "result": f"{name} ran"}

    async def schemas(pool, eid, interaction_context=None, **kw):
        return [{"type": "function", "function": P_REG["workspace_file"]["function"]}]

    async def fake_window(model_id, provider=None):
        return window

    saved = (S._call_entity_model, PG.call_model, IC.resolve_role_route,
             S._AVAILABLE.get("ceigas_domain_controller"), S._execute_meta_tool,
             S._build_tool_schemas, CB.model_context_window)
    S._call_entity_model = model
    PG.call_model = model
    IC.resolve_role_route = route
    S._AVAILABLE["ceigas_domain_controller"] = None
    S._execute_meta_tool = meta
    S._build_tool_schemas = schemas
    CB.model_context_window = fake_window
    try:
        rt = S.EntityRuntime(_TurnPool())

        async def ctx(entity_id, **kw):
            return S.EntitySession(
                session_id="sess-w4", entity_id=entity_id,
                entity={"id": entity_id, "name": "Nova", "behavioral_params": {},
                        "permission_overrides": {"bot_capabilities": 255}},
                interaction_context="app:circles")
        rt._build_chat_context_transient = ctx
        await rt.send_message(entity_id=9003, user_message="Read big.txt.")
    finally:
        (S._call_entity_model, PG.call_model, IC.resolve_role_route,
         S._AVAILABLE["ceigas_domain_controller"], S._execute_meta_tool,
         S._build_tool_schemas, CB.model_context_window) = saved
    for msgs in calls[1:]:
        for m in msgs:
            if m.get("role") == "tool":
                return m.get("content"), len(calls)
    return None, len(calls)


for window in (1_000_000, 200_000):
    content, ncalls = asyncio.run(_host_page(window))
    page, env = _page_of(content) if isinstance(content, str) else (None, {})
    n = len((page or {}).get("content") or "")
    cap = W.result_wall_bytes(window) - 4096
    check(f"W4: a Host turn on a {window:,}-token model reads a page sized to its window, uncut by the wall",
          page is not None and LEGACY_PAGE < n <= cap and page.get("next_offset") == n,
          {"calls": ncalls, "page_chars": n, "cap": cap,
           "env": {k: v for k, v in (env or {}).items() if k != "result"}})


# ── H1: a reporting machine run keeps the Host turn alive ─────────────────
from server import turn_registry as TR  # noqa: E402
from server.network import nac_harness as NH  # noqa: E402


class _Box:
    """A harness worker on a machine: takes the job, sends a frame every
    `beat` s for `run_s`, then the result (`beat` None: silent)."""

    def __init__(self, run_s, beat):
        self.run_s, self.beat = run_s, beat
        self.last_frame_at = None
        self.bastion_token_id, self.lane = 7, "harness"

    def assign(self, job):
        spec, result = job
        loop = asyncio.get_event_loop()

        async def _work():
            t_end = loop.time() + self.run_s
            while loop.time() < t_end:
                if self.beat:
                    self.last_frame_at = loop.time()
                await asyncio.sleep(self.beat or 0.05)
            if not result.done():
                result.set_result((b'{"ok": true}', b"log", 0))
        self._task = loop.create_task(_work())
        return True


async def _turn_waiting_on_box(box, idle_s, silence_s):
    saved = (TR.TURN_IDLE_S, NH.HARNESS_SILENCE_S)
    TR.TURN_IDLE_S, NH.HARNESS_SILENCE_S = idle_s, silence_s
    got: dict = {}
    try:
        relay = NH.HarnessRelay()
        relay._q(7).put_nowait(box)
        relay._workers[(7, "harness")].add(box)

        async def inner():
            yield '0:"Running it on the machine."\n'
            try:
                got["result"] = await relay.dispatch(7, {"mode": "agent"}, timeout=30)
            except NH.HarnessRunStopped as e:
                got["stopped"] = e.why
            yield '0:"Done."\n'
            yield "e:{}\n"
        reg = TR.TurnRegistry()
        rec, _ = reg.start_or_attach(thread_key="h1", entity_id=1, app_id="t",
                                     message="m", make_inner=inner)
        try:
            await asyncio.wait_for(rec.done.wait(), 15)
        except asyncio.TimeoutError:
            pass
        return rec, got
    finally:
        TR.TURN_IDLE_S, NH.HARNESS_SILENCE_S = saved


rec, got = asyncio.run(_turn_waiting_on_box(_Box(run_s=2.5, beat=0.1), idle_s=1.0, silence_s=0.8))
check("H1: a Host tool call waiting on a machine run that keeps reporting keeps the turn alive past "
      "the idle window, and the run's result comes back",
      rec.status == "done" and got.get("result") == (b'{"ok": true}', b"log", 0),
      (rec.status, getattr(rec, "stop_cause", None), got))
rec, got = asyncio.run(_turn_waiting_on_box(_Box(run_s=6.0, beat=None), idle_s=5.0, silence_s=0.8))
check("H1: ...while a box that goes silent still ends the wait (HARNESS_SILENCE_S), and the turn goes on",
      got.get("stopped") == "silent" and rec.status == "done", (rec.status, got))


def _cleanup():
    import shutil
    shutil.rmtree(_TMP, ignore_errors=True)


_cleanup()
print(f"[caps-batch] {'PASS' if not FAILS else 'FAIL'} ({len(FAILS)} failed)")
sys.exit(1 if FAILS else 0)
