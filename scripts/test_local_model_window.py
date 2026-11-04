#!/usr/bin/env python3
"""A local model server's context size: learned from the server, fitted to,
and said plainly when a request still cannot fit.

A llama.cpp server started with `-c 4096` refused the Host's first turn
(~4,650 tokens) and the person read only "Local model error: 400". The Host
now reads the server's own context size (llama.cpp GET /props
default_generation_settings.n_ctx; GET /v1/models: llama.cpp meta.n_ctx, vLLM
max_model_len), fits what it sends with the trim every other provider's
window already gets (context_budget), learns the size from the server's
refusal when the server publishes none, and when the request still does not
fit, tells the person what the server allows and what to start it with.

Container-free: APP_ROOT=<repo> python3 scripts/test_local_model_window.py.
The model servers are real HTTP servers on 127.0.0.1 (port 0) speaking
llama.cpp's and vLLM's wire, their refusal bodies in the shape their sources
build them (tools/server/server-context.cpp, server-common.cpp, server-task.cpp
read 2026-11-04; vllm serving_engine.py). The turns run the real
EntityRuntime.send_message and the real provider gateway with a stand-in pool.

  A. parsing: /props, /v1/models, the refusals of both servers, the typed code
  B. the probe against a live server: /props, /v1/models when /props is
     refused, nothing from n_ctx_train, cached; the window it learns beats
     every table
  C. the local wire: a refusal is told in plain words, its window learned,
     the person's message sent whole; vLLM's output-size refusal is resent
     once with the reply's room the window leaves
  D. a real turn on a 4,096-token server: the person reads what the server
     allows and the -c to start it with, and every request carried their
     message whole; on a large server the turn is answered with the window
     read before the first call
  E. a turn whose window was unknown: the server's refusal names it, the turn
     trims to it and retries once, the person's words kept

Exit 0 = all pass; exit 1 + FAIL lines otherwise.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

APP_ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_ROOT)
sys.path.insert(0, os.path.join(APP_ROOT, "brain"))
sys.path.insert(0, os.path.join(APP_ROOT, "scripts"))
logging.basicConfig(level=logging.ERROR)

from aiohttp import web  # noqa: E402

FAILS: list[str] = []


def check(name, cond, extra=""):
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"[local-window] FAIL {name} {extra}")


from mastercode import context_budget as CB  # noqa: E402
from mastercode import model_backends as MB  # noqa: E402
from mastercode import provider_gateway as PG  # noqa: E402
from mastercode import attachment_parts as AP  # noqa: E402


def reset():
    CB._SERVER_WINDOWS.clear()
    MB._WINDOW_PROBES.clear()
    try:
        from server.routes import inference as INF
        INF._ROUTE_WARMS.clear()
    except Exception:  # noqa: BLE001
        pass


# ── A. parsing ──────────────────────────────────────────────────────────────
print("A. parsing")
LLAMA_BODY = json.dumps({"error": {
    "code": 400,
    "message": "request (4650 tokens) exceeds the available context size (4096 tokens), try increasing it",
    "type": "exceed_context_size_error", "n_prompt_tokens": 4650, "n_ctx": 4096}})
check("llama.cpp /props: the slot's n_ctx",
      CB.server_window_from_props({"default_generation_settings": {"n_ctx": 4096, "params": {}},
                                   "total_slots": 1}) == 4096)
check("/props of another shape is no window", CB.server_window_from_props({"foo": 1}) is None)
check("llama.cpp /v1/models meta.n_ctx, never n_ctx_train",
      CB.server_window_from_models({"data": [{"id": "tiny.gguf", "meta": {"n_ctx": 8192, "n_ctx_train": 131072}}]},
                                   "tiny.gguf") == 8192)
check("n_ctx_train alone is no window",
      CB.server_window_from_models({"data": [{"id": "tiny.gguf", "meta": {"n_ctx_train": 131072}}]},
                                   "tiny.gguf") is None)
check("vLLM /v1/models max_model_len, matched by id",
      CB.server_window_from_models({"data": [{"id": "other", "max_model_len": 1},
                                             {"id": "Qwen/Qwen3-8B", "max_model_len": 32768}]},
                                   "Qwen/Qwen3-8B") == 32768)
check("a model matched by its alias", CB.server_window_from_models(
    {"data": [{"id": "/m/a.gguf", "aliases": ["chat"], "meta": {"n_ctx": 2048}}]}, "chat") == 2048)
_r = CB.server_window_from_refusal(LLAMA_BODY)
check("llama.cpp's typed refusal: its window and the request's size",
      _r == {"window": 4096, "prompt": 4650, "server": "llama.cpp"}, _r)
_r = CB.server_window_from_refusal(json.dumps({"error": {
    "code": 400, "type": "exceed_context_size_error",
    "message": "input (9000 tokens) is larger than the max context size (8192 tokens). skipping"}}))
check("llama.cpp's context-shift refusal read from its words", _r and _r["window"] == 8192 and _r["prompt"] == 9000, _r)
_r = CB.server_window_from_refusal(json.dumps({"object": "error", "message": (
    "This model's maximum context length is 8192 tokens. However, you requested 12000 tokens "
    "(4000 in the messages, 8000 in the completion). Please reduce the length of the messages or completion.")}))
check("vLLM's 'you requested' refusal: the input is the messages' share",
      _r == {"window": 8192, "prompt": 4000, "server": "vllm"}, _r)
check("a refusal that is not a window one is none",
      CB.server_window_from_refusal(json.dumps({"error": {"code": 400, "message": "bad json"}})) is None)
check("llama.cpp's refusal is a context overflow (typed code)",
      CB.is_context_overflow({"error": "x", "error_body": LLAMA_BODY, "content": ""}))
check("llama.cpp's refusal without its typed code is an overflow by its words",
      CB.is_context_overflow({"error": "x", "content": "", "error_body": json.dumps({"error": {
          "code": 400, "message": "request (4650 tokens) exceeds the available context size (4096 tokens), "
                                  "try increasing it"}})}))
check("and so is its context-shift wording",
      CB.is_context_overflow({"error": "x", "content": "", "error_body": json.dumps({"error": {
          "code": 400, "message": "input (9000 tokens) is larger than the max context size (8192 tokens). skipping"}})}))
check("llama.cpp's typed code is read past the numeric code", CB.refusal_parts(LLAMA_BODY)[1] == "exceed_context_size_error",
      CB.refusal_parts(LLAMA_BODY))
_m = CB.local_window_message("tiny.gguf", 4096, 4650, "llama.cpp")
check("the person reads what the server allows, what the request needs, and the -c to use",
      "allows 4,096 tokens" in _m and "needs 4,650" in _m and "-c 16384" in _m, _m)
check("vLLM's setting is --max-model-len",
      "--max-model-len 32768" in CB.local_window_message("q", 8192, 9000, "vllm"))
check("the suggestion is above the server's size and holds the request twice",
      CB.suggested_context_size(20000, 16384) == 65536 and CB.suggested_context_size(None, 32768) == 65536)


# ── fake model servers ──────────────────────────────────────────────────────
class FakeServer:
    """llama.cpp's or vLLM's wire. Tokens are counted as characters / 4 over
    the messages' text, as close to a tokenizer as the test needs."""

    def __init__(self, kind="llama.cpp", n_ctx=4096, props=True, models_meta=True, model="tiny.gguf"):
        self.kind, self.n_ctx, self.props, self.models_meta, self.model = kind, n_ctx, props, models_meta, model
        self.hits: list[str] = []
        self.chats: list[dict] = []
        self.runner = None
        self.origin = ""

    def tokens(self, body):
        return sum(len(str(m.get("content") or "")) for m in body.get("messages") or []) // 4

    async def h_props(self, req):
        self.hits.append("/props")
        if not self.props:
            return web.json_response({"error": {"code": 501, "message": "This server does not support "
                                                 "changing global properties.", "type": "not_supported_error"}},
                                     status=501)
        return web.json_response({"default_generation_settings": {"n_ctx": self.n_ctx, "params": {}},
                                  "total_slots": 1, "model_path": "/models/" + self.model})

    async def h_models(self, req):
        self.hits.append("/v1/models")
        row = {"id": self.model, "object": "model", "owned_by": "llamacpp"}
        if self.kind == "vllm":
            row["max_model_len"] = self.n_ctx
        elif self.models_meta:
            row["meta"] = {"n_ctx": self.n_ctx, "n_ctx_train": 131072}
        else:
            row["meta"] = {"n_ctx_train": 131072}
        return web.json_response({"object": "list", "data": [row]})

    async def h_chat(self, req):
        body = await req.json()
        self.hits.append("/v1/chat/completions")
        self.chats.append(body)
        n = self.tokens(body)
        mt = int(body.get("max_tokens") or 0)
        if self.kind == "llama.cpp" and n >= self.n_ctx:
            return web.json_response({"error": {
                "code": 400, "message": f"request ({n} tokens) exceeds the available context size "
                                        f"({self.n_ctx} tokens), try increasing it",
                "type": "exceed_context_size_error", "n_prompt_tokens": n, "n_ctx": self.n_ctx}}, status=400)
        if self.kind == "vllm" and n + mt > self.n_ctx:
            if n < self.n_ctx:
                msg = (f"'max_tokens' or 'max_completion_tokens' is too large: {mt}. This model's maximum "
                       f"context length is {self.n_ctx} tokens and your request has {n} input tokens "
                       f"({mt} > {self.n_ctx} - {n}).")
            else:
                msg = (f"This model's maximum context length is {self.n_ctx} tokens. However, your request "
                       f"has {n} input tokens. Please reduce the length of the input messages.")
            return web.json_response({"object": "error", "message": msg, "type": "BadRequestError",
                                      "code": 400}, status=400)
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(req)
        chunk = {"choices": [{"delta": {"content": "Hello from the local model."}, "finish_reason": None}]}
        await resp.write(f"data: {json.dumps(chunk)}\n\n".encode())
        done = {"choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": n, "completion_tokens": 6, "total_tokens": n + 6}}
        await resp.write(f"data: {json.dumps(done)}\n\n".encode())
        await resp.write(b"data: [DONE]\n\n")
        await resp.write_eof()
        return resp

    async def start(self):
        app = web.Application()
        app.router.add_get("/props", self.h_props)
        app.router.add_get("/v1/models", self.h_models)
        app.router.add_post("/v1/chat/completions", self.h_chat)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.origin = f"http://127.0.0.1:{port}"
        return self

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()


