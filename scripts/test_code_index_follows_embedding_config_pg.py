#!/usr/bin/env python3
"""
Gate: the Host's code index and research store embed with the Host's
CONFIGURED embedding model, at that model's size, into the column of that
size (migrations/v231) — as its memory does.

The standing rule: no embedding size is hardcoded; the embedding model is
100% host / config driven, and every pin and every fallback default is a bug. Memory
followed it (memory_engine.EMBEDDING_COLUMN_BY_DIM, a column per size). The
code index did not: brain/codesearch embedded with its own model
(CODESEARCH_EMBED_MODEL, default qwen3-embedding:4b) into one halfvec(2560)
column (CODESEARCH_EMBED_DIM, default 2560), so a Host whose embedder is
nomic-embed-text (768) on its own machine could not index code by meaning.

A real Postgres carrying the kernel's schema (init-db.sql + every migration),
the real indexer, store, searcher, search_my_code tool and research store, the
real config read (entity_registry.behavioral_params, the one memory reads)
and the real lane resolution. Only the HTTP call to the Host's own embedding
endpoint is faked (memory_engine._embed_via_byo): it answers for the models it
serves, at each model's real size, and for no other — as a Host's own
machine does.

  C   a column per size memory stores, in code_chunks and research_chunks,
      each of the type memory casts that size to; an HNSW index on every
      code_chunks column pgvector can index (to 2000 for vector, 4000 for
      halfvec)
  N   no model set up: the index writes every file for keyword search, asks
      no embedder, and says embeddings are not set up yet; search returns the
      keyword hit and search_my_code says "not set up yet", never that a lane
      did not answer
  M   nomic-embed-text (768) configured: the next pass embeds every file with
      THAT model, into embedding_768 only; search by meaning finds it
  S   a switch to mxbai-embed-large (1024): the 768 vectors are untouched,
      every file reads as needing a vector in the new column, and the next
      pass fills embedding_1024 with the new model — a refresh, not a change
  B   switching back to the 768 model embeds nothing: its vectors are there
  E   an edited file keeps no vector of its old text in another model's
      column; an untouched file keeps both
  W   a configured size the endpoint does not return: no vector is stored
      in the wrong column, the pass does not fail, the file is still found
      by its words
  R   the research store saves a page's vectors in the configured model's
      column and finds it by meaning
  R2  after a switch, a find fills one wave of the passages saved before it
      with the new model (fill only: the old model's vectors stay, a vector
      already there is never embedded again), and the research tool says how
      many passages the search by meaning could not see, with which model
  R3  with no model set up, a find says embeddings are not set up yet — never
      that no embedder answered — and asks no embedder

Each step runs on its own: on a tree without the fix a missing column or
call fails that step's checks rather than stopping the gate.

Fresh clone of the kernel schema on TEST_PG_DSN / PEER_TEST_DSN / TEST_DSN,
else a docker throwaway. Prints RESULT: ALL PASS.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
for p in (str(ROOT / "brain"), str(ROOT), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)
import asyncpg  # noqa: E402

from kernel_schema import kernel_schema_db, throwaway_server  # noqa: E402

SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    print("SKIP-LOUD: no test server (TEST_PG_DSN/PEER_TEST_DSN/TEST_DSN) and docker unavailable — "
          "this gate needs a real database for code_chunks and research_chunks.")
    sys.exit(2)

FAILED = 0
E = 1                       # brain_service._entity_id in a Host pod (server/brain_service.py)
PROJECT = "cidx-notes"
ENDPOINT = "http://embedder.host.test:11434"
# What the Host's own machine serves, at each model's real size. Nothing else:
# a request for any other model gets no vector, as from a real endpoint.
SERVED = {"nomic-embed-text": 768, "mxbai-embed-large": 1024}
BILLING = ("def compute_invoice_total(lines):\n"
           "    '''Sum the invoice lines and add the tax.'''\n"
           "    return sum(line.amount for line in lines) * 1.2\n")
WEATHER = "The forecast says rain tomorrow afternoon over the mountains.\n"


def check(label, cond, detail=""):
    global FAILED
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + ("" if cond else f"  [{str(detail)[:500]}]"))
    if not cond:
        FAILED += 1


def fake_vec(text: str, dim: int) -> list[float]:
    """Bag of words: texts sharing words point the same way."""
    v = [0.0] * dim
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        v[int(hashlib.sha256(w.encode()).hexdigest(), 16) % dim] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


async def attempt(label, fn):
    """Run one step (`fn` makes its coroutine); an exception is that step's
    failure, never the gate's crash."""
    try:
        return await fn()
    except Exception as e:  # noqa: BLE001
        check(f"{label} ran", False, f"{type(e).__name__}: {e}")
        return None


