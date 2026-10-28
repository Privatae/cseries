#!/usr/bin/env python3
# gate-scope: whole-tree — every circles-web and workbench-web source file and the built Circles bundle, for /api/search callers
"""Gate: the one index — POST /api/search (memory_retrieval.unified_search).

A single index surface for finding context, files and the rest.
What this ratchets:
  1. IDENTITY WITH THE HOST — the memory + documents half goes through
     federated_recall with the SAME call shape the per-turn pre-fire uses
     (memory_engine.py ~3130), called ONCE (one query embedding), and then the
     pre-fire's own RELEVANCE_FLOOR is applied the way the pre-fire applies it
     (memory: not lexical/graph-matched → floor on `relevance`; documents:
     matched == ["semantic"] → floor on `similarity`), COUNTED as
     `suppressed`, never silent. What the pre-fire does beyond that (per-model
     visibility filter, own-words cap, 6-item cut) is deliberately NOT applied.
  2. EVERY STORE ANSWERS UNDER ITS OWN KEY in the shape its own route returns
     (projects: {results, semantic}; code: {results,
     projects, semantic}) — an app that reads one of those routes reads the
     same keys here.
  3. A STORE THAT CANNOT RUN IS NAMED, NEVER FATAL — a raising store, a hanging
     store (per-store timeout) and a RecallUnavailable from the host half each
     land as {unavailable: reason} + the store in `unavailable`, and the other
     stores still answer.
  4. NO CROSS-STORE RANKING — each section keeps its engine's order.
  5. NO VISIBILITY FILTER — unified_search never calls mastercode.visibility
     (no filter; the pre-fire's per-model filter is the model's, not
     the owner's).
  6. THE CODE STORE EMBEDS ONCE AND RERANKS ONCE across every indexed project
     (per-project searcher.search made N identical embeds
     and N reranks per keystroke pause).
  7. THE CALLS MATCH THE REAL SERVICES — the stubs below are checked against
     the real functions' signatures (read from source, container-free), so a
     renamed parameter in projects.py/brain.py/codesearch cannot leave the
     store silently `unavailable` while this gate stays green.
  8. THE ROUTE is gated on read:search, the scope is in the catalog, the
     scope is NOT in the community allowlist, and an unknown `stores` name is
     refused. Context declares the scope (its box is the everything-search).
     Tokens are minted from the manifest.
  9. THE WORDS ARE NEVER IN A URL — the route is a POST whose words
     are the JSON body's {q, limit, stores}; the GET form is gone (the kernel's /api
     catch-all answers a GET with its 404; the router alone, a 405), and a
     query string on the POST is not read. Driven over HTTP through the real
     router, the real app-token check and the real secret cut, with only the
     store engine stubbed: no token is a 401, a token without read:search a
     403; a hard secret shape never reaches the stores; a lone surrogate half
     is answered; a blank or missing `q`, a bad `limit` and an unknown store
     are refused. Every caller sends a body: the kit's search (Circles' pane,
     and its built bundle), and the Context app.

Container-free: every store engine is stubbed at its import seam.
  APP_ROOT=<repo> python3 scripts/test_unified_search_route.py
"""
from __future__ import annotations

import ast
import asyncio
import os
import re
import sys
import types
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
for p in (str(ROOT), str(ROOT / "brain"), str(ROOT / "server")):
    if p not in sys.path:
        sys.path.insert(0, p)

fails = 0