# ── B. the probe ────────────────────────────────────────────────────────────
async def part_b():
    print("B. the probe against a live server")
    reset()
    s = await FakeServer(n_ctx=4096).start()
    try:
        w = await MB.probe_server_window(s.origin, "tiny.gguf")
        check("llama.cpp: the window from /props", w == 4096, (w, s.hits))
        check("the window it read is the model's window for every reader",
              await CB.model_context_window("tiny.gguf", provider="local") == 4096)
        n = len(s.hits)
        await MB.probe_server_window(s.origin + "/v1", "tiny.gguf")
        check("a second look within the TTL asks the server nothing", len(s.hits) == n, s.hits)
    finally:
        await s.stop()
    reset()
    s = await FakeServer(n_ctx=8192, props=False).start()
    try:
        w = await MB.probe_server_window(s.origin, "tiny.gguf")
        check("/props refused (a scoped pairing's bastion): the window from /v1/models", w == 8192, (w, s.hits))
    finally:
        await s.stop()
    reset()
    s = await FakeServer(n_ctx=8192, props=False, models_meta=False).start()
    try:
        w = await MB.probe_server_window(s.origin, "tiny.gguf")
        check("only n_ctx_train published: no window, never the training size", w is None, w)
    finally:
        await s.stop()
    reset()
    s = await FakeServer(kind="vllm", n_ctx=32768, props=False, model="Qwen/Qwen3-8B").start()
    try:
        w = await MB.probe_server_window(s.origin, "Qwen/Qwen3-8B")
        check("vLLM: max_model_len from /v1/models", w == 32768, w)
    finally:
        await s.stop()
    reset()
    # A model whose id a table knows as large: the server's own word wins.
    CB.learn_context_window("local", "claude-local:latest", 4096)
    check("the server's word beats the static table (a 'claude' id is 200k there)",
          await CB.model_context_window("claude-local", provider="local") == 4096)
    reset()


