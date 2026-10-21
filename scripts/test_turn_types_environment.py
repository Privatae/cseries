#!/usr/bin/env python3
"""
Turn types: the environment from the type.

A typed turn takes three more things from its profile — the SKILL it reads,
the CONTEXT the recall reads, the MODEL it runs on — and an untyped turn
takes none of them: every helper is an identity for None, asserted FIRST.
Container-free: APP_ROOT=<repo> python3 scripts/<this>. No pod, no pool, no
model; the recall engine and the route are driven with stubs.

  1. untyped is unchanged: context_filter(None), type_skill(None) are None;
     route_for_type(None) returns the chat route object itself; federated
     recall with no context searches both stores exactly as before; the
     memory block's fallback still runs for an untyped empty result
  2. context_filter normalizes the column: projects → memory 'projects';
     'projects' with none listed reads NOTHING (never everything); documents
     defaults on; a bad memory scope falls to 'all'; an empty context is
     None; a CLOSED type reads nothing at all
  3. the memory arm's WHERE gains `project_id = ANY($n::int[])` only when
     project_ids is given, and recall_memories actually hands its
     project_ids to that WHERE (the arms see the clause)
  4. federated_recall under a context: memory 'none' never calls the memory
     arm and says so under `narrowed`; documents False never calls the
     documents store; 'projects' passes the listed ids to the memory arm; the
     caller's own scope still narrows on top; `narrowed` reaches the model
     through format_recall_text
  5. build_memory_context: memory 'none' renders no block and touches no
     store; 'projects' never reads the standing list, reads the taught rows
     only through the pre-fire's own project test (rules have
     places), and hands the context to the pre-fire (counted, not assumed)
  6. the memory block (_b_memory) never falls back to the unscoped
     entity_context summary under a scoped type
  7. route_for_type: no model → the chat route; the chat model by name → the
     chat route; another model → the ONE normalizer's provider; a
     `provider:model` name → that provider; a model a machine on the network
     serves → LOCAL with its endpoint; the person's effort rides through; a
     failure → the chat route, never a failed turn
  8. session.py wiring: the type is resolved BEFORE the chat route and the
     route is overridden right after it unless a per-thread pick exists; the
     type's skill replaces the vote and rides the STATIC system message; the
     recall tool passes the type's context; the memory block passes it

Exit 0 = all pass; exit 1 + [turn-types-env] FAIL lines otherwise.
"""

import asyncio
import os
import re
import sys

FAILS = []


