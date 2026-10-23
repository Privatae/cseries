#!/usr/bin/env python3
"""Two more readers of a stopped reply never hand the next model a platform
notice as the Host's words.

Container-free: APP_ROOT=<repo> python3 scripts/test_stopped_reply_readers.py.
A scripted model, stand-in pools and a blocked HTTP client; no pod, no DB,
no model, no network.

  a  The session fallback: two turns in ONE session with recall
     returning nothing, the first stopped on its budget. The second turn's
     history is built from sess.messages; its assistant content holds only the
     Host's own words, the stop is a platform note, and sess.messages still
     holds what the person saw.
  b  knowledge_toc.scope_episodes (chat_threads op='episodes' and
     op='toc'): an episode with no message from the person previews a reply;
     a notice on that reply is named apart (platform_notice, host_preview),
     the preview the person sees is unchanged, and an episode that has the
     person's message is untouched.
  c  context_budget.build_thread_context: the panel's "History window"
     preview is the window the model gets (history_messages) — the Host's own
     words, and the stop as the platform's note row, which the panel once
     showed nowhere.

Exit 0 = all pass; exit 1 + FAIL lines otherwise.
"""

import asyncio
import json
import logging
import os
import sys
import types

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
        print(f"[g1-r13] FAIL {name} {extra}")


# No request leaves the box: every HTTP client this process could open raises.
_NET_TRIES: list = []


class _NoNet:
    def __init__(self, *a, **kw):
        _NET_TRIES.append(a)
        raise RuntimeError("test_stopped_reply_readers: no network")


import aiohttp  # noqa: E402
aiohttp.ClientSession = _NoNet
try:
    import httpx  # noqa: E402
    httpx.AsyncClient = _NoNet
    httpx.Client = _NoNet
except ImportError:
    pass

from mastercode import inference_config as IC  # noqa: E402
from mastercode import knowledge_toc as KT  # noqa: E402
from mastercode import memory_engine as ME  # noqa: E402
from mastercode import provider_gateway as PG  # noqa: E402
from mastercode.runtime import session as S  # noqa: E402
import mastercode.runtime.turn_allowance as TA  # noqa: E402

HOST_WORDS = "Done: the clock. Left: the theme."
STOP = "This turn reached its budget."


# ── a. the fallback history path ──────────────────────────────────────────
class Pool:
    """Every read is empty (recall returns nothing) and every write lands
    nowhere — the turn's own bookkeeping is fail-soft."""

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