# ── C. the local wire ───────────────────────────────────────────────────────
PERSON = "Please summarise my week. " + "x" * 600


async def part_c():
    print("C. the local wire")
    reset()
    s = await FakeServer(n_ctx=4096).start()
    try:
        msgs = [{"role": "system", "content": "s" * 20000}, {"role": "user", "content": PERSON}]
        r = await PG._call_local(msgs, "tiny.gguf", base_url=s.origin, max_tokens=2048)
        err = str(r.get("error"))
        check("the refusal is told in plain words with the server's numbers and the -c to use",
              err.startswith("The model server running tiny.gguf allows 4,096 tokens") and "-c 16384" in err, err)
        check("never the bare status", "Local model error" not in err, err)
        check("the result carries the window and stays an overflow and a refused request",
              r.get("context_window") == 4096 and CB.is_context_overflow(r) and AP.refused_input(r), r.keys())
        check("the window is learned for the model", CB.learned_context_window("local", "tiny.gguf") == 4096)
        sent = s.chats[-1]["messages"]
        check("the person's message went whole", any(m.get("content") == PERSON for m in sent))
    finally:
        await s.stop()
    reset()
    # vLLM's output-size refusal through the real gateway: resent once.
    s = await FakeServer(kind="vllm", n_ctx=8192, props=False, model="qwen").start()
    try:
        r = await PG.call_model([{"role": "system", "content": "s" * 8000}, {"role": "user", "content": PERSON}],
                                provider_model="qwen", provider="local", base_url=s.origin, max_tokens=16384,
                                _source="test")
        mts = [c.get("max_tokens") for c in s.chats]
        check("vLLM's 'max_tokens too large' is resent once with the room the window leaves",
              r.get("content") == "Hello from the local model." and len(mts) == 2
              and int(mts[1]) < int(mts[0]) and int(mts[1]) <= 8192, (r.get("error"), mts))
    finally:
        await s.stop()
    reset()


