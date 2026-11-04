#!/usr/bin/env python3
"""Embedding auto-setup chooses its model by testing it, never by its name.

Auto-setup used to pick only a model whose id was in this host's known list
(memory_engine.MODEL_DIMS). llama.cpp names a model by its file unless it is
given an alias, so a person's `llama-server -m some-embed.gguf --embeddings`
was never picked. Decision (embed-assignment-probe-authoritative): an
embedding endpoint is assigned only when a live test embed proves it serves
the model at the expected dimension; what a machine lists is never enough.
So auto-setup tries a real embed against each candidate the machine lists,
known good models first, takes the dimension from the answer, and keeps a
record of what it chose and what every candidate answered. A chat-only model
that refuses the embed is never chosen.

Container-free: APP_ROOT=<repo> python3 scripts/test_embed_probe_choice.py.
The embedders are a real HTTP server on 127.0.0.1 (port 0) speaking the
OpenAI /v1/embeddings wire; a chat-only model answers as llama.cpp's server
does without --embeddings (501, "This server does not support embeddings.
Start it with `--embeddings`", tools/server/server-context.cpp). The pool is
a stand-in; the bastion's serve lane is its origin on this Host's loopback.
Model ids are made up for the test.

  1. the candidates: known first, then named, then what the llm server lists;
     ollama's other tags (its chat models) are not tried
  2. the choice by test embed: the dimension from the answer; a chat-only
     model and a wrong-dim known model are passed over; a dim with no column
     is refused; known good wins over named
  3. the whole auto-setup: an unconfigured Host and a machine serving
     `some-embed.gguf` end configured with it at the measured dim, and the
     record of the choice is stored; a machine serving only a chat model
     configures nothing

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
logging.basicConfig(level=logging.ERROR)

from aiohttp import web  # noqa: E402

FAILS: list[str] = []


def check(name, cond, extra=""):
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"[embed-probe] FAIL {name} {extra}")


from mastercode import embed_backends as EB  # noqa: E402
from mastercode import embed_autowire as AW  # noqa: E402
from mastercode import model_backends as MB  # noqa: E402
from server.network import serve_relay  # noqa: E402
from server.network import topology  # noqa: E402
from server.routes import embedding as E  # noqa: E402


class Embedder:
    """One OpenAI-compatible server on the person's machine. `dims` maps a
    model id to the dim it embeds at; any other model only chats."""

    def __init__(self, dims):
        self.dims = dims
        self.asked: list[str] = []
        self.origin = ""

    async def h_embed(self, req):
        body = await req.json()
        m = body.get("model")
        self.asked.append(m)
        if m not in self.dims:
            return web.json_response({"error": {"code": 501, "message": "This server does not support "
                                                "embeddings. Start it with `--embeddings`",
                                                "type": "not_supported_error"}}, status=501)
        return web.json_response({"object": "list", "model": m,
                                  "data": [{"object": "embedding", "index": 0,
                                            "embedding": [0.01] * self.dims[m]}]})

    async def start(self):
        app = web.Application()
        app.router.add_post("/v1/embeddings", self.h_embed)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.origin = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        return self

    async def stop(self):
        await self.runner.cleanup()


def bastion(tid, *, llm=(), models=(), caps=("llm-openai",)):
    return {"id": tid, "hostname": f"box{tid}", "online": True, "reported_ip": "",
            "capabilities": list(caps), "models": list(models), "llm_models": list(llm)}


class Pool:
    """The heartbeat rows embed_backends reads, the Host's behavioral_params
    (read and written as apply_embedding_switch does), nothing else."""

    def __init__(self, rows):
        self.rows = {b["hostname"]: b for b in rows}
        self.bp: dict = {}
        self.writes: list = []

    async def fetchrow(self, q, *a):
        if "network_bastion_heartbeat" in q:
            b = self.rows.get(a[0])
            if not b:
                return None
            return {"token_id": b["id"], "reported_ip": b["reported_ip"],
                    "capabilities": json.dumps(b["capabilities"]), "models": json.dumps(b["models"]),
                    "llm_models": json.dumps(b["llm_models"])}
        if "behavioral_params" in q:
            return {"behavioral_params": json.dumps(self.bp)}
        return None

    async def fetchval(self, q, *a):
        if "behavioral_params" in q:
            return json.dumps(self.bp)
        return 0

    async def fetch(self, q, *a):
        return []

    async def execute(self, q, *a):
        if "UPDATE entity_registry SET behavioral_params" in q:
            self.bp = json.loads(a[0])
            self.writes.append(dict(self.bp))
        return "OK"


async def main():
    srv = await Embedder({"/models/some-embed.gguf": 768, "nomic-v1.5.Q8_0.gguf": 768,
                          "nomic-embed-text": 768, "bge-m3": 768, "all-minilm-l6.gguf": 384}).start()
    saved = (MB._serve_lane_endpoint, serve_relay.is_lane_origin, topology.list_bastions,
             E.current, AW._caps_ok, E._load_bp)
    MB._serve_lane_endpoint = lambda tid, service: _ret(srv.origin)
    serve_relay.is_lane_origin = lambda url: str(url or "").startswith(srv.origin)
    try:
        # ── 1. candidates ──────────────────────────────────────────────────
        print("1 the candidates")
        c = EB.embed_candidates(bastion(1, llm=["chat.gguf", "/models/some-embed.gguf", "bge-m3"]))
        check("known first, then named, then the rest the llm server lists",
              [(x["model"], x["why"]) for x in c] ==
              [("bge-m3", "known"), ("/models/some-embed.gguf", "named"), ("chat.gguf", "listed")], c)
        c = EB.embed_candidates(bastion(2, caps=["embed"], models=["llama3.1:8b", "nomic-embed-text"]))
        check("ollama's chat tags are not tried; its embedders are",
              [x["model"] for x in c] == ["nomic-embed-text"], c)
        check("a file named for embedding is named so", EB.embedding_named("/models/some-embed.gguf")
              and not EB.embedding_named("chat.gguf"))
        check("an address-less llama.cpp whose model only a test can judge is an embed lane",
              EB.lane_embed_service(bastion(3, llm=["model.gguf"])) == "llm")

        # ── 2. the choice ──────────────────────────────────────────────────
        print("2 the choice by test embed")
        rows = [bastion(10, llm=["/models/some-embed.gguf"]),
                bastion(11, llm=["chat.gguf", "nomic-v1.5.Q8_0.gguf"]),
                bastion(12, llm=["chat.gguf"]),
                bastion(13, llm=["bge-m3", "/models/some-embed.gguf"]),
                bastion(14, llm=["all-minilm-l6.gguf"]),
                bastion(15, llm=["/models/some-embed.gguf", "nomic-embed-text"])]
        pool = Pool(rows)

        async def _list(p, eid):
            return [dict(r) for r in rows]
        topology.list_bastions = _list

        r = await EB.choose_embed_model_by_probe(pool, 1, "box10")
        check("llama.cpp's -m some-embed.gguf is chosen, its dim from the answer",
              r.get("model") == "/models/some-embed.gguf" and r.get("dim") == 768, r)
        check("the record says what it answered", r.get("tried") == [
            {"model": "/models/some-embed.gguf", "result": "embeds at 768d"}], r.get("tried"))
        srv.asked.clear()
        r = await EB.choose_embed_model_by_probe(pool, 1, "box11")
        check("a model named only by its file is found by testing every one listed",
              r.get("model") == "nomic-v1.5.Q8_0.gguf" and "chat.gguf" in srv.asked, (r, srv.asked))
        check("the chat-only model's refusal is in the record",
              any(t["model"] == "chat.gguf" and "no vector" in t["result"] for t in r.get("tried") or []),
              r.get("tried"))
        r = await EB.choose_embed_model_by_probe(pool, 1, "box12")
        check("a machine with only a chat model: nothing chosen, and it says why",
              r.get("error") and not r.get("model") and r["tried"][0]["model"] == "chat.gguf", r)
        r = await EB.choose_embed_model_by_probe(pool, 1, "box13")
        check("a known model answering at the wrong dim is passed over (bge-m3 is 1024d, said 768d)",
              r.get("model") == "/models/some-embed.gguf"
              and "expected 1024d" in r["tried"][0]["result"], r)
        r = await EB.choose_embed_model_by_probe(pool, 1, "box14")
        check("a dim with no storage column is refused (384d)",
              r.get("error") and "no column" in r["tried"][0]["result"], r)
        r = await EB.choose_embed_model_by_probe(pool, 1, "box15")
        check("a known good model is preferred over one picked by its name",
              r.get("model") == "nomic-embed-text" and r["tried"][0]["model"] == "nomic-embed-text", r)

        # ── 3. the whole auto-setup ────────────────────────────────────────
        print("3 auto-setup on an unconfigured Host")

        async def _current():
            return {"configured": bool(pool.bp.get("embedding_model")),
                    "model": pool.bp.get("embedding_model"), "dim": pool.bp.get("embedding_dim")}

        async def _bp(p, eid):
            return dict(pool.bp)

        async def _caps(p, eid):
            return True
        E.current = _current
        E._load_bp = _bp
        AW._caps_ok = _caps
        AW._last_failed.clear()
        res = await AW._autowire_now(pool, 1, "box10")
        check("wired with the model its test embed proved, at the measured dim",
              res.get("wired") and res.get("model") == "/models/some-embed.gguf" and res.get("dim") == 768, res)
        cb = pool.bp.get("embedding_chosen_by") or {}
        check("the config holds the model, the dim and the record of the choice",
              pool.bp.get("embedding_model") == "/models/some-embed.gguf" and pool.bp.get("embedding_dim") == 768
              and pool.bp.get("embedding_bastion") == "box10" and cb.get("how") == "test embed"
              and cb.get("tried") and cb.get("model") == "/models/some-embed.gguf", pool.bp)
        res = await AW._autowire_now(pool, 1, "box10")
        check("a configured Host is never re-pointed", not res.get("wired") and "already" in res.get("reason", ""),
              res)

        pool.bp = {}
        AW._last_failed.clear()
        res = await AW._autowire_now(pool, 1, "box12")
        check("a machine serving only a chat model configures nothing", not res.get("wired")
              and not pool.bp.get("embedding_model") and res.get("tried"), (res, pool.bp))

        # set_embed_bastion with no model and none configured: the same test.
        pool.bp = {}
        res = await EB.set_embed_bastion(pool, 1, "box11")
        check("use_bastion with no model on a fresh Host picks by test embed and records it",
              res.get("model") == "nomic-v1.5.Q8_0.gguf"
              and (pool.bp.get("embedding_chosen_by") or {}).get("model") == "nomic-v1.5.Q8_0.gguf", (res, pool.bp))
        # Moving only WHERE it embeds (no model named) keeps the model, so the
        # record of how it was chosen still describes it.
        _rec = dict(pool.bp.get("embedding_chosen_by") or {})
        res = await EB.set_embed_bastion(pool, 1, "box11")
        check("use_bastion with no model on a configured Host keeps the record of the choice",
              pool.bp.get("embedding_model") == "nomic-v1.5.Q8_0.gguf"
              and (pool.bp.get("embedding_chosen_by") or {}) == _rec and _rec, (res, pool.bp))
        check("and says the model was kept, not picked again",
              "model kept unchanged" in str(res.get("note")), res.get("note"))
        # A model named by hand clears the record: it was not chosen by a test.
        res = await EB.set_embed_bastion(pool, 1, "box11", model="nomic-v1.5.Q8_0.gguf", dim=768)
        check("a model named by hand leaves no 'chosen by test embed' record",
              "embedding_chosen_by" not in pool.bp, pool.bp)
    finally:
        (MB._serve_lane_endpoint, serve_relay.is_lane_origin, topology.list_bastions,
         E.current, AW._caps_ok, E._load_bp) = saved
        await srv.stop()


async def _ret(v):
    return v


asyncio.run(main())

if FAILS:
    print(f"\n{len(FAILS)} FAIL")
    sys.exit(1)
print("\nall pass")