async def run(dsn: str) -> None:
    from codesearch import indexer, searcher, store
    from mastercode import memory_engine
    from mastercode.runtime import research_store as rs
    from mastercode.runtime import tools
    from server import brain_service

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="cidx-"))
    root = tmp / "projects" / PROJECT
    root.mkdir(parents=True)
    (root / "billing.py").write_text(BILLING)
    (root / "weather.md").write_text(WEATHER)
    asked: list[tuple[str, str]] = []

    async def _endpoint(text, model, cfg, *, entity_id=None, pool=None):
        asked.append((model, cfg.get("base_url")))
        dim = SERVED.get(model)
        return fake_vec(text, dim) if dim else None

    saved = (brain_service._pool, brain_service._entity_id, memory_engine._embed_via_byo,
             indexer.PROJECTS_ROOT, rs.RESEARCH_ROOT)
    brain_service._pool, brain_service._entity_id = pool, E
    memory_engine._embed_via_byo = _endpoint
    indexer.PROJECTS_ROOT = str(tmp / "projects")
    rs.RESEARCH_ROOT = str(tmp / "research")
    columns = getattr(store, "COLUMN_BY_DIM", {})
    space_of = getattr(store, "Space", None)

    def space(model, dim):
        return space_of(model=model, dim=dim) if space_of else None

    def col(dim):     # the column the fix names (on a tree without it: what it would be)
        return columns.get(dim) or ("embedding" if dim == 2560 else f"embedding_{dim}")

    async def configure(**emb):
        """The Host's embedding setup, as apply_embedding_switch writes it
        (behavioral_params is nullable JSONB, init-db.sql)."""
        bp = await pool.fetchval("SELECT behavioral_params FROM entity_registry WHERE id = $1", E)
        bp = json.loads(bp) if isinstance(bp, str) else dict(bp or {})
        bp = {k: v for k, v in bp.items() if not k.startswith("embedding_")}
        bp.update(emb)
        await pool.execute("UPDATE entity_registry SET behavioral_params = $1::jsonb WHERE id = $2",
                           json.dumps(bp), E)

    def nomic(dim=768):
        return dict(embedding_model="nomic-embed-text", embedding_dim=dim, embedding_base_url=ENDPOINT)

    async def vectors(column):
        """{file_path: [vector text per chunk]} in one column."""
        out: dict = {}
        for r in await pool.fetch(f"SELECT file_path, {column}::text AS v FROM code_chunks "
                                  "WHERE project = $1 ORDER BY file_path, chunk_index", PROJECT):
            out.setdefault(r["file_path"], []).append(r["v"])
        return out

    async def any_vector(cols) -> int:
        return await pool.fetchval(
            "SELECT count(*) FROM code_chunks WHERE project = $1 AND ("
            + " OR ".join(f"{c} IS NOT NULL" for c in cols) + ")", PROJECT)

    async def search(query):
        meta: dict = {}
        hits = await searcher.search(pool, project=PROJECT, query=query, limit=5, meta=meta)
        return hits, meta

    def whole(v: dict, files=("billing.py", "weather.md")) -> bool:
        return set(v or {}) == set(files) and all(all(x) for x in v.values())

    try:
        row = await pool.fetchrow("SELECT id FROM entity_registry WHERE id = $1", E)
        check("precondition: the kernel schema carries the Host's entity row", row is not None, row)

        print("C. a column per size memory stores")
        check("C the code index's sizes are memory's sizes (memory_engine.EMBEDDING_COLUMN_BY_DIM)",
              sorted(columns) == sorted(memory_engine.EMBEDDING_COLUMN_BY_DIM), sorted(columns))
        for table in ("code_chunks", "research_chunks"):
            have = {r["attname"]: r["t"] for r in await pool.fetch(
                "SELECT attname, format_type(atttypid, atttypmod) AS t FROM pg_attribute "
                "WHERE attrelid = $1::regclass AND attnum > 0 AND NOT attisdropped", table)}
            for dim in sorted(memory_engine.EMBEDDING_COLUMN_BY_DIM):
                want = memory_engine.embedding_cast_type(dim)
                check(f"C {table}.{col(dim)} is {want}", have.get(col(dim)) == want, have.get(col(dim)))
        idx = "\n".join(r["indexdef"] for r in await pool.fetch(
            "SELECT indexdef FROM pg_indexes WHERE tablename = 'code_chunks'"))
        for dim in sorted(memory_engine.EMBEDDING_COLUMN_BY_DIM):
            has = re.search(rf"USING hnsw \({col(dim)} ", idx) is not None
            if dim <= 4000:
                check(f"C code_chunks.{col(dim)} has an HNSW index", has, idx)
            else:
                check(f"C code_chunks.{col(dim)} has none (over pgvector's HNSW limit)", not has, idx)

        print("N. no embedding model set up")
        await configure()
        r = await attempt("N index", lambda: indexer.index_project(pool, project=PROJECT)) or {}
        n_rows = await pool.fetchval("SELECT count(*) FROM code_chunks WHERE project = $1", PROJECT)
        n_vec = await attempt("N count vectors", lambda: any_vector([col(d) for d in memory_engine.EMBEDDING_COLUMN_BY_DIM]))
        check("N every file is written for keyword search, with no vector in any column",
              n_rows >= 2 and n_vec == 0, (n_rows, n_vec))
        check("N no embedder is asked", asked == [], asked)
        check("N the pass says embeddings are not set up yet (a setup step, not a lane down)",
              "not set up yet" in str(r.get("embedder")), r)
        hits, meta = await search("compute_invoice_total")
        check("N search finds the identifier by its words",
              bool(hits) and hits[0]["file_path"] == "billing.py", hits)
        check("N search says why it was keyword-only: not set up yet",
              meta.get("semantic") is False and "not set up yet" in str(meta.get("embeddings")), meta)
        t = await tools._exec_search_my_code({"query": "compute_invoice_total", "project": PROJECT})
        degraded = str(((t or {}).get("result") or {}).get("degraded"))
        check("N search_my_code says embeddings are not set up yet, never that a lane did not answer",
              "not set up yet" in degraded and "did not answer" not in degraded, t)
        cur = await attempt("N vectors_current", lambda: indexer.vectors_current(pool, project=PROJECT))
        check("N with no model there is nothing to embed, so an unmoved repo is not walked again for it",
              cur is True, cur)

        print("M. nomic-embed-text (768) configured")
        await configure(**nomic())
        asked.clear()
        r = await attempt("M index", lambda: indexer.index_project(pool, project=PROJECT)) or {}
        v768 = await attempt("M read the 768 column", lambda: vectors(col(768))) or {}
        check("M every chunk has its 768 vector", whole(v768), v768.keys())
        n_other = await attempt("M count other columns", lambda: any_vector(
            [col(d) for d in memory_engine.EMBEDDING_COLUMN_BY_DIM if d != 768]))
        check("M no other column holds a vector", n_other == 0, n_other)
        check("M the embedder was asked for the CONFIGURED model at the configured endpoint, only",
              bool(asked) and set(asked) == {("nomic-embed-text", ENDPOINT)}, sorted(set(asked)))
        check("M a refresh: nothing failed, no file counted as changed, the model named",
              r.get("chunks_failed") == 0 and r.get("files_changed") == 0 and not r.get("embedder")
              and r.get("embedding_model") == "nomic-embed-text", r)
        vh = await attempt("M vector search", lambda: store.search_vector(
            pool, project=PROJECT, query_embedding=fake_vec("invoice tax total", 768),
            space=space("nomic-embed-text", 768), limit=5)) or []
        check("M search by meaning in the 768 column finds the file",
              bool(vh) and vh[0]["file_path"] == "billing.py", vh)
        hits, meta = await search("invoice tax total")
        check("M the searcher's vector arm ran with the configured model",
              meta.get("semantic") is True and bool(hits) and hits[0]["file_path"] == "billing.py",
              (meta, hits))
        cur = await attempt("M vectors_current", lambda: indexer.vectors_current(pool, project=PROJECT))
        check("M the project is current by meaning", cur is True, cur)

        print("S. switch to mxbai-embed-large (1024)")
        before = dict(v768)
        await configure(embedding_model="mxbai-embed-large", embedding_dim=1024, embedding_base_url=ENDPOINT)
        after = await attempt("S read the 768 column", lambda: vectors(col(768)))
        check("S the switch itself leaves every 768 vector as it was", bool(before) and after == before)
        st = await attempt("S file state", lambda: store.get_file_state(
            pool, project=PROJECT, space=space("mxbai-embed-large", 1024))) or {}
        check("S every file reads as needing a vector in the new column",
              bool(st) and all(not emb for _h, emb in st.values()), st)
        cur = await attempt("S vectors_current", lambda: indexer.vectors_current(pool, project=PROJECT))
        check("S the project is not current by meaning until the next pass", cur is False, cur)
        t = await tools._exec_search_my_code({"query": "invoice tax total", "project": PROJECT})
        part = str(((t or {}).get("result") or {}).get("partly_by_meaning"))
        n_chunks = await pool.fetchval("SELECT count(*) FROM code_chunks WHERE project = $1", PROJECT)
        check("S before that pass, search_my_code says the search by meaning saw none of the chunks "
              "(all of them lack a mxbai-embed-large vector), never that it searched them all",
              f"{n_chunks} of the {n_chunks} chunks" in part and "mxbai-embed-large" in part, t)
        asked.clear()
        r = await attempt("S index", lambda: indexer.index_project(pool, project=PROJECT)) or {}
        v1024 = await attempt("S read the 1024 column", lambda: vectors(col(1024))) or {}
        check("S the next pass fills the 1024 column for every chunk", whole(v1024), v1024.keys())
        after = await attempt("S read the 768 column", lambda: vectors(col(768)))
        check("S ...and the 768 vectors are still there, unchanged", bool(before) and after == before)
        check("S the embedder was asked for the new model only",
              bool(asked) and {m for m, _u in asked} == {"mxbai-embed-large"}, sorted(set(asked)))
        check("S a refresh, not a change", r.get("files_changed") == 0 and r.get("chunks_failed") == 0, r)
        vh = await attempt("S vector search", lambda: store.search_vector(
            pool, project=PROJECT, query_embedding=fake_vec("invoice tax total", 1024),
            space=space("mxbai-embed-large", 1024), limit=5)) or []
        check("S search by meaning in the 1024 column finds the file",
              bool(vh) and vh[0]["file_path"] == "billing.py", vh)
        cur = await attempt("S vectors_current", lambda: indexer.vectors_current(pool, project=PROJECT))
        check("S the project is current again", cur is True, cur)
        t = await tools._exec_search_my_code({"query": "invoice tax total", "project": PROJECT})
        res = (t or {}).get("result") or {}
        check("S after it, search_my_code says nothing is missed by meaning",
              "partly_by_meaning" not in res and "degraded" not in res and res.get("count"), t)

        print("B. switch back to the 768 model")
        await configure(**nomic())
        asked.clear()
        r = await attempt("B index", lambda: indexer.index_project(pool, project=PROJECT)) or {}
        check("B nothing is embedded again: its vectors were kept",
              asked == [] and r.get("chunks_written") == 0 and r.get("chunks_failed") == 0, (asked, r))

        print("E. an edited file")
        (root / "billing.py").write_text(BILLING + "\n\ndef refund(order):\n    return -order.total\n")
        r = await attempt("E index", lambda: indexer.index_project(pool, project=PROJECT)) or {}
        v768 = await attempt("E read the 768 column", lambda: vectors(col(768))) or {}
        v1024 = await attempt("E read the 1024 column", lambda: vectors(col(1024))) or {}
        check("E the edited file has its new 768 vectors",
              r.get("files_changed") == 1 and all(v768.get("billing.py") or [None]), (r, v768.get("billing.py")))
        check("E ...and keeps no 1024 vector of its old text",
              "billing.py" in v1024 and not any(v1024["billing.py"]), v1024.get("billing.py"))
        check("E the untouched file keeps both models' vectors",
              all(v768.get("weather.md") or [None]) and all(v1024.get("weather.md") or [None]),
              (v768.get("weather.md"), v1024.get("weather.md")))

        print("W. a configured size the endpoint does not return")
        await configure(**nomic(dim=1024))      # nomic answers 768, not 1024
        (root / "weather.md").write_text(WEATHER + "Snow later in the week.\n")
        r = await attempt("W index", lambda: indexer.index_project(pool, project=PROJECT))
        check("W the pass does not fail", r is not None and not r.get("error"), r)
        wrow = await attempt("W read", lambda: pool.fetch(
            f"SELECT content, {col(1024)} IS NOT NULL AS has FROM code_chunks "
            "WHERE project = $1 AND file_path = 'weather.md'", PROJECT)) or []
        check("W the file is written with its new text and no vector in the wrong-size column",
              bool(wrow) and not any(x["has"] for x in wrow) and any("Snow" in x["content"] for x in wrow),
              [dict(x) for x in wrow])
        hits, _meta = await search("snow")
        check("W ...and is found by its words", any(h["file_path"] == "weather.md" for h in hits), hits)

        print("R. the research store follows the same setup")
        await configure(**nomic())
        asked.clear()
        page = ("# Battery pilot\n\nThe zebrafluxtoken battery stored energy for eighteen months.\n\n"
                "The inverter failed twice in winter.\n")
        res = await attempt("R save", lambda: rs.save_page(
            pool, E, project_id=None, thread_key="cidx-thread", url="https://pilot.example/report",
            title="Battery pilot", text=page, whole=True)) or {}
        n768 = await attempt("R count", lambda: pool.fetchval(
            "SELECT count(*) FROM research_chunks c JOIN research_pages p ON p.id = c.page_id "
            f"WHERE p.thread_key = 'cidx-thread' AND c.{col(768)} IS NOT NULL"))
        check("R every passage is embedded with the configured model, in its column",
              bool(res.get("chunks")) and res.get("embedded") == res.get("chunks") == n768
              and {m for m, _u in asked} == {"nomic-embed-text"}, (res, n768, sorted(set(asked))))
        found = await attempt("R find", lambda: rs.find(pool, E, project_id=None, thread_key="cidx-thread",
                                                        query="battery energy storage")) or {}
        check("R find searches it by meaning", "meaning" in (found.get("arms") or []), found)

        print("R2. research passages saved before a switch are filled by finds, and the find says what is left")
        para = " ".join(["The turbine blade survey logged corrosion on the northern ridge."] * 38)
        long_page = "\n\n".join(f"Section {i}. {para}" for i in range(3))   # three passages
        res2 = await attempt("R2 save", lambda: rs.save_page(
            pool, E, project_id=None, thread_key="cidx-thread", url="https://pilot.example/turbines",
            title="Turbine survey", text=long_page, whole=True)) or {}
        n_here = await pool.fetchval(
            "SELECT count(*) FROM research_chunks c JOIN research_pages p ON p.id = c.page_id "
            "WHERE p.thread_key = 'cidx-thread'")

        async def research_vectors(column):
            return [r["v"] for r in await pool.fetch(
                f"SELECT c.{column}::text AS v FROM research_chunks c JOIN research_pages p "
                "ON p.id = c.page_id WHERE p.thread_key = 'cidx-thread' ORDER BY c.page_id, c.chunk_index")]
        r768 = await attempt("R2 read the 768 column", lambda: research_vectors(col(768))) or []
        check("R2 precondition: four passages here, each with its 768 vector",
              res2.get("chunks") == 3 and n_here == 4 and len(r768) == 4 and all(r768), (res2, n_here))
        await configure(embedding_model="mxbai-embed-large", embedding_dim=1024, embedding_base_url=ENDPOINT)
        saved_wave = rs.EMBED_WAVE
        rs.EMBED_WAVE = 3
        try:
            asked.clear()
            t = await tools._exec_research({"op": "find", "query": "turbine corrosion"}, entity_id=E,
                                           pool=pool, thread_key="cidx-thread")
            r1024 = await attempt("R2 read the 1024 column", lambda: research_vectors(col(1024))) or []
            check("R2 the first find after the switch fills one wave (3) of the passages with the new model",
                  sum(1 for v in r1024 if v) == 3 and {m for m, _u in asked} == {"mxbai-embed-large"}
                  and len(asked) == 1 + 3, (r1024, asked))
            note = str(((t or {}).get("result") or {}).get("note"))
            check("R2 ...and the research tool says how many passages the search by meaning could not see, "
                  "with which model, never a find by meaning alone",
                  "1 of the 4 saved passages" in note and "mxbai-embed-large" in note, t)
            check("R2 the 768 vectors are as they were", await research_vectors(col(768)) == r768)
            f2 = await attempt("R2 find 2", lambda: rs.find(pool, E, project_id=None, thread_key="cidx-thread",
                                                           query="turbine corrosion")) or {}
            check("R2 the next find fills the last one and counts nothing left, naming the model",
                  f2.get("without_vectors") == 0 and f2.get("passages_here") == 4
                  and f2.get("embedding_model") == "mxbai-embed-large"
                  and all(await research_vectors(col(1024))), f2)
            asked.clear()
            f3 = await attempt("R2 find 3", lambda: rs.find(pool, E, project_id=None, thread_key="cidx-thread",
                                                           query="turbine corrosion")) or {}
            check("R2 once every passage has its vector, a find embeds the query only (fill only, nothing again)",
                  len(asked) == 1 and f3.get("without_vectors") == 0, (asked, f3))
        finally:
            rs.EMBED_WAVE = saved_wave

        print("R3. no embedding model set up: a find says so, never that no embedder answered")
        await configure()
        asked.clear()
        t = await tools._exec_research({"op": "find", "query": "turbine corrosion"}, entity_id=E,
                                       pool=pool, thread_key="cidx-thread")
        note = str(((t or {}).get("result") or {}).get("note"))
        check("R3 the find is by words, says embeddings are not set up yet, and asks no embedder",
              "not set up yet" in note and "no embedder answered" not in note and asked == []
              and bool(((t or {}).get("result") or {}).get("passages")), t)
    finally:
        (brain_service._pool, brain_service._entity_id, memory_engine._embed_via_byo,
         indexer.PROJECTS_ROOT, rs.RESEARCH_ROOT) = saved
        await pool.close()
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as srv, \
            kernel_schema_db(srv) as dsn:
        asyncio.run(run(dsn))
    print("RESULT: ALL PASS" if FAILED == 0 else f"RESULT: {FAILED} FAILED")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