# ── D/E. real turns ─────────────────────────────────────────────────────────
from turn_source import sync_turn_attr  # noqa: E402,F401
from mastercode.runtime import session as S  # noqa: E402
from mastercode import inference_config as IC  # noqa: E402
from server import brain_service as BS  # noqa: E402
from mastercode import memory_engine as ME  # noqa: E402


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


class Turns:
    def __init__(self, origin, model="tiny.gguf", scripted=None):
        self.origin, self.model, self.scripted = origin, model, scripted
        self.events: list = []
        self.calls: list = []

    def patch(self):
        self._saved = (IC.resolve_role_route, S._AVAILABLE.get("ceigas_domain_controller"),
                       S._run_close_turn, S._execute_meta_tool, S._build_tool_schemas,
                       BS.get_pool, S._call_entity_model)
        origin, model = self.origin, self.model

        async def route(pool, eid, role, thread_override=None, **kw):
            return {"provider": "local", "model": model, "base_url": origin}

        async def run_close_turn(pool, sess, args, *, prompt_console=None):
            return {"status": "ok", "result": "recorded"}

        async def meta(name, args, **kw):
            return {"status": "ok", "result": "r" * 60000}

        async def schemas(pool, eid, interaction_context=None, **kw):
            return []
        IC.resolve_role_route = route
        S._AVAILABLE["ceigas_domain_controller"] = None
        S._run_close_turn = run_close_turn
        S._execute_meta_tool = meta
        S._build_tool_schemas = schemas
        pool = Pool()
        BS.get_pool = lambda: pool
        if self.scripted is not None:
            harness = self

            async def model_call(messages=None, *a, **kw):
                msgs = messages if messages is not None else (a[0] if a else [])
                harness.calls.append([dict(m) for m in msgs])
                r = harness.scripted.pop(0) if harness.scripted else {"content": "(out)"}
                return {"tokens_used": 1, "prompt_tokens": 1, "completion_tokens": 1, "tool_calls": None, **r}
            S._call_entity_model = model_call
        self.rt = S.EntityRuntime(pool)

        async def ctx(entity_id, **kw):
            return S.EntitySession(
                session_id="lw-sess", entity_id=entity_id,
                entity={"id": entity_id, "name": "Sage", "behavioral_params": {},
                        "permission_overrides": {"bot_capabilities": 255}},
                interaction_context="app:workbench")
        self.rt._build_chat_context_transient = ctx

    def unpatch(self):
        (IC.resolve_role_route, S._AVAILABLE["ceigas_domain_controller"], S._run_close_turn,
         S._execute_meta_tool, S._build_tool_schemas, BS.get_pool, S._call_entity_model) = self._saved

    async def turn(self, message):
        async def cb(e):
            self.events.append(e)
        return await self.rt.send_message(entity_id=9101, user_message=message, progress_cb=cb)