def check(name, ok, detail=""):
    global fails
    print(("  ok    " if ok else "  FAIL  ") + name + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        fails += 1


from mastercode import memory_retrieval as mr  # noqa: E402
from mastercode.memory_engine import RecallUnavailable  # noqa: E402

FLOOR = mr.RELEVANCE_FLOOR

# ── stubs at the import seams unified_search uses ───────────────────────────
calls: dict[str, list] = {}


def _install_stubs(*, projects_raise=False, projects_hang=False, fed_raise=False,
                   code_projects=("alpha", "beta"), embed_none=False, rerank_none=False,
                   projects_raise_words=False):
    calls.clear()
    calls.update({"fed": [], "projects": [], "embed": [], "rerank": [],
                  "vector": [], "lexical": []})

    async def fed(pool, entity_id, query, *, scope="all", limit=10):
        calls["fed"].append({"entity_id": entity_id, "query": query, "scope": scope, "limit": limit})
        if fed_raise:
            raise RecallUnavailable("no arm could run (gate)")
        out = {"query": query, "scope": scope}
        if scope in ("all", "memory"):
            out["memory"] = {"hits": [
                {"id": 1, "content": f"memory about {query}", "matched": ["lexical"], "relevance": None},
                {"id": 2, "content": "strong semantic", "matched": ["semantic"], "relevance": FLOOR + 0.2},
                {"id": 3, "content": "weak semantic noise", "matched": ["semantic"], "relevance": FLOOR - 0.1},
                {"id": 4, "content": "graph-linked, low cosine", "matched": ["semantic", "graph"], "relevance": 0.05},
            ], "arms": ["semantic", "lexical"], "degraded": False,
                "semantic_unavailable": None, "lexical_unavailable": None}
        if scope in ("all", "documents"):
            out["documents"] = {"results": [
                {"id": 7, "content": f"doc mentioning {query}", "matched": ["lexical"]},
                {"id": 8, "content": "weak semantic doc", "matched": ["semantic"], "similarity": FLOOR - 0.05},
                {"id": 9, "content": "a picture", "matched": ["media"], "similarity": 0.01},
            ], "query": query, "arms": ["semantic", "lexical"]}
        return out
    mr.federated_recall = fed

    proj_mod = types.ModuleType("server.routes.projects")

    async def search_projects_service(pool, entity_id, q, limit=8, *, lane=None, refresh_in_background=True):
        calls["projects"].append((entity_id, q, limit))
        if projects_hang:
            await asyncio.sleep(3600)
        if projects_raise:
            raise RuntimeError("projects table missing (gate)")
        if projects_raise_words:
            # asyncpg's own shape for an argument it cannot encode
            # (asyncpg/protocol/prepared_stmt.pyx: "invalid input for query
            # argument $N: <the value's repr, first 40 characters> (<why>)").
            raise ValueError(f"invalid input for query argument $2: {q[:40]!r} (gate)")
        return {"projects": [{"id": 42, "name": "Trading software", "score": 0.9},
                             {"id": 43, "name": "Trading journal", "score": 0.4}], "semantic": False, "q": q}
    proj_mod.search_projects_service = search_projects_service
    sys.modules["server.routes.projects"] = proj_mod

    # The Host's own database (mastercode/world_recall.py, store `world`): a
    # stub like the others, so this gate tests the fan-out, not the catalog.
    world_mod = types.ModuleType("mastercode.world_recall")

    async def world_search(pool, entity_id, q, limit, *, catalog=None):
        calls.setdefault("world", []).append((entity_id, q, limit))
        return {"results": [{"name": "world.kitchen", "kind": "table", "summary": f"tools for {q}",
                             "open": "data(op='describe', name='kitchen')"}],
                "semantic": False, "objects": 1}
    world_mod.search = world_search
    sys.modules["mastercode.world_recall"] = world_mod
    import mastercode as _mc
    _mc.world_recall = world_mod

    cs_route = types.ModuleType("server.routes.codesearch")
    cs_route._get_db = lambda: "DB"
    sys.modules["server.routes.codesearch"] = cs_route
    cs_pkg = types.ModuleType("codesearch")
    store = types.ModuleType("codesearch.store")
    async def stats(db):
        return [{"project": p, "chunks": 3} for p in code_projects] + [{"project": "empty", "chunks": 0}]
    store.stats = stats

    async def search_vector(db, *, project, query_embedding, space, limit=30):
        calls["vector"].append((project, limit))
        if project == "beta":
            raise RuntimeError("beta index locked (gate)")
        return [{"file_path": f"{project}/v.py", "chunk_index": 0, "content": f"def {project}_vec(): ...", "relevance": 0.8}]

    async def search_lexical(db, *, project, query, limit=30):
        calls["lexical"].append((project, limit))
        return [{"file_path": f"{project}/l.py", "chunk_index": 1, "content": f"# {query} in {project}", "lex_rank": 2.0},
                {"file_path": f"{project}/v.py", "chunk_index": 0, "content": f"def {project}_vec(): ...", "lex_rank": 1.0}]
    store.search_vector, store.search_lexical = search_vector, search_lexical

    searcher = types.ModuleType("codesearch.searcher")
    searcher.RERANK_CANDIDATES = 30

    def rrf_fuse(vector_hits, lexical_hits, k=60):
        scores, rows = {}, {}
        for arm in (vector_hits, lexical_hits):
            for rank, hit in enumerate(arm):
                key = (hit["file_path"], hit["chunk_index"])
                scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
                rows.setdefault(key, hit)
        return [dict(rows[key], rrf_score=sc) for key, sc in sorted(scores.items(), key=lambda kv: -kv[1])]
    searcher.rrf_fuse = rrf_fuse

    bridge = types.ModuleType("codesearch.bridge_embed")

    async def embed(text, *, space=None):
        calls["embed"].append(text)
        return None if embed_none else [0.1, 0.2]

    async def active_space():
        return "SPACE", ""     # the configured model's space; opaque to the stub store

    async def rerank(query, docs):
        calls["rerank"].append((query, list(docs)))
        if rerank_none:
            return None
        # favour the lexical-only "l.py" chunks so rerank visibly reorders
        return [0.9 if "# " in d else 0.1 for d in docs]
    bridge.embed, bridge.rerank, bridge.active_space = embed, rerank, active_space
    cs_pkg.store, cs_pkg.searcher, cs_pkg.bridge_embed = store, searcher, bridge
    for name, mod in (("codesearch", cs_pkg), ("codesearch.store", store),
                      ("codesearch.searcher", searcher), ("codesearch.bridge_embed", bridge)):
        sys.modules[name] = mod


async def _run():
    # 1 + 2 + 4: the happy path.
    _install_stubs()
    r = await mr.unified_search("POOL", 9003, "  trading  ", limit=5)
    check("stores default to every store, in catalog order",
          r["stores"] == list(mr.SEARCH_STORES) and r["query"] == "trading" and r["limit"] == 5, repr(r["stores"]))
    check("memory + documents go through federated_recall ONCE, scope='all', same limit (the pre-fire's call shape)",
          calls["fed"] == [{"entity_id": 9003, "query": "trading", "scope": "all", "limit": 5}], repr(calls["fed"]))
    mem_ids = [h["id"] for h in r["memory"]["hits"]]
    check("memory: the pre-fire's floor — a semantic-only hit under RELEVANCE_FLOOR is out; lexical, strong semantic and graph-linked stay; order kept",
          mem_ids == [1, 2, 4] and r["memory"]["suppressed"] == 1, repr((mem_ids, r["memory"].get("suppressed"))))
    check("memory section keeps federated_recall's own keys (arms, degraded, why)",
          r["memory"]["arms"] == ["semantic", "lexical"] and "semantic_unavailable" in r["memory"], repr(r["memory"].keys()))
    doc_ids = [d["id"] for d in r["documents"]["results"]]
    check("documents: floored ONLY when matched == ['semantic'] (a media-arm hit is never floored on a text cosine)",
          doc_ids == [7, 9] and r["documents"]["suppressed"] == 1, repr((doc_ids, r["documents"].get("suppressed"))))
    check("projects: {results, semantic}, entity-scoped, the engine's order kept (no re-ranking)",
          [p["id"] for p in r["projects"]["results"]] == [42, 43] and r["projects"]["semantic"] is False
          and calls["projects"] == [(9003, "trading", 5)], repr((r["projects"], calls["projects"])))
    check("the journal store is gone with its table (2026-10-17)", "journal" not in r and "journal" not in mr.SEARCH_STORES, repr(r.keys()))
    # 6: the code store.
    check("code: the query is embedded ONCE for every indexed project, and reranked ONCE",
          calls["embed"] == ["trading"] and len(calls["rerank"]) == 1, repr((calls["embed"], len(calls["rerank"]))))
    check("code: both sqlite arms run per INDEXED project (chunks>0), never the empty one",
          sorted(p for p, _ in calls["vector"]) == ["alpha", "beta"] and sorted(p for p, _ in calls["lexical"]) == ["alpha"]
          and r["code"]["projects"] == ["alpha", "beta"], repr((calls["vector"], calls["lexical"])))
    check("code: a project whose index fails is named in `failed`; the others still answer",
          r["code"]["failed"] == ["beta: beta index locked (gate)"] and r["code"]["results"], repr(r["code"]))
    check("code: reranked order wins, each hit stamped with its project, semantic:true when the embed answered",
          r["code"]["results"][0]["file_path"] == "alpha/l.py" and r["code"]["results"][0]["project"] == "alpha"
          and r["code"]["semantic"] is True, repr(r["code"]["results"]))
    check("nothing was unavailable", r["unavailable"] == [], repr(r["unavailable"]))

    _install_stubs(embed_none=True, rerank_none=True)
    r = await mr.unified_search("POOL", 9003, "trading", stores=["code"], limit=5)
    check("code: no embed → lexical-only, semantic:false, RRF order stands when rerank is refused",
          calls["vector"] == [] and r["code"]["semantic"] is False and r["code"]["results"]
          and r["code"]["results"][0]["file_path"] == "alpha/l.py" and "rerank_score" not in r["code"]["results"][0],
          repr(r["code"]))

    # The reranker's candidate pool is drawn round-robin
    # across projects, so with MORE indexed projects than RERANK_CANDIDATES
    # the best match in the last project still reaches the reranker.
    many = tuple(f"proj{i:02d}" for i in range(40))
    _install_stubs(code_projects=many)
    bridge = sys.modules["codesearch.bridge_embed"]

    async def rerank_last_wins(query, docs):
        calls["rerank"].append((query, list(docs)))
        return [0.99 if "proj39" in d else 0.1 for d in docs]
    bridge.rerank = rerank_last_wins
    # every project returns the same two hits at the same ranks → identical rrf scores everywhere
    async def same_hit(db, *, project, query_embedding, space, limit=30):
        return [{"file_path": f"{project}/v.py", "chunk_index": 0, "content": f"def {project}_vec(): ...", "relevance": 0.8}]
    sys.modules["codesearch.store"].search_vector = same_hit
    r = await mr.unified_search("POOL", 9003, "trading", stores=["code"], limit=5)
    pooled_docs = calls["rerank"][0][1]
    pooled_projects = {m for d in pooled_docs for m in re.findall(r"proj\d\d", d)}
    check("code: with 40 indexed projects (> RERANK_CANDIDATES) the reranker sees EVERY project's top hit, one rerank",
          len(calls["rerank"]) == 1 and len(pooled_docs) == 40 and len(pooled_projects) == 40,
          repr((len(pooled_docs), sorted(pooled_projects)[:3], sorted(pooled_projects)[-1:])))
    check("code: the best rerank match in the LAST project surfaces first (it was cut before the reranker in the flat sort)",
          r["code"]["results"][0]["project"] == "proj39", repr([h["project"] for h in r["code"]["results"]]))

    # A subset of stores: only those run; the host half narrows its scope.
    _install_stubs()
    r = await mr.unified_search("POOL", 9003, "x", stores=["documents", "code"], limit=3)
    check("stores narrows: unrequested stores are absent and not called",
          r["stores"] == ["documents", "code"] and "memory" not in r and "projects" not in r
          and calls["projects"] == [], repr(r))
    check("documents-only → federated_recall scope='documents'", calls["fed"][0]["scope"] == "documents", repr(calls["fed"]))
    r = await mr.unified_search("POOL", 9003, "x", stores=["memory"], limit=3)
    check("memory-only → scope='memory'", calls["fed"][-1]["scope"] == "memory", repr(calls["fed"]))
    try:
        await mr.unified_search("POOL", 9003, "x", stores=["projcts"], limit=3)
        check("an unknown store name is REFUSED (UnknownSearchStore), never a silent search of everything", False)
    except mr.UnknownSearchStore as e:
        check("an unknown store name is REFUSED (UnknownSearchStore), never a silent search of everything", "projcts" in str(e))

    # 3: failure shapes.
    _install_stubs(projects_raise=True)
    r = await mr.unified_search("POOL", 9003, "trading", limit=5)
    check("a raising store lands as {unavailable: reason} and is named; the rest still answer",
          r["projects"] == {"unavailable": "projects table missing (gate)"} and r["unavailable"] == ["projects"]
          and r["memory"]["hits"] and r["code"]["results"], repr(r["unavailable"]))
    # The log line for a failing store names the store and the exception's
    # type, never its text: a store's exception can carry the words it was
    # asked for (asyncpg quotes an argument it cannot encode), and the log is
    # kept by the pod, outside the person's search.
    import logging
    _install_stubs(projects_raise_words=True)
    caught: list = []

    class _Keep(logging.Handler):
        def emit(self, record):
            caught.append(record.getMessage())
    keep, mr_log = _Keep(level=logging.DEBUG), logging.getLogger(mr.log.name)
    old_level = mr_log.level
    mr_log.addHandler(keep)
    mr_log.setLevel(logging.DEBUG)
    try:
        r = await mr.unified_search("POOL", 9003, "hunter2hunter2 ledger words", limit=5)
    finally:
        mr_log.removeHandler(keep)
        mr_log.setLevel(old_level)
    check("a store raising with the words in its message: no log line holds them",
          r["unavailable"] == ["projects"] and caught
          and not any("hunter2hunter2" in m or "ledger words" in m for m in caught), caught)
    check("...the log line names the store and the exception's type",
          any("projects" in m and "ValueError" in m for m in caught), caught)
    _install_stubs(projects_hang=True)
    r = await mr.unified_search("POOL", 9003, "trading", limit=5, timeout_s=0.05)
    check("a hanging store hits the per-store timeout, is named, and does not stall the others",
          r["projects"]["unavailable"].startswith("timed out") and r["unavailable"] == ["projects"]
          and r["memory"]["hits"] and r["code"]["results"], repr(r["projects"]))
    _install_stubs(fed_raise=True)
    r = await mr.unified_search("POOL", 9003, "trading", limit=5)
    check("RecallUnavailable from the host half → memory AND documents unavailable with the reason, others fine",
          r["memory"] == {"unavailable": "no arm could run (gate)"} and r["documents"] == r["memory"]
          and r["unavailable"] == ["memory", "documents"] and r["projects"]["results"], repr(r))
    _install_stubs(code_projects=())
    r = await mr.unified_search("POOL", 9003, "trading", limit=5)
    check("nothing indexed → code answers empty with projects=[] and no embed spent (a real empty, not unavailable)",
          r["code"] == {"results": [], "projects": []} and calls["embed"] == [] and "code" not in r["unavailable"], repr(r["code"]))

    # Blank query: empty shapes (each store's documented keys), no engine called.
    _install_stubs()
    r = await mr.unified_search("POOL", 9003, "   ", limit=5)
    check("blank query → every store empty in its own shape, no engine called",
          r["memory"] == {"hits": []} and r["code"] == {"results": [], "projects": []} and r["projects"] == {"results": []}
          and not calls["fed"] and not calls["projects"] and not calls["embed"], repr(r))
    check("limit is clamped to [1, 50]", (await mr.unified_search("POOL", 9003, "q", limit=999))["limit"] == 50)


asyncio.run(_run())

# 7: the stubs match the REAL service signatures (read from source; the real
# modules pull the kernel and are not importable container-free).
def _real_params(path: Path, func: str) -> list[str]:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func:
            a = node.args
            return ([x.arg for x in a.posonlyargs] + [x.arg for x in a.args]
                    + (["*" + a.vararg.arg] if a.vararg else ["*"] if a.kwonlyargs else [])
                    + [x.arg for x in a.kwonlyargs])
    return []


def _real_is_async(path: Path, func: str) -> bool:
    return any(isinstance(n, ast.AsyncFunctionDef) and n.name == func for n in ast.walk(ast.parse(path.read_text())))


def _stub_params(mod: str, func: str) -> list[str]:
    import inspect
    fn = getattr(sys.modules[mod], func)
    out = []
    for p in inspect.signature(fn).parameters.values():
        if p.kind is p.VAR_POSITIONAL:
            out.append("*" + p.name)
        elif p.kind is p.KEYWORD_ONLY and "*" not in out and not any(x.startswith("*") for x in out):
            out.append("*")
            out.append(p.name)
        else:
            out.append(p.name)
    return out


for path, mod, func in (
    (ROOT / "server/routes/projects.py", "server.routes.projects", "search_projects_service"),
    (ROOT / "brain/codesearch/store.py", "codesearch.store", "search_vector"),
    (ROOT / "brain/codesearch/store.py", "codesearch.store", "search_lexical"),
    (ROOT / "brain/codesearch/store.py", "codesearch.store", "stats"),
    (ROOT / "brain/codesearch/searcher.py", "codesearch.searcher", "rrf_fuse"),
    (ROOT / "brain/codesearch/bridge_embed.py", "codesearch.bridge_embed", "embed"),
    (ROOT / "brain/codesearch/bridge_embed.py", "codesearch.bridge_embed", "rerank"),
):
    real, stub = _real_params(path, func), _stub_params(mod, func)
    check(f"stub {func} has the REAL signature ({path.name}) — a renamed parameter cannot leave the store silently unavailable",
          real and real == stub, f"real={real} stub={stub}")
    import inspect as _inspect
    check(f"stub {func} is async exactly when the real one is ({path.name}) — an awaited sync stub, or a sync "
          f"stub for an awaited call, would pass here and fail in the pod",
          _real_is_async(path, func) == _inspect.iscoroutinefunction(getattr(sys.modules[mod], func)),
          f"real async={_real_is_async(path, func)}")
check("the real codesearch searcher still exports RERANK_CANDIDATES (the candidate pool size this reuses)",
      re.search(r"^RERANK_CANDIDATES\s*=", (ROOT / "brain/codesearch/searcher.py").read_text(), re.M) is not None)
check("the real codesearch route still exposes _get_db (the store's handle: the pod's Postgres pool)",
      re.search(r"^def _get_db\(", (ROOT / "server/routes/codesearch.py").read_text(), re.M) is not None)

# 5: no visibility filter in the one index; the pre-fire identity it rests on.
src = (ROOT / "brain/mastercode/memory_retrieval.py").read_text()
body = src.split("async def unified_search(")[1].split("\ndef _thread_label(")[0]
check("unified_search never imports or calls mastercode.visibility (no filter)",
      "visibility" not in body, "visibility referenced in unified_search")
eng = (ROOT / "brain/mastercode/memory_engine.py").read_text()
# One level of nested parentheses is tolerated in the call's arguments, so a
# future `query.strip()` or `min(limit, 6)` inside it does not false-red.
_ARG = r'(?:[^()]|\([^()]*\))*'
# The pre-fire reads memory only
# (preferences and relationship notes); the documents store is reached with
# `recall`, which runs this same unified search. The memory half is still
# the same engine with the same floor.
check("the pre-fire calls federated_recall(scope='memory', limit=6) — the memory half this gate rests on",
      re.search(r'federated_recall\(' + _ARG + r'scope="memory"' + _ARG + r'limit=6', eng, re.S) is not None)
check("the pre-fire still floors memory on relevance at RELEVANCE_FLOOR (what _apply_relevance_floor mirrors)",
      re.search(r'rel = h\.get\("relevance"\)\s*\n\s*if rel is None or rel < RELEVANCE_FLOOR', eng) is not None)

# 8: the route, the scope, the manifests.
route = (ROOT / "server/routes/search.py").read_text()
check("POST /api/search is gated on read:search, and there is no GET form",
      'APIRouter(prefix="/api/search"' in route
      and '@router.post("", dependencies=[Depends(requires_permission("read:search"))])' in route
      and "@router.get(" not in route and "Query(" not in route)
check("an unknown store name is a 400 at the route", "except UnknownSearchStore" in route and "HTTPException(400" in route)
_down = route.split("Brain not initialized")[1].split("unavailable")[0]
check("the brain-down error body still carries stores + limit (the TS contract's non-optional fields)",
      '"stores"' in _down and '"limit"' in _down, repr(_down))
check("the route is registered", "_search_route.router" in (ROOT / "server/main.py").read_text())
from server.applications.scopes import PERMISSIONS  # noqa: E402
check("read:search is in the scope catalog", "read:search" in PERMISSIONS)
from server.applications.build_contract import COMMUNITY_ALLOWED_SCOPES  # noqa: E402
check("read:search is NOT a community-app scope (whole-corpus read, like read:brain_memory_all)",
      "read:search" not in COMMUNITY_ALLOWED_SCOPES)
# Context holds the everything-search. Circles' Search
# pane, the kit's app-web-kit/search.tsx, left with the rail:
# Circles finds people, Hosts and conversations by name from what its cards
# already read, nothing sent, and holds no read:search. The Workbench dropped
# the box with its rail (the book of pages): its finder is the Index
# (POST /api/projects/index), so it neither mounts the box nor holds the scope.
_perms = {app: (ROOT / f"system-apps/{app}/app.yaml").read_text().split("\npermissions:")[1]
          for app in ("circles", "workbench", "context-manager")}
check("context manifest declares read:search (its box is the everything-search; tokens are minted from the manifest)",
      "\n  - read:search" in _perms["context-manager"])
_ci_src = "".join(p.read_text() for p in sorted((ROOT / "circles-web/src").rglob("*.ts*")))
check("circles mounts no Search pane, calls no /api/search, and holds no read:search",
      "@kit/search" not in _ci_src and "/api/search" not in _ci_src
      and "\n  - read:search" not in _perms["circles"])
check("the kit's Search pane is gone with its only user", not (ROOT / "app-web-kit/search.tsx").exists())
check("workbench manifest no longer declares read:search (nothing in it calls /api/search)",
      "read:search" not in _perms["workbench"])
_ci_assets = "".join(p.read_text(errors="replace") for p in sorted((ROOT / "system-apps/circles/assets").glob("*.js")))
check("the built Circles bundle calls no /api/search", "/api/search" not in _ci_assets)
_wb_src = "".join(p.read_text() for p in sorted((ROOT / "workbench-web/src").rglob("*.ts*")))
check("workbench-web mounts no SearchPane and calls no /api/search",
      "@kit/search" not in _wb_src and "/api/search" not in _wb_src)
ctx_js = (ROOT / "system-apps/context-manager/app.js").read_text()
_ctx_call = ctx_js.split("fetchJSON('/api/search', {")[1].split("});")[0] if "fetchJSON('/api/search', {" in ctx_js else ""
check("the Context app's box POSTs to /api/search with {q, limit} in the body, and never puts it in a URL",
      "method: 'POST'" in _ctx_call and "JSON.stringify({ q: sent, limit: SEARCH_LIMIT })" in _ctx_call
      and "/api/search?" not in ctx_js, _ctx_call or "no fetchJSON('/api/search', ...) call")
check("the Context app's box no longer searches documents alone (POST /api/brain/context/search)",
      "${BAPI}/search" not in ctx_js)
check("the Context app's box searches every store (it names none, so the route searches all five)",
      "stores" not in _ctx_call, _ctx_call)


# 9: over HTTP — the real router, the real token check, the real secret cut.
def _http_checks():
    import httpx
    from fastapi import FastAPI, Request
    from server import brain_service as bs
    from server.applications.tokens import AppToken
    from server.routes import search as search_route

    asked: list = []

    async def recording_search(pool, entity_id, query, *, stores=None, limit=10, timeout_s=None):
        asked.append({"pool": pool, "entity_id": entity_id, "query": query, "stores": stores, "limit": limit})
        return {"query": query, "stores": list(stores or mr.SEARCH_STORES), "limit": limit, "unavailable": []}

    def mount(perms):
        app = FastAPI()

        @app.middleware("http")
        async def window_token(request: Request, call_next):
            # What the kernel's token middleware leaves on the request for a
            # window: its app's declared scopes and nothing more (None = no token).
            if perms is not None:
                request.state.app_token = AppToken(app_id="context-manager", permissions=list(perms),
                                                   window_id="gate", iat=0)
            return await call_next(request)

        app.include_router(search_route.router)
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gate")

    key = "sk-ant-api03-" + "Wv5" * 20
    saved = (mr.unified_search, bs._pool, bs._entity_id)

    async def go():
        mr.unified_search = recording_search
        bs._pool, bs._entity_id = "POOL", 7
        try:
            async with mount(["read:search"]) as http:
                r = await http.post("/api/search", json={"q": "trading journal", "limit": 7, "stores": ["memory", "code"]})
                check("POST /api/search answers from the body's {q, limit, stores}",
                      r.status_code == 200 and asked[-1] == {"pool": "POOL", "entity_id": 7, "query": "trading journal",
                                                             "stores": ["memory", "code"], "limit": 7},
                      (r.status_code, r.text[:200], asked[-1:]))
                r = await http.post("/api/search", json={"q": "trading"})
                check("...with no stores and no limit: every store, limit 10",
                      r.status_code == 200 and asked[-1]["stores"] is None and asked[-1]["limit"] == 10,
                      (r.status_code, asked[-1:]))
                n = len(asked)
                r = await http.get("/api/search", params={"q": "password = hunter2hunter2"})
                check("the search router has no GET form: on its own a GET /api/search?q= is a 405 and searches nothing",
                      r.status_code == 405 and len(asked) == n, (r.status_code, r.text[:200]))
                r = await http.post("/api/search?q=from-the-url&limit=3", json={"q": "from the body"})
                check("a query string on the POST is not read: the body's words are searched, at the body's limit",
                      r.status_code == 200 and asked[-1]["query"] == "from the body" and asked[-1]["limit"] == 10,
                      (r.status_code, asked[-1:]))
                r = await http.post("/api/search", json={"q": f"invoice {key} for the shop"})
                check("a hard secret shape in the body is cut before any store is asked; the words around it stay",
                      r.status_code == 200 and key not in asked[-1]["query"] and "Wv5Wv5" not in asked[-1]["query"]
                      and "invoice" in asked[-1]["query"] and "shop" in asked[-1]["query"]
                      and "REDACTED" not in asked[-1]["query"], asked[-1:])
                r = await http.post("/api/search", content=b'{"q": "half \\ud800 a pair"}',
                                    headers={"Content-Type": "application/json"})
                check("a lone surrogate half in the body is answered, read as U+FFFD",
                      r.status_code == 200 and asked[-1]["query"] == "half \ufffd a pair", (r.status_code, r.text[:200], asked[-1:]))
                n = len(asked)
                for body, why in (({"q": ""}, "a blank q"), ({}, "no q"), ({"q": "x", "limit": 0}, "limit 0"),
                                  ({"q": "x", "limit": 51}, "limit 51"), ({"q": "x" * 2001}, "a q over 2000 characters")):
                    r = await http.post("/api/search", json=body)
                    check(f"{why} is refused (422) and searches nothing", r.status_code == 422 and len(asked) == n,
                          (r.status_code, r.text[:160]))
                r = await http.post("/api/search", json={"q": "x", "stores": ["projcts"]})
                check("an unknown store is a 400 naming it, and searches nothing",
                      r.status_code == 400 and "projcts" in r.text and len(asked) == n, (r.status_code, r.text[:160]))
                bs._pool = None
                r = await http.post("/api/search", json={"q": "x", "stores": ["projcts"]})
                check("...the same 400 while the brain is down", r.status_code == 400, (r.status_code, r.text[:160]))
                r = await http.post("/api/search", json={"q": "x", "limit": 4, "stores": ["code"]})
                d = r.json() if r.status_code == 200 else {}
                check("brain down: a 200 that says so, with stores and limit (the TS contract's non-optional fields)",
                      r.status_code == 200 and d.get("error") and d.get("stores") == ["code"] and d.get("limit") == 4
                      and d.get("unavailable") == [] and len(asked) == n, (r.status_code, r.text[:200]))
                bs._pool = "POOL"
            async with mount(None) as http:
                r = await http.post("/api/search", json={"q": "x"})
                check("no app token: 401, and nothing searched", r.status_code == 401 and len(asked) == n, r.status_code)
            async with mount(["read:memory", "read:context"]) as http:
                r = await http.post("/api/search", json={"q": "x"})
                check("a token without read:search: 403, and nothing searched", r.status_code == 403 and len(asked) == n,
                      r.status_code)
            ctx = (ROOT / "system-apps/context-manager/app.yaml").read_text().split("\npermissions:")[1]
            ctx_perms = [ln.strip()[2:].strip() for ln in ctx.splitlines() if ln.strip().startswith("- ")]
            async with mount(ctx_perms) as http:
                r = await http.post("/api/search", json={"q": "budget", "limit": 50})
                check("a token minted from the Context manifest's own scopes is let in",
                      r.status_code == 200 and asked[-1]["query"] == "budget" and asked[-1]["limit"] == 50,
                      (r.status_code, r.text[:160], ctx_perms))
        finally:
            mr.unified_search, bs._pool, bs._entity_id = saved
    asyncio.run(go())


# What the kernel's app answers a GET with, through its REAL route table. The
# router above, mounted on its own, answers a 405; in server.main the /api
# catch-all (`@app.api_route("/api/{rest:path}")`, registered after every
# router and taking GET) is a full match for the same path, so it answers
# first, with its 404. Either refuses and searches nothing; this checks the one
# the deployed app gives, and that the POST still reaches the search route.
# Run in a child process: server.main must not import the stubs this gate
# installs, and its import must not leave state behind in this one. The app's
# middleware stack is left out on purpose (it wants the pod's dirs and token
# store); the routes, their order and their own dependencies are the app's.
_REAL_APP_GET = r'''
import asyncio, json, os, sys, tempfile
root = sys.argv[1]
sys.path[:0] = [root, os.path.join(root, "brain")]
os.environ.setdefault("TM_JOBS_ROOT", tempfile.mkdtemp(prefix="usr-real-app-"))
import starlette.staticfiles as sf
orig = sf.StaticFiles.__init__
def _init(self, *a, **k):
    k["check_dir"] = False
    return orig(self, *a, **k)
sf.StaticFiles.__init__ = _init
import fastapi, httpx
from server.main import app
bare = fastapi.FastAPI()
bare.router.routes = app.router.routes
async def go():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bare), base_url="http://pod") as c:
        g = await c.get("/api/search", params={"q": "password = hunter2hunter2"})
        p = await c.post("/api/search", json={"q": "x"})
    print(json.dumps({"get": [g.status_code, g.text[:200]], "post": [p.status_code, p.text[:200]]}))
asyncio.run(go())
'''


def _real_app_get():
    import json
    import subprocess
    r = subprocess.run([sys.executable, "-c", _REAL_APP_GET, str(ROOT)],
                       capture_output=True, text=True, timeout=180, cwd=str(ROOT))
    line = (r.stdout.strip().splitlines() or [""])[-1]
    try:
        got = json.loads(line)
    except ValueError:
        check("the kernel's real route table answers a GET /api/search?q=", False,
              (r.returncode, (r.stderr or r.stdout)[-400:]))
        return
    check("the kernel's real route table refuses GET /api/search?q=: the /api catch-all answers it (404), "
          "so it never reaches the search route",
          got["get"][0] == 404 and "no such API route: /api/search" in got["get"][1], got)
    check("...while a POST still reaches the search route's own token check (no token: 401)",
          got["post"][0] == 401 and "application token required" in got["post"][1], got)


_http_checks()
_real_app_get()

print("\nPASS unified_search_route" if not fails else f"\nFAIL unified_search_route: {fails} check(s)")
sys.exit(1 if fails else 0)
