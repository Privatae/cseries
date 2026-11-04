#!/usr/bin/env python3
"""An embedding model a machine serves is never offered as a chat model.

brain/mastercode/local_model_kind.py asks each model's server what it is:
ollama's POST /api/show `capabilities`, and on an OpenAI-compatible server
(llama.cpp, vLLM) one test embed for a model whose name or the memory store
says it may be an embedder. Read by GET /api/inference/local-models (each row
marked `embedding`), inference_config.local_chat_models (the model chat runs
on when nobody chose one) and apply_role_route (a text role's local model).

Pinned, against a stand-in ollama and llama.cpp on 127.0.0.1 (real HTTP):
  1. ollama: a model whose capabilities are ["embedding"] is an embedder
     whatever its name; ["completion", ...] is a chat model even when its
     name says embed
  2. llama.cpp: nomic-embed-text answers the test embed -> embedder; a chat
     model is not test-embedded at all (a router would load it)
  3. GET /local-models marks each row; the default chat pick and the route
     writer leave the embedders out (409 in words)
  4. a server that cannot be asked: the name decides, as before
  5. outside a running Host nothing is dialled (no pool, no stand-in asker)

APP_ROOT=<repo> python3 scripts/test_local_model_kind.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain"), os.path.join(_ROOT, "scripts")]
from gate_loopback import ClientClosesFirst  # noqa: E402

from mastercode import local_model_kind as lmk  # noqa: E402
from mastercode import embed_backends, inference_config as ic, model_backends  # noqa: E402

FAILS: list[str] = []
SEEN: list[tuple[str, str]] = []


def check(name, ok, detail=""):
    print(("OK   " if ok else "FAIL ") + name + ("" if ok else f"  -- {detail}"))
    if not ok:
        FAILS.append(name)


OLLAMA_CAPS = {"mystery:latest": ["embedding"], "llama3.2:3b": ["completion", "tools"],
               "chat-embed-tuned:7b": ["completion"]}
LLAMA_EMBEDDERS = {"nomic-embed-text"}


class H(ClientClosesFirst, BaseHTTPRequestHandler):
    # ClientClosesFirst: the CI runner's firewall refuses a loopback SYN that
    # lands on a TIME_WAIT the server left (scripts/gate_loopback.py).
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        SEEN.append((self.server.name, self.path + " " + str(body.get("model"))))
        if self.server.name == "ollama" and self.path == "/api/show":
            caps = OLLAMA_CAPS.get(body.get("model"))
            if caps is None:
                return self._send(404, {"error": "model not found"})
            return self._send(200, {"capabilities": caps})
        if self.server.name == "llama" and self.path == "/v1/embeddings":
            if body.get("model") in LLAMA_EMBEDDERS:
                return self._send(200, {"data": [{"embedding": [0.1] * 768}]})
            return self._send(501, {"error": {"message": "This server does not support embeddings. "
                                                         "Start it with `--embeddings`"}})
        return self._send(404, {})

    def _send(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def serve(name):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.name = name
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


OLL_SRV, OLL = serve("ollama")
LLA_SRV, LLA = serve("llama")


def rows():
    return [
        {"id": "llama3.2:3b", "bastion": "desk", "endpoint": OLL, "kind": "ollama"},
        {"id": "mystery:latest", "bastion": "desk", "endpoint": OLL, "kind": "ollama"},
        {"id": "chat-embed-tuned:7b", "bastion": "desk", "endpoint": OLL, "kind": "ollama"},
        {"id": "qwen", "bastion": "desk", "endpoint": LLA, "kind": "llm-openai"},
        {"id": "nomic-embed-text", "bastion": "desk", "endpoint": LLA, "kind": "llm-openai"},
    ]


class _M:
    def __init__(self):
        self.undo = []

    def set(self, obj, name, value):
        self.undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def restore(self):
        for obj, name, old in reversed(self.undo):
            setattr(obj, name, old)


def with_servers(fn):
    def run():
        m = _M()
        lmk._reset_for_tests()
        SEEN.clear()
        # The stand-in servers are on this machine's loopback: no Hub forward.
        m.set(embed_backends, "peer_forward_for", lambda ep, b: None)
        m.set(lmk, "ASK_OVERRIDE", lmk._ask)
        try:
            fn()
        finally:
            m.restore()
            lmk._reset_for_tests()
    run.__name__ = fn.__name__
    return run


@with_servers
def test_the_servers_say_which_models_embed():
    kinds = asyncio.run(lmk.classify(rows(), wait_s=10))
    by_id = {k[1]: v for k, v in kinds.items()}
    check("1. ollama capabilities ['embedding'] -> an embedder, whatever its name",
          by_id.get("mystery:latest") is True, by_id)
    check("1. ollama capabilities with 'completion' -> chat, even named for embedding",
          by_id.get("chat-embed-tuned:7b") is False and by_id.get("llama3.2:3b") is False, by_id)
    check("2. llama.cpp: nomic-embed-text answers the test embed -> an embedder",
          by_id.get("nomic-embed-text") is True, by_id)
    check("2. llama.cpp: a chat model is a chat model and is never test-embedded",
          by_id.get("qwen") is False and ("llama", "/v1/embeddings qwen") not in SEEN, SEEN)
    n = len(SEEN)
    asyncio.run(lmk.classify(rows(), wait_s=10))
    check("answers are kept: a second read asks nothing", len(SEEN) == n, SEEN[n:])


@with_servers
def test_the_list_the_default_and_the_writer_leave_embedders_out():
    from server import brain_service
    from server.routes import inference as inf

    class Pool:
        async def fetchrow(self, q, *a):
            return {"behavioral_params": "{}"} if "entity_registry" in q else None

        async def fetch(self, q, *a):
            return []

        async def execute(self, q, *a):
            return "OK"
    m = _M()

    async def _list(pool, eid, **kw):
        return rows()

    async def _match(pool, eid, model, allowed=None):
        return next((r for r in rows() if r["id"] == model), None)

    async def _own(pool, eid):
        return set()
    m.set(brain_service, "get_pool", lambda: Pool())
    m.set(brain_service, "_entity_id", 1)
    m.set(model_backends, "list_local_models", _list)
    m.set(model_backends, "match_local_model", _match)
    m.set(ic, "own_machine_tokens", _own)
    try:
        got = asyncio.run(inf.get_local_models())["local_models"]
        marks = {r["id"]: r.get("embedding") for r in got}
        check("3. GET /local-models marks every row; the embedders read embedding: true",
              marks == {"llama3.2:3b": False, "mystery:latest": True, "chat-embed-tuned:7b": False,
                        "qwen": False, "nomic-embed-text": True}, marks)
        cat = {r["id"]: r.get("embedding") for r in asyncio.run(inf._table_local_models())}
        check("3. the catalog's copy of the list is marked the same way",
              cat == marks, cat)
        chat = [r["id"] for r in asyncio.run(ic.local_chat_models(Pool(), 1))]
        check("3. the default chat pick never takes an embedder (the probe's, not the name's)",
              "mystery:latest" not in chat and "nomic-embed-text" not in chat
              and "chat-embed-tuned:7b" in chat and chat[0] == "qwen", chat)
        try:
            asyncio.run(inf.apply_role_route(inf.SetRoleModel(role="chat", provider="local",
                                                              model="mystery:latest")))
            check("3. the route writer refuses an embedder for chat", False, "no refusal")
        except inf.HTTPException as e:
            check("3. the route writer refuses an embedder for chat, in words",
                  e.status_code == 409 and "embedding model" in str(e.detail), e.detail)
    finally:
        m.restore()


@with_servers
def test_a_server_that_cannot_be_asked_reads_by_the_name():
    gone = [{"id": "nomic-embed-text", "bastion": "desk", "endpoint": "http://127.0.0.1:9", "kind": "ollama"},
            {"id": "llama3.2:3b", "bastion": "desk", "endpoint": "http://127.0.0.1:9", "kind": "ollama"}]
    kinds = asyncio.run(lmk.classify(gone, wait_s=10))
    check("4. nobody answers: the name decides (nomic is an embedder, llama chats)",
          [kinds[(r["endpoint"], r["id"])] for r in gone] == [True, False], kinds)


def test_nothing_is_dialled_outside_a_host():
    lmk._reset_for_tests()
    SEEN.clear()
    kinds = asyncio.run(lmk.classify(rows(), wait_s=2))
    check("5. no pool and no stand-in asker: no request leaves, the name decides",
          not SEEN and kinds[(OLL, "mystery:latest")] is False and kinds[(LLA, "nomic-embed-text")] is True,
          (SEEN, kinds))


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                check(name, False, repr(e))
    OLL_SRV.shutdown()
    LLA_SRV.shutdown()
    print(f"\nFAILURES: {len(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