def _said(result, events) -> str:
    parts = [json.dumps(result, default=str)] + [json.dumps(e, default=str) for e in events]
    return "\n".join(parts)


async def part_d():
    print("D. real turns on a llama.cpp server")
    reset()
    # The stand-in Host's prompt is ~2,700 tokens (the reported first turn was
    # ~4,650), so a 2,048-token server is the small one here.
    s = await FakeServer(n_ctx=2048).start()
    t = Turns(s.origin)
    t.patch()
    try:
        res = await t.turn(PERSON)
        said = _said(res, t.events)
        check("on a 2,048-token server the person reads what it allows and the -c to start it with",
              "allows 2,048 tokens" in said and "-c 16384" in said, said[:600])
        check("never only 'Local model error: 400'", "Local model error: 400" not in said)
        check("the window was read from the server before the first request",
              "/props" in s.hits and s.hits.index("/props") < s.hits.index("/v1/chat/completions"), s.hits)
        check("every request carried the person's message whole",
              s.chats and all(any(PERSON == m.get("content") for m in c["messages"]) for c in s.chats),
              len(s.chats))
        check("each request's reply room is held to the window",
              all(int(c.get("max_tokens") or 0) <= 1024 for c in s.chats), [c.get("max_tokens") for c in s.chats])
    finally:
        t.unpatch()
        await s.stop()
    reset()
    s = await FakeServer(n_ctx=65536).start()
    t = Turns(s.origin)
    t.patch()
    try:
        res = await t.turn(PERSON)
        said = _said(res, t.events)
        check("on a 65,536-token server the turn is answered", "Hello from the local model." in said, said[:400])
        check("its history budget was made from the server's window",
              ((res or {}).get("context") or {}).get("window") == 65536, (res or {}).get("context"))
        check("no request was refused, and the reply room is half the window at most",
              s.chats and all(int(c.get("max_tokens") or 0) <= 32768 for c in s.chats),
              [c.get("max_tokens") for c in s.chats])
    finally:
        t.unpatch()
        await s.stop()
    reset()


def _tc(name, i):
    return {"id": f"call_{name}_{i}", "type": "function", "function": {"name": name, "arguments": "{}"}}