def _tc(name, args, i=1):
    return {"id": f"call_{name}_{i}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


class TwoTurns:
    """Two turns through EntityRuntime.send_message on the SAME session
    object, so the second turn's history can only come from sess.messages."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[list[dict]] = []
        self.rows: list[dict] = []
        self.pool = Pool()
        self.sess = None

    async def model(self, messages=None, *a, **kw):
        msgs = messages if messages is not None else (a[0] if a else [])
        self.calls.append([dict(m) for m in msgs])
        r = self.script.pop(0) if self.script else {"content": "(the script ran out)"}
        cb = kw.get("stream_cb")
        if cb and r.get("content"):
            await cb(r["content"])
        return {"tokens_used": 1, "prompt_tokens": 1, "completion_tokens": 1,
                "tool_calls": None, **r}

    async def go(self, turns):
        S._pending_nudges.clear()
        S._nudge_times.clear()
        getattr(S, "_nudge_orders", {}).clear()
        S._turn_events.clear()
        S._task_holds.clear()
        saved = (S._call_entity_model, PG.call_model, IC.resolve_role_route,
                 S._AVAILABLE.get("ceigas_domain_controller"), S._run_close_turn,
                 S._execute_meta_tool, S._build_tool_schemas,
                 ME.write_turn_row, ME.write_turn_row_by_token,
                 TA.TurnSpend.check, TA.TurnSpend.would_stop, TA.TurnSpend.would_overrun)
        saved_mods = {n: sys.modules.get(n) for n in
                      ("server.main", "server.bridge.memory_handlers", "server.brain_service")}
        import server
        saved_bs = getattr(server, "brain_service", None)
        me = self

        async def route(pool, eid, role, thread_override=None, **kw):
            return {"provider": "anthropic", "model": "fake-model"}

        async def run_close_turn(pool, sess, args, *, prompt_console=None):
            return {"status": "ok", "result": "recorded"}

        async def meta(name, args, **kw):
            if name == "escalate":
                return {"status": "ok", "result": {
                    "ticket_id": 41, "ticket_uuid": "u-41", "status": "open",
                    "auto_resolved": False}}
            return {"status": "ok", "result": f"{name} ran"}

        async def schemas(pool, eid, interaction_context=None, **kw):
            return []

        async def write_turn_row(pool, entity_id, session_id, role, content, **kw):
            me.rows.append({"role": role, "content": content, "tags": dict(kw.get("tags") or {})})
            return len(me.rows)

        async def write_turn_row_by_token(pool, entity_id, session_id, role, content, *,
                                          token, held, row_id=None, project_id=None,
                                          tags=None, created_at=None):
            for r in me.rows:
                if r.get("token") == token and r["role"] == role:
                    r.update(content=content)
                    r["tags"].update({k: v for k, v in (tags or {}).items() if v is not None})
                    return 1
            me.rows.append({"role": role, "content": content, "tags": dict(tags or {}),
                            "token": token})
            return len(me.rows)

        async def broadcast(msg):
            return None

        async def inbox_append(payload):
            return {"ok": True}

        async def send_message(**kw):
            if False:
                yield ""

        S._call_entity_model = self.model
        PG.call_model = self.model
        IC.resolve_role_route = route
        S._AVAILABLE["ceigas_domain_controller"] = None
        S._run_close_turn = run_close_turn
        S._execute_meta_tool = meta
        S._build_tool_schemas = schemas
        ME.write_turn_row = write_turn_row
        ME.write_turn_row_by_token = write_turn_row_by_token
        for n in saved_mods:
            sys.modules[n] = types.ModuleType(n)
        sys.modules["server.main"].broadcast = broadcast
        sys.modules["server.bridge.memory_handlers"].handle_inbox_append = inbox_append
        sys.modules["server.brain_service"].send_message = send_message
        server.brain_service = sys.modules["server.brain_service"]
        rt = S.EntityRuntime(self.pool)
        self.sess = S.EntitySession(
            session_id="sess-r13", entity_id=9003,
            entity={"id": 9003, "name": "Nova", "behavioral_params": {},
                    "permission_overrides": {"bot_capabilities": 255}},
            interaction_context="project:251")

        async def ctx(entity_id, **kw):
            return me.sess
        rt._build_chat_context_transient = ctx
        self.turn_starts = []
        try:
            for message, spend in turns:
                TA.TurnSpend.check, TA.TurnSpend.would_stop, TA.TurnSpend.would_overrun = spend
                self.turn_starts.append(len(self.calls))
                await rt.send_message(entity_id=9003, user_message=message,
                                      project_id=251, app_id="workbench")
                await asyncio.sleep(0.2)      # the row writes run detached
        finally:
            (S._call_entity_model, PG.call_model, IC.resolve_role_route,
             S._AVAILABLE["ceigas_domain_controller"], S._run_close_turn,
             S._execute_meta_tool, S._build_tool_schemas,
             ME.write_turn_row, ME.write_turn_row_by_token,
             TA.TurnSpend.check, TA.TurnSpend.would_stop, TA.TurnSpend.would_overrun) = saved
            for n, m in saved_mods.items():
                if m is None:
                    sys.modules.pop(n, None)
                else:
                    sys.modules[n] = m
            if saved_bs is not None:
                server.brain_service = saved_bs
            elif hasattr(server, "brain_service"):
                delattr(server, "brain_service")


# Turn 1: the spend check stops the turn after its first call; the tools-off
# answer is empty and its retry writes the Host's words (the
# "tool-loop stop, retry has text" shape). Turn 2: nothing stops.
_STOP_AT_1 = (lambda self, n, r: (("stop", self._stop_message(0.0)) if self.calls >= 1 else ("ok", None)),
              lambda self, *a, **k: False, lambda self, n, r: False)
_NEVER = (lambda self, n, r: ("ok", None), lambda self, *a, **k: False, lambda self, n, r: False)

h = TwoTurns([{"content": "", "tool_calls": [_tc("recall", {"query": "dock"}, 1)]},
              {"content": ""}, {"content": HOST_WORDS},
              {"content": "Picking up the theme."}])
asyncio.run(h.go([("Build the dock.", _STOP_AT_1), ("Go on with the theme.", _NEVER)]))

_asst_rows = [r for r in h.rows if r["role"] == "assistant"]
_row1 = _asst_rows[0] if _asst_rows else {}
check("a: turn 1 stopped on its budget: its row is the Host's words then the stop line, split on the row",
      _row1.get("content", "").startswith(HOST_WORDS + S.REPLY_SEP + STOP)
      and (_row1.get("tags") or {}).get("model_content") == HOST_WORDS
      and (_row1.get("tags") or {}).get("platform_notice") == ["budget"],
      (_row1.get("content"), _row1.get("tags", {}).get("platform_notice")))
_sm = [m for m in (h.sess.messages or []) if m.get("role") == "assistant"]
check("a: sess.messages keeps what the person saw (the stop line) beside the split",
      bool(_sm) and _sm[0].get("content") == _row1.get("content")
      and _sm[0].get("model_content") == HOST_WORDS and _sm[0].get("platform_notice") == ["budget"],
      _sm[:1])
_t2 = h.calls[h.turn_starts[1]] if len(h.turn_starts) > 1 and len(h.calls) > h.turn_starts[1] else []
_hist = [m for m in _t2 if m.get("role") in ("user", "assistant")]
_asst = [str(m.get("content") or "") for m in _t2 if m.get("role") == "assistant"]
_sys = [str(m.get("content") or "") for m in _t2 if m.get("role") == "system"]
check("a: the second turn's history came from the session (recall returned nothing): the person's first "
      "message and the Host's reply are in it",
      any(m.get("role") == "user" and m.get("content") == "Build the dock." for m in _hist)
      and HOST_WORDS in _asst, (len(h.calls), h.turn_starts, [m.get("role") for m in _t2]))
check("a: no assistant content in the second turn carries the notice; the stop is a platform note",
      bool(_asst) and all("reached its budget" not in a and "question box" not in a for a in _asst)
      and ME.PLATFORM_NOTES["budget"] in _sys,
      ([a[-120:] for a in _asst], [s[:80] for s in _sys if s.startswith("[platform]")]))

# ── b. knowledge_toc.scope_episodes ───────────────────────────────────────
_OLD_STALL = ME._OLD_STALL_TEXTS[0]
_EPISODES = [
    # (turns of the episode, oldest first)
    [{"content": HOST_WORDS + "\n\n" + STOP + " The choice to go on is in the question box above.",
      "metadata": {"role": "assistant", "model_content": HOST_WORDS, "platform_notice": ["budget"]}}],
    [{"content": "Build the dock.", "metadata": {"role": "user"}},
     {"content": "On it.\n\n" + STOP, "metadata": {"role": "assistant", "model_content": "On it.",
                                                   "platform_notice": ["budget"]}}],
    [{"content": _OLD_STALL, "metadata": {"role": "assistant"}}],
]


class EpisodePool:
    """Answers the episodes query the way its aggregate does (checked against
    PostgreSQL 17): the preview is the first user message, else
    the first turn; `preview_md`, only when the query asks for it, is the
    first turn's metadata when the episode has no user message."""

    async def fetch(self, q, *a):
        out = []
        for i, turns in enumerate(_EPISODES):
            users = [t for t in turns if t["metadata"].get("role") == "user"]
            row = {"first_turn_id": 10 * i + 1, "last_turn_id": 10 * i + len(turns),
                   "started_at": None, "ended_at": None, "turn_count": len(turns),
                   "preview": (users or turns)[0]["content"]}
            if "preview_md" in q:
                row["preview_md"] = None if users else json.dumps(turns[0]["metadata"])
            out.append(row)
        return out


_eps = asyncio.run(KT.scope_episodes(EpisodePool(), 9003, {"kind": "thread", "ref": "t-1"}))
check("b: an episode previewing a stopped reply names the notice apart: platform_notice + the "
      "Host's own words; the preview the person sees is unchanged",
      len(_eps) == 3 and _eps[0].get("platform_notice") == ["budget"]
      and _eps[0].get("host_preview") == HOST_WORDS
      and _eps[0].get("preview") == _EPISODES[0][0]["content"][:200], _eps[:1])
check("b: an old row that is exactly the old stall text previews as a stall notice with no Host words",
      len(_eps) == 3 and _eps[2].get("platform_notice") == ["stall"] and _eps[2].get("host_preview") == "",
      _eps[2:])
check("b: an episode with the person's message previews it, untouched",
      len(_eps) == 3 and _eps[1].get("preview") == "Build the dock."
      and "platform_notice" not in _eps[1] and "host_preview" not in _eps[1], _eps[1:2])

# ── c. context_budget.build_thread_context's history preview ──────────────
from mastercode import context_budget as CB  # noqa: E402
_STOPPED = HOST_WORDS + "\n\n" + STOP + " The choice to go on is in the question box above."


class PreviewPool:
    """The thread as stored, newest first as the recall query returns rows:
    the person's message, then the stopped reply. Every other read is empty."""

    async def fetch(self, q, *a):
        if "SELECT id, content, metadata, created_at, project_id, source_session" in q:
            return [{"id": 2, "content": _STOPPED, "created_at": None, "project_id": None,
                     "source_session": "s",
                     "metadata": {"role": "assistant", "model_content": HOST_WORDS,
                                  "platform_notice": ["budget"]}},
                    {"id": 1, "content": "Build the dock.", "created_at": None, "project_id": None,
                     "source_session": "s", "metadata": {"role": "user"}}]
        return []

    async def fetchrow(self, q, *a):
        return None

    async def fetchval(self, q, *a):
        return None


_pv = asyncio.run(CB.build_thread_context(PreviewPool(), 9003, {"kind": "thread", "ref": "t-1"}))
_pv_rows = _pv.get("history_preview") or []
check("c: the History window preview is the window the model gets: the person's message, the "
      "Host's own words, and the stop as the platform's note row (never in the Host's words, never dropped)",
      _pv_rows == [{"role": "user", "excerpt": "Build the dock."},
                   {"role": "assistant", "excerpt": HOST_WORDS},
                   {"role": "system", "excerpt": ME.PLATFORM_NOTES["budget"]}], _pv_rows)

print(f"  (HTTP clients refused: {len(_NET_TRIES)})")
if FAILS:
    print(f"[g1-r13] {len(FAILS)} FAIL")
    sys.exit(1)
print("[g1-r13] all pass")