def check(name: str, ok: bool, detail: str = ""):
    print(f"[turn-types-env] {'ok  ' if ok else 'FAIL'} {name}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def _type(**over):
    base = {
        "id": "00000000-0000-0000-0000-000000000001", "name": "Finance",
        "status": "active", "keywords": ["invoice"], "context": {},
        "skill": "finance", "tool_groups": ["vault", "comms"],
        "grant_names": ["read_context", "write_context", "external_api"],
        "keys": ["stripe-live"], "bastions": ["books-1"], "model": None,
        "thread_id": None, "version": 1,
    }
    base.update(over)
    return base


def main() -> int:
    app_root = os.environ.get("APP_ROOT", "/app")
    sys.path.insert(0, os.path.join(app_root, "brain"))
    # `server.routes.context` (the documents store's search) lives at the
    # repo root, beside `brain/`.
    sys.path.insert(0, app_root)

    from mastercode.runtime import turn_types as tt
    from mastercode import memory_retrieval as mr
    from mastercode import memory_engine as me
    from mastercode.runtime import prompt_schema as ps

    real_recall_memories = mr.recall_memories

    # ── 1. UNTYPED IS UNCHANGED.
    check("untyped: context_filter(None) is None", tt.context_filter(None) is None)
    check("untyped: type_skill(None) is None", tt.type_skill(None) is None)
    chat_route = {"provider": "openrouter", "model": "test/model", "base_url": None}
    r = asyncio.run(tt.route_for_type(None, 1, None, chat_route))
    check("untyped: route_for_type returns the chat route object itself", r is chat_route)

    calls = {"memory": [], "documents": []}

    async def fake_recall_memories(pool, entity_id, query, *, limit=15, **kw):
        calls["memory"].append(kw)
        return {"hits": [], "arms": ["lexical"], "degraded": False,
                "semantic_unavailable": None, "lexical_unavailable": None}

    async def fake_search_context_service(pool, entity_id, *, query="", limit=20):
        calls["documents"].append({"query": query, "limit": limit})
        return {"results": [], "query": query, "arms": ["lexical"]}

    mr.recall_memories = fake_recall_memories
    import server.routes.context as ctx_routes
    ctx_routes.search_context_service = fake_search_context_service

    out = asyncio.run(mr.federated_recall(None, 1, "q", scope="all"))
    check("untyped: federated recall with no context searches memory and documents",
          len(calls["memory"]) == 1 and len(calls["documents"]) == 1
          and "narrowed" not in out and calls["memory"][0].get("project_ids") is None,
          f"calls={calls} out={out}")

    # ── 2. context_filter.
    check("empty context narrows nothing (None)", tt.context_filter(_type(context={})) is None)
    f = tt.context_filter(_type(context={"projects": [1, "2", "x"]}))
    check("projects listed → memory 'projects', ids as ints, documents on",
          f == {"projects": [1, 2], "documents": True, "memory": "projects"}, f"got {f}")
    f = tt.context_filter(_type(context={"memory": "projects"}))
    check("'projects' with none listed reads NOTHING, never everything",
          f is not None and f["memory"] == "none", f"got {f}")
    f = tt.context_filter(_type(context={"documents": False}))
    check("documents can be switched off on their own",
          f == {"projects": None, "documents": False, "memory": "all"}, f"got {f}")
    f = tt.context_filter(_type(context={"memory": "everything?"}))
    check("a bad memory scope falls to 'all'", f is not None and f["memory"] == "all", f"got {f}")
    f = tt.context_filter(_type(context={"memory": "none", "projects": [3]}))
    check("an explicit 'none' wins over listed projects", f["memory"] == "none", f"got {f}")
    f = tt.context_filter(tt.closed_type("store unreadable"))
    check("a CLOSED type reads nothing: memory 'none', documents off",
          f == {"projects": None, "documents": False, "memory": "none"}, f"got {f}")
    f = tt.context_filter({"status": "closed", "context": {}})
    check("a closed dict with an empty context still reads nothing",
          f is not None and f["memory"] == "none" and f["documents"] is False, f"got {f}")

    # ── 3. The memory arm's WHERE, and that recall_memories hands it on.
    sql, params = mr._canonical_where(7)
    check("no project_ids → no project clause", "project_id" not in sql and params == [7], f"{sql} {params}")
    sql, params = mr._canonical_where(7, project_ids=[1, "2"])
    check("project_ids → `project_id = ANY($n::int[])` with int params",
          "project_id = ANY($2::int[])" in sql and params == [7, [1, 2]], f"{sql} {params}")
    seen_where = []

    async def fake_embed_query(pool, entity_id, query):
        return None, "no embedder in this gate"

    async def fake_vector_arm(pool, entity_id, query, *, where_sql, params, **kw):
        seen_where.append(("vector", where_sql, params))
        return [], True, None

    async def fake_lexical_arm(pool, entity_id, query, *, where_sql, params, **kw):
        seen_where.append(("lexical", where_sql, params))
        return [], True, None

    _saved = (mr._embed_query, mr._vector_arm, mr._lexical_arm, mr._read_pool, mr._priv_pool)
    mr._embed_query, mr._vector_arm, mr._lexical_arm = fake_embed_query, fake_vector_arm, fake_lexical_arm
    mr._read_pool = lambda p: p
    mr._priv_pool = lambda p: p
    try:
        res = asyncio.run(real_recall_memories(None, 7, "invoice", limit=5, project_ids=[4, 5]))
        check("recall_memories hands project_ids to the WHERE both arms read",
              len(seen_where) >= 2 and all("project_id = ANY(" in w and [4, 5] in p for _, w, p in seen_where),
              f"seen={seen_where} res={res}")
        seen_where.clear()
        asyncio.run(real_recall_memories(None, 7, "invoice", limit=5))
        check("recall_memories without project_ids adds no project clause",
              len(seen_where) >= 2 and all("project_id" not in w for _, w, _p in seen_where),
              f"seen={seen_where}")
    finally:
        mr._embed_query, mr._vector_arm, mr._lexical_arm, mr._read_pool, mr._priv_pool = _saved

    # ── 4. federated_recall under a context.
    calls = {"memory": [], "documents": []}
    out = asyncio.run(mr.federated_recall(None, 1, "q", scope="all",
                                          context={"projects": None, "documents": True, "memory": "none"}))
    check("memory 'none': the memory arm is never called and `narrowed` says so",
          calls["memory"] == [] and "memory" not in out
          and any("reads no memory" in n for n in out.get("narrowed", [])), f"calls={calls} out={out}")
    check("memory 'none': documents still searched", len(calls["documents"]) == 1)
    text = mr.format_recall_text(out)
    check("`narrowed` reaches the model: the rendered text says what was not searched",
          "Not searched" in text and "reads no memory" in text, f"got {text!r}")
    out_mem_only = asyncio.run(mr.federated_recall(None, 1, "q", scope="memory",
                                                   context={"projects": None, "documents": True, "memory": "none"}))
    text = mr.format_recall_text(out_mem_only)
    check("scope=memory under memory 'none' says why instead of 'No sources searched'",
          "Not searched" in text and "No sources searched" not in text, f"got {text!r}")
    calls = {"memory": [], "documents": []}
    out = asyncio.run(mr.federated_recall(None, 1, "q", scope="all",
                                          context={"projects": None, "documents": False, "memory": "all"}))
    check("documents False: the documents store is never called and `narrowed` says so",
          calls["documents"] == [] and "documents" not in out
          and any("documents store" in n for n in out.get("narrowed", [])), f"calls={calls} out={out}")
    calls = {"memory": [], "documents": []}
    out = asyncio.run(mr.federated_recall(None, 1, "q", scope="all",
                                          context={"projects": [4, 5], "documents": True, "memory": "projects"}))
    check("memory 'projects': the listed ids reach the memory arm",
          calls["memory"] and calls["memory"][0].get("project_ids") == [4, 5], f"calls={calls}")
    calls = {"memory": [], "documents": []}
    out = asyncio.run(mr.federated_recall(None, 1, "q", scope="memory",
                                          context={"projects": None, "documents": True, "memory": "all"}))
    check("the caller's own scope still narrows on top (scope=memory searches no documents)",
          len(calls["memory"]) == 1 and calls["documents"] == [], f"calls={calls}")

    # ── 5. build_memory_context under a context — every store read is COUNTED.
    reads = {"standing": 0, "teaching": 0}
    taught_reads: list = []

    class _NoRowsPool:
        """The untyped path reads the taught rows straight off the pool when
        there is no conversation context; this gate has none to give."""
        async def fetch(self, *a, **k):
            reads["teaching"] += 1
            taught_reads.append(a)
            return []

    class _Stub:
        pool = _NoRowsPool()

        async def retrieve(self, *a, **k):
            reads["standing"] += 1
            return []

        async def retrieve_teaching(self, *a, **k):
            reads["teaching"] += 1
            return []

        async def _build_teaching_block(self, *a, **k):
            return ""

    fed_calls = []

    async def fake_fed(pool, entity_id, query, *, scope="all", limit=10, context=None,
                       memory_types=None, turn_scope=None, scope_everywhere_types=None,
                       exclude_rules=False, skip_words=None, any_words=False):
        fed_calls.append({"scope": scope, "limit": limit, "context": context})
        return {"query": query, "scope": scope,
                "memory": {"hits": [{"content": "prefers invoice 42 paid by transfer", "matched": ["lexical"],
                                     "memory_type": "preference", "metadata": {}, "created_at": None}]},
                "documents": {"results": []}}
    mr.federated_recall = fake_fed

    block = asyncio.run(me.MemoryEngine.build_memory_context(
        _Stub(), 1, {}, max_tokens=1000, query="invoice",
        context={"projects": [4], "documents": True, "memory": "none"}))
    check("memory 'none': no block, no store touched",
          block == "" and fed_calls == [] and reads == {"standing": 0, "teaching": 0},
          f"got {block!r} {fed_calls} {reads}")
    block = asyncio.run(me.MemoryEngine.build_memory_context(
        _Stub(), 1, {}, max_tokens=1000, query="invoice",
        context={"projects": [4], "documents": True, "memory": "projects"}))
    check("memory 'projects': the pre-fire runs WITH the context and renders its hits",
          bool(fed_calls) and (fed_calls[-1].get("context") or {}).get("projects") == [4]
          and "prefers invoice 42 paid by transfer" in (block or ""),
          f"fed_calls={fed_calls} block={block!r}")
    _proj_sql = mr.project_rows_sql(2)
    check("memory 'projects': the standing list was never read; the taught rows were read only "
          "through the pre-fire's own project test, with the type's projects",
          reads == {"standing": 0, "teaching": 1} and len(taught_reads) == 1
          and _proj_sql in taught_reads[0][0] and taught_reads[0][2] == [4],
          f"reads={reads} taught={taught_reads}")
    reads = {"standing": 0, "teaching": 0}
    fed_calls.clear()
    block = asyncio.run(me.MemoryEngine.build_memory_context(
        _Stub(), 1, {}, max_tokens=1000, query="invoice", context=None))
    check("untyped: the standing list IS read and the pre-fire runs with no context",
          # one read per prompt type (preference, relationship), so neither
          # crowds the other out of its section
          reads["standing"] == len(me.PROMPT_MEMORY_TYPES) and fed_calls
          and fed_calls[-1].get("context") is None,
          f"reads={reads} fed_calls={fed_calls}")

    # ── 6. _b_memory never falls back to the entity_context files (the
    #       fallback is gone everywhere, scoped or not).
    fallback_calls = []

    class _FakeCtxStore:
        def __init__(self, pool, eid): pass

        async def build_memory_prompt(self):
            fallback_calls.append(1)
            return "## Previous conversation context\n(cross-project summary)"

    import mastercode.context_store as cs_mod
    cs_mod.ContextStore = _FakeCtxStore

    class _Mem:
        def __init__(self, ret): self.ret = ret

        async def build_memory_context(self, *a, **k): return self.ret

    class _Runtime:
        pool = None

        def __init__(self, ret): self.memory = _Mem(ret)

    class _Sess:
        entity_id = 1
        entity = {"id": 1}
        resolved_weights = {}
        session_id = "s"

        def __init__(self, tt_): self.turn_type = tt_

    import mastercode.runtime.session_intake as si
    si._to_dim_names = lambda w: []
    out = asyncio.run(ps._b_memory(_Runtime(""), _Sess(_type(context={"memory": "none"})), "hi"))
    check("_b_memory: memory 'none' with an empty block renders NOTHING (no fallback)",
          out is None and fallback_calls == [], f"got {out!r} fallback_calls={fallback_calls}")
    fallback_calls.clear()
    out = asyncio.run(ps._b_memory(_Runtime(""), _Sess(_type(context={"projects": [4]})), "hi"))
    check("_b_memory: 'projects' with no hits renders NOTHING (no fallback)",
          out is None and fallback_calls == [], f"got {out!r} fallback_calls={fallback_calls}")
    fallback_calls.clear()
    out = asyncio.run(ps._b_memory(_Runtime(""), _Sess(tt.closed_type("x")), "hi"))
    check("_b_memory: a CLOSED turn renders NOTHING (no fallback)",
          out is None and fallback_calls == [], f"got {out!r} fallback_calls={fallback_calls}")
    fallback_calls.clear()
    out = asyncio.run(ps._b_memory(_Runtime(""), _Sess(None), "hi"))
    check("_b_memory: untyped with an empty block renders NOTHING — the entity_context "
          "fallback is gone (its files held April test data)",
          out is None and fallback_calls == [], f"got {out!r} fallback_calls={fallback_calls}")
    check("the recent_conversations block is out of the schema",
          "recent_conversations" not in {b.name for b in ps.PROMPT_SCHEMA})

    # ── 7. route_for_type.
    chat_route = {"provider": "kimi", "model": "k3", "base_url": None, "unconfigured": None,
                  "effort": "high"}
    r = asyncio.run(tt.route_for_type(None, 1, _type(model=""), chat_route))
    check("no model on the type → the chat route", r is chat_route)
    r = asyncio.run(tt.route_for_type(None, 1, _type(model="K3"), chat_route))
    check("the chat model by name (case-insensitive) → the chat route", r is chat_route)
    from mastercode import inference_config as ic
    exp_provider, exp_model = ic.normalize_provider("openrouter", "claude-opus-5-5")
    r = asyncio.run(tt.route_for_type(None, 1, _type(model="claude-opus-5-5"), chat_route))
    check("another model → the ONE normalizer's provider and model",
          r.get("provider") == exp_provider and r.get("model") == exp_model and r.get("turn_type_model") is True,
          f"got {r} expected {exp_provider}/{exp_model}")
    check("the person's effort rides through the type's route", r.get("effort") == "high", f"got {r}")
    ic.provider_allowed_for_role = lambda p, role: p in ("anthropic", "openrouter", "local", "kimi")
    r = asyncio.run(tt.route_for_type(None, 1, _type(model="anthropic:claude-opus-5-5"), chat_route))
    check("`provider:model` names the provider and strips the prefix from the model id",
          r.get("provider") == "anthropic" and ":" not in (r.get("model") or "") and "claude" in (r.get("model") or ""),
          f"got {r}")
    import mastercode.model_backends as mb

    async def fake_match_local(pool, entity_id, model):
        return {"endpoint": "http://[fd00::9]:11434"} if model == "qwen3:8b" else None
    mb.match_local_model = fake_match_local
    r = asyncio.run(tt.route_for_type(None, 1, _type(model="qwen3:8b"), chat_route))
    check("a local tag like qwen3:8b is a model id, not a provider prefix, and routes LOCAL",
          r.get("provider") == "local" and r.get("base_url") == "http://[fd00::9]:11434", f"got {r}")

    r = asyncio.run(tt.route_for_type(None, 1, _type(model="local:qwen3:8b"), chat_route))
    check("`local:<model>` looks up the live endpoint too",
          r.get("provider") == "local" and r.get("base_url") == "http://[fd00::9]:11434", f"got {r}")
    r = asyncio.run(tt.route_for_type(None, 1, _type(model="local:nothing-serves-this"), chat_route))
    check("`local:<model>` nothing serves stays local with no endpoint (the gateway says so)",
          r.get("provider") == "local" and r.get("base_url") is None, f"got {r}")

    async def raising_match_local(pool, entity_id, model):
        raise RuntimeError("mesh down")
    mb.match_local_model = raising_match_local
    r = asyncio.run(tt.route_for_type(None, 1, _type(model="qwen3:8b"), chat_route))
    check("a local-match failure does not fail the turn: normalizer route, no endpoint",
          r.get("provider") == "openrouter" and r.get("base_url") is None, f"got {r}")

    # ── 8. session.py + prompt_schema wiring (structural).
    sess_src = open(os.path.join(app_root, "brain", "mastercode", "runtime", "session.py"),
                    encoding="utf-8").read()
    i_load = sess_src.find("sess.turn_type = await resolve_session_type(")
    i_route = sess_src.find("_chat_route = await _rrr(")
    i_override = sess_src.find("_chat_route = await _tt_route_for_type(")
    check("session: the type is resolved BEFORE the chat route",
          0 < i_load < i_route, f"load={i_load} route={i_route}")
    check("session: the type's model overrides the route right after it is resolved",
          0 < i_route < i_override < i_route + 1500, f"route={i_route} override={i_override}")
    check("session: a per-thread MODEL pick wins over the type's model — by pick, not by truthiness",
          "and not _tt_thread_model_pick(thread_override)):" in sess_src
          and 'and not thread_override' not in sess_src)
    check("thread_model_pick: a budget-only override is NOT a model pick",
          tt.thread_model_pick({"context_budget": 5000, "turn_allowance_usd": 1.0}) is False)
    check("thread_model_pick: provider+model IS a pick",
          tt.thread_model_pick({"provider": "kimi", "model": "k3"}) is True)
    check("thread_model_pick: lane+model_id IS a pick",
          tt.thread_model_pick({"lane": "openrouter", "model_id": "x/y"}) is True)
    check("thread_model_pick: a model with no provider is not a pick, nor is None",
          tt.thread_model_pick({"model": "k3"}) is False and tt.thread_model_pick(None) is False)
    # The Host's own skill first, then the disk default (host_skills.load_type_skill).
    _tsk_call = "await _tt_load_skill(self.pool, sess.entity_id, _type_skill_name)"
    check("session: the type's skill is loaded beside the type, outside the pick's try",
          sess_src.find(_tsk_call) > 0
          and sess_src.find(_tsk_call) < sess_src.find("_chat_route = await _rrr("))
    check("session: the type's skill is the Host's own skill before the disk default",
          "from mastercode.runtime.host_skills import load_type_skill as _tt_load_skill" in sess_src)
    # The per-group skill vote and its per-turn block are gone; the type's
    # skill is the one skill
    # that still rides, on the static message.
    check("session: the type's skill is resolved from the type, and no per-group vote rides",
          "_type_skill_name = _tt_type_skill(getattr(sess, \"turn_type\", None))" in sess_src
          and "_pick.get(\"groups\")" not in sess_src and "_loaded_block" not in sess_src)
    i_static = sess_src.find('grok_messages[0]["content"] += "\\n\\n" + _orch_block + _type_skill_tail')
    check("session: the type's skill rides the STATIC system message (the one static append)",
          i_static > 0, f"static={i_static}")
    check("session: the type's skill is logged to prompt_log at per_rebuild scope",
          '"turn_type_skill", _type_skill_block, scope="per_rebuild"' in sess_src)
    check("session: the recall tool passes the type's context",
          re.search(r"unified_search\((?:.|\n)*?context=_tt_context_filter\(getattr\(sess, \"turn_type\", None\)\)", sess_src) is not None)
    check("session: the module-unavailable fallback still closes a closed turn's context",
          "if (_t or {}).get(\"status\") == \"closed\" else None)" in sess_src)
    ps_src = open(os.path.join(app_root, "brain", "mastercode", "runtime", "prompt_schema.py"),
                  encoding="utf-8").read()
    check("prompt_schema: the memory block passes the type's context",
          "context=_ctx_filter" in ps_src and "context_filter as _cf" in ps_src)

    print()
    if FAILS:
        print(f"[turn-types-env] {len(FAILS)} FAILED: {FAILS}")
        return 1
    print("[turn-types-env] all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