async def part_e():
    print("E. a turn whose window was unknown learns it from the refusal")
    reset()
    # No server answers the window probe (nothing listens there): the window
    # is unknown when the turn begins.
    refusal = {"content": "", "error": CB.local_window_message("tiny.gguf", 4096, 20000, "llama.cpp"),
               "error_body": json.dumps({"error": {"code": 400, "type": "exceed_context_size_error",
                                                   "message": "request (20000 tokens) exceeds the available "
                                                              "context size (4096 tokens), try increasing it",
                                                   "n_prompt_tokens": 20000, "n_ctx": 4096}}),
               "context_window": 4096, "refused_status": 400}
    script = [refusal, {"content": "All done."}]
    t = Turns("http://127.0.0.1:9", scripted=script)
    t.patch()
    saved_recall = ME.recall_recent_turns

    async def recall(*a, **kw):
        # An earlier exchange in the thread, made up for this test.
        return [{"role": r, "content": f"{r} turn {i}: " + "h" * 1500}
                for i, r in enumerate(["user", "assistant"] * 3)]
    ME.recall_recent_turns = recall
    try:
        res = await t.turn(PERSON)
        said = _said(res, t.events)
        check("the refused call was retried once and answered", len(t.calls) == 2 and "All done." in said,
              (len(t.calls), said[:300]))
        if len(t.calls) == 2:
            before = sum(len(str(m.get("content") or "")) for m in t.calls[0])
            after = sum(len(str(m.get("content") or "")) for m in t.calls[1])
            check("the retry was trimmed toward the window the server named", after < before, (before, after))
            check("the person's message survived the trim whole",
                  any(m.get("content") == PERSON for m in t.calls[1]))
    finally:
        ME.recall_recent_turns = saved_recall
        t.unpatch()
    reset()


async def part_f():
    print("F. a turn that began with a LARGER window than the server's learns the smaller one")
    reset()
    # The Host believed 131,072 tokens (what the server said before it was
    # restarted with a smaller -c, or a catalog's number); nothing answers the
    # window probe, so the belief stands when the turn begins.
    CB.learn_context_window("local", "tiny.gguf", 131072)
    refusal = {"content": "", "error": CB.local_window_message("tiny.gguf", 4096, 20000, "llama.cpp"),
               "error_body": json.dumps({"error": {"code": 400, "type": "exceed_context_size_error",
                                                   "message": "request (20000 tokens) exceeds the available "
                                                              "context size (4096 tokens), try increasing it",
                                                   "n_prompt_tokens": 20000, "n_ctx": 4096}}),
               "context_window": 4096, "refused_status": 400}
    script = [refusal, {"content": "All done."}]
    t = Turns("http://127.0.0.1:9", scripted=script)
    t.patch()
    saved_recall = ME.recall_recent_turns

    async def recall(*a, **kw):
        # An earlier exchange in the thread, made up for this test.
        return [{"role": r, "content": f"{r} turn {i}: " + "h" * 1500}
                for i, r in enumerate(["user", "assistant"] * 3)]
    ME.recall_recent_turns = recall
    try:
        res = await t.turn(PERSON)
        said = _said(res, t.events)
        check("the refused call was retried once, trimmed to the server's window, and answered",
              len(t.calls) == 2 and "All done." in said, (len(t.calls), said[:300]))
        if len(t.calls) == 2:
            before = sum(len(str(m.get("content") or "")) for m in t.calls[0])
            after = sum(len(str(m.get("content") or "")) for m in t.calls[1])
            check("the retry was smaller than the refused request", after < before, (before, after))
            check("the person's message survived the trim whole",
                  any(m.get("content") == PERSON for m in t.calls[1]))
    finally:
        ME.recall_recent_turns = saved_recall
        t.unpatch()
    reset()


async def main():
    await part_b()
    await part_c()
    await part_d()
    await part_e()
    await part_f()
    await PG.close_all_sessions()


asyncio.run(main())

if FAILS:
    print(f"\n{len(FAILS)} FAIL")
    sys.exit(1)
print("\nall pass")
