#!/usr/bin/env python3
"""
Gate: the Host's code search runs on the Host's own Postgres (migrations/v200).

There is no need for a second database. The
index used to be a SQLite file beside the pod's Postgres (sqlite-vec + FTS5);
brain/codesearch/store.py now keeps it in code_chunks (pgvector halfvec +
tsvector). This proves the store against a real Postgres carrying init-db.sql +
migrations, the way smoke runs it in the built image:

  V1  nearest first, only the asked project
  V2  the result count follows `limit`
  V3  a small project under a bigger one nearer the query still gets its own
      nearest rows — with a NEGATIVE CONTROL: a plain HNSW scan filtered by
      project returns it short, which is why small projects rank exactly
  V4  a big project walks the HNSW index (plan checked) and answers nearest
      first, matching the exact top hit and >= 8 of the exact top 10; a small
      project forced onto that path still gets its rows in order
  L1  an identifier (_ticket_project) is found by the lexical arm, first
  L2  a term in no chunk finds nothing; a query of only punctuation is []
  H1  searcher.search fuses both arms: a chunk only the vector arm finds and a
      chunk only the lexical arm finds both come back
  S1  upsert refreshes a chunk in place; get_file_hashes / delete_file /
      stats / drop_project
  V5  the HNSW walk runs with ef_search >= 200 — read from the server on the
      query's own connection (at 40, whole queries of a 50k-chunk project
      came back from the wrong cluster)
  R1  retire_sqlite_index: the old file's projects are indexed here and the
      file (and its -wal/-shm) removed; a project only partly indexed keeps
      the file; no file is a no-op
  R2  the embedding lane is asked first: down = nothing walked, file kept
  R3  a project that can never be indexed (a link) does not hold the file
  R4  the real indexer: a file only some of whose chunks embedded is never
      written half — a new one not at all, an edited one keeps its old
      version — and the next pass indexes it whole; gone from disk, dropped
  R5  the start-up task retries until the lane answers, not at the next start
  R6  while a changed file's new version is embedding, a search sees the old
      version (store.replace_file swaps them in one transaction)
  R7  a write that fails mid-file rolls back to the previous version
  R8  a file emptied of content loses its chunks

Run: SMOKE_PG_HOST=<host or socket dir> [SMOKE_PG_PORT/USER/PASSWORD/DB] \\
     python3 scripts/test_codesearch_pg.py
In the image: python3 /app/scripts/test_codesearch_pg.py
Prints OK <n> checks on success (smoke reads the last line).
"""
from __future__ import annotations

import asyncio
import math
import os
import pathlib
import random
import sqlite3
import sys
import tempfile

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))

FAILS: list = []
PASSED = 0


def check(label, cond, detail=""):
    global PASSED
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + ("" if cond else f"  [{str(detail)[:400]}]"))
    if cond:
        PASSED += 1
    else:
        FAILS.append(label)


P = "gate-cs-"          # every project this gate writes starts with this


class _EfSpyConn:
    """A pool connection that, just before the HNSW-path query, asks the
    server which hnsw.ef_search is in force on that same connection."""
    def __init__(self, conn, seen):
        self._c, self._seen = conn, seen

    def transaction(self):
        return self._c.transaction()

    async def execute(self, *a, **k):
        return await self._c.execute(*a, **k)

    async def fetchval(self, *a, **k):
        return await self._c.fetchval(*a, **k)

    async def fetch(self, sql, *a, **k):
        if "ORDER BY embedding <=>" in sql:     # the index-ordered (HNSW) query only
            self._seen.append(await self._c.fetchval("SHOW hnsw.ef_search"))
        return await self._c.fetch(sql, *a, **k)


class _EfSpyPool:
    def __init__(self, pool, seen):
        self._p, self._seen = pool, seen

    def acquire(self):
        outer = self

        class _Ctx:
            async def __aenter__(self):
                self.c = await outer._p.acquire()
                return _EfSpyConn(self.c, outer._seen)

            async def __aexit__(self, *exc):
                await outer._p.release(self.c)
        return _Ctx()


# The space this gate runs in: a Host configured with qwen3-embedding:4b,
# whose 2560-dimension vectors live in v200's `embedding` column, the one the
# HNSW checks below name. Following the configured model at every size is
# scripts/test_code_index_follows_embedding_config_pg.py's.
DIM = 2560


def space():
    from codesearch import store
    return store.Space(model="qwen3-embedding:4b", dim=DIM)


async def _space():
    """bridge_embed.active_space as a Host so configured answers it."""
    return space(), ""


def vec(*head: float) -> list[float]:
    """A 2560-dim vector: `head` then zeros, unit length."""
    v = list(head) + [0.0] * (DIM - len(head))
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def chunk(project, path, content, *, idx=0, symbol=None, fhash=None):
    from codesearch import store
    return store.Chunk(project=project, file_path=path, chunk_index=idx, content=content,
                       language="python", chunk_kind="function", symbol=symbol, start_line=1,
                       end_line=2, file_hash=fhash or path)


async def db() -> None:
    import asyncpg
    from codesearch import indexer, searcher, store, bridge_embed

    # Seq scan and sort off, so the planner reaches for an index wherever it
    # can: the exact path must still be exact, and the negative control and
    # V4 really walk HNSW rather than an exact scan picked for a small table.
    pool = await asyncpg.create_pool(
        host=os.environ.get("SMOKE_PG_HOST", "/var/run/postgresql"),
        port=int(os.environ.get("SMOKE_PG_PORT", "5432")),
        user=os.environ.get("SMOKE_PG_USER", "entity"),
        password=os.environ.get("SMOKE_PG_PASSWORD") or None,
        database=os.environ.get("SMOKE_PG_DB", "hostmem"),
        min_size=1, max_size=4,
        server_settings={"enable_seqscan": "off", "enable_sort": "off"})

    async def wipe():
        await pool.execute("DELETE FROM code_chunks WHERE project LIKE $1", P + "%")

    await wipe()
    sp = space()
    saved_space = bridge_embed.active_space
    bridge_embed.active_space = _space
    try:
        a, b, c = P + "a", P + "b", P + "c"
        await store.upsert_chunks(pool, space=sp, chunks=[
            # cosine to the query (1, 0, 0): near .707, mid .447, far 0 —
            # every one farther than any row of project b below (>= .78).
            (chunk(a, "near.py", "def near(): pass"), vec(1, 1, 0)),
            (chunk(a, "mid.py", "def mid(): pass"), vec(1, 2, 0)),
            (chunk(a, "far.py", "def far(): pass"), vec(0, 1, 0)),
            (chunk(c, "other.py", "def other(): pass"), vec(1, 0, 0)),
        ])
        hits = await store.search_vector(pool, space=sp, project=a, query_embedding=vec(1, 0, 0), limit=10)
        check("V1: nearest first, only the asked project",
              [h["file_path"] for h in hits] == ["near.py", "mid.py", "far.py"], hits)
        check("V1: relevance is cosine similarity",
              abs(hits[0]["relevance"] - math.sqrt(0.5)) < 1e-2 and abs(hits[1]["relevance"] - 1 / math.sqrt(5)) < 1e-2,
              [h["relevance"] for h in hits])
        hits = await store.search_vector(pool, space=sp, project=a, query_embedding=vec(1, 0, 0), limit=2)
        check("V2: the count follows limit", [h["file_path"] for h in hits] == ["near.py", "mid.py"], hits)

        # V3: 3,000 rows of project b, all nearer the query than any row of a.
        rnd = random.Random(7)
        bvecs = [vec(1, 0, 0, *[rnd.uniform(-0.1, 0.1) for _ in range(64)]) for _ in range(3000)]
        await store.upsert_chunks(pool, space=sp, chunks=[
            (chunk(b, f"b{i}.py", f"def b{i}(): pass"), bv) for i, bv in enumerate(bvecs)])
        await pool.execute("ANALYZE code_chunks")
        q = store._lit(vec(1, 0, 0))
        hits = await store.search_vector(pool, space=sp, project=a, query_embedding=vec(1, 0, 0), limit=2)
        check("V3: a small project under 3,000 nearer rows gets its own nearest two (exact path)",
              [h["file_path"] for h in hits] == ["near.py", "mid.py"], hits)
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SET LOCAL hnsw.iterative_scan = off")
                plain = await conn.fetch(
                    f"SELECT file_path FROM code_chunks WHERE project = $1 "
                    f"ORDER BY embedding <=> $2::halfvec({DIM}) LIMIT 2", a, q)
        check("V3 negative control: a plain HNSW scan filtered by project comes back short",
              len(plain) < 2, [r["file_path"] for r in plain])

        # V4: the big-project path walks the HNSW index (EXACT_MAX lowered so
        # 3,000 rows count as big) and still answers nearest first.
        saved_max = store.EXACT_MAX
        store.EXACT_MAX = 100
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
                    plan = "\n".join(r[0] for r in await conn.fetch(
                        f"EXPLAIN SELECT file_path FROM code_chunks WHERE project = $1 "
                        f"ORDER BY embedding <=> $2::halfvec({DIM}) LIMIT 10", b, q))
            check("V4: a big project's query walks the HNSW index", "idx_code_chunks_embedding_hnsw" in plan, plan)
            qv = vec(1, 0, 0, 0.1, -0.1, 0.05)
            hits = await store.search_vector(pool, space=sp, project=b, query_embedding=qv, limit=10)
            exact = sorted(range(3000), key=lambda i: -sum(x * y for x, y in zip(bvecs[i][:67], qv[:67])))[:10]
            want = [f"b{i}.py" for i in exact]
            got = [h["file_path"] for h in hits]
            check("V4: big project — 10 rows, nearest first", len(got) == 10
                  and all(hits[k]["relevance"] >= hits[k + 1]["relevance"] for k in range(9)), hits)
            check("V4: big project — top hit exact, >= 8 of the exact top 10 (approximate index)",
                  got[0] == want[0] and len(set(got) & set(want)) >= 8, (got, want))
            store.EXACT_MAX = 0
            hits = await store.search_vector(pool, space=sp, project=a, query_embedding=vec(1, 0, 0), limit=2)
            check("V4: when the HNSW walk comes back short, that query is ranked exactly — rows, nearest first",
                  [h["file_path"] for h in hits] == ["near.py", "mid.py"], hits)
            # V5: the candidate list the server actually walks with, read on
            # the query's own connection inside its transaction. At pgvector's
            # default 40 a 50,000-chunk project lost whole queries to the wrong
            # cluster (store.HNSW_EF_SEARCH); this reads the setting in force,
            # it does not supply it.
            store.EXACT_MAX = 100
            seen_ef: list = []
            await store.search_vector(_EfSpyPool(pool, seen_ef), space=sp, project=b, query_embedding=qv, limit=30)
            check("V5: the HNSW walk runs with ef_search >= 200 (40 lost whole queries at 50k chunks)",
                  len(seen_ef) == 1 and int(seen_ef[0]) >= 200, seen_ef)
        finally:
            store.EXACT_MAX = saved_max

        # Lexical.
        await store.upsert_chunks(pool, space=sp, chunks=[
            (chunk(c, "support.py", "async def _ticket_project(pool, eid, pid, md):\n    return 1",
                   symbol="_ticket_project"), vec(0, 0, 1)),
            (chunk(c, "words.py", "ticket and project are words here, far apart: ticket ... the project"),
             vec(0, 0, 1)),
        ])
        hits = await store.search_lexical(pool, project=c, query="_ticket_project", limit=5)
        check("L1: an identifier is found by the lexical arm, first",
              bool(hits) and hits[0]["file_path"] == "support.py", hits)
        check("L1: lex_rank is higher-is-better", all(h["lex_rank"] > 0 for h in hits), hits)
        hits = await store.search_lexical(pool, project=c, query="zzqqxx_nothing", limit=5)
        check("L2: a term in no chunk finds nothing", hits == [], hits)
        check("L2: a query with no word in it is []",
              await store.search_lexical(pool, project=c, query="!! ?", limit=5) == [])

        # Hybrid through the real searcher, the remote arms stubbed.
        saved = (bridge_embed.embed, bridge_embed.rerank)

        async def _embed(text, *, space=None):
            return vec(1, 0, 0)

        async def _rerank(query, docs):
            return None   # the BYO/bastion shape: RRF order stands
        bridge_embed.embed, bridge_embed.rerank = _embed, _rerank
        try:
            res = await searcher.search(pool, project=c, query="_ticket_project", limit=10)
        finally:
            bridge_embed.embed, bridge_embed.rerank = saved
        files = [h["file_path"] for h in res]
        check("H1: hybrid search returns the lexical-only hit and the vector-only hit",
              "support.py" in files and "other.py" in files, files)

        # Store housekeeping.
        await store.upsert_chunks(pool, space=sp, chunks=[(chunk(a, "near.py", "def near2(): pass", fhash="h2"),
                                                 vec(1, 1, 0))])
        row = await pool.fetchrow("SELECT content, file_hash FROM code_chunks WHERE project=$1 AND file_path='near.py'", a)
        check("S1: upsert refreshes a chunk in place", row and row["content"] == "def near2(): pass"
              and row["file_hash"] == "h2", row)
        hashes = await store.get_file_hashes(pool, project=a)
        check("S1: get_file_hashes", hashes == {"near.py": "h2", "mid.py": "mid.py", "far.py": "far.py"}, hashes)
        check("S1: delete_file counts what it removed", await store.delete_file(pool, project=a, file_path="far.py") == 1)
        st = {s["project"]: s for s in await store.stats(pool)}
        check("S1: stats per project", st.get(a, {}).get("chunks") == 2 and st.get(b, {}).get("chunks") == 3000, st.get(a))
        check("S1: a chunk with no vector is skipped (the indexer retries it)",
              await store.upsert_chunks(pool, space=sp, chunks=[(chunk(a, "x.py", "x"), None)]) == 0)
        check("S1: drop_project", await store.drop_project(pool, project=b) == 3000)

        # R1: the old SQLite index is re-indexed here, then removed.
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="gate-cs-legacy-"))
        legacy = tmp / "codesearch.db"

        def make_legacy():
            con = sqlite3.connect(legacy)
            con.execute("CREATE TABLE chunks (id INTEGER PRIMARY KEY, project TEXT)")
            con.executemany("INSERT INTO chunks (project) VALUES (?)", [("p1",), ("p1",), ("owner/r",), ("gone",)])
            con.commit()
            con.close()
            (tmp / "codesearch.db-wal").write_text("")
            (tmp / "codesearch.db-shm").write_text("")

        (tmp / "projects" / "p1").mkdir(parents=True)
        (tmp / "repos" / "owner" / "r" / ".git").mkdir(parents=True)
        saved_idx = (store.LEGACY_SQLITE_PATH, indexer.PROJECTS_ROOT, indexer.REPOS_ROOT, indexer.index_project,
                     bridge_embed.embed, bridge_embed.embed_many)
        store.LEGACY_SQLITE_PATH = str(legacy)
        indexer.PROJECTS_ROOT, indexer.REPOS_ROOT = str(tmp / "projects"), str(tmp / "repos")
        seen: list = []
        lane = {"up": True}

        async def _probe(text, *, space=None):
            return vec(1, 0, 0) if lane["up"] else None
        bridge_embed.embed = _probe
        # owner/r: three of its chunks did not embed (the lane dropped mid-run).
        outcome = {"p1": {"files_changed": 2, "chunks_written": 5, "chunks_failed": 0},
                   "owner/r": {"files_changed": 3, "chunks_written": 4, "chunks_failed": 3}}

        async def _index(db_, *, project, project_root=None):
            seen.append(project)
            return dict(outcome[project], project=project)
        indexer.index_project = _index
        try:
            make_legacy()
            r = await indexer.retire_sqlite_index(pool)
            check("R1: every project of the old file that still exists is indexed here (a gone one is not)",
                  sorted(seen) == ["owner/r", "p1"], seen)
            check("R1: a project only partly indexed (some chunks failed to embed) keeps the old file",
                  legacy.exists() and r.get("pending") == ["owner/r"] and not r["retired"], r)
            outcome["owner/r"] = {"files_changed": 3, "chunks_written": 3, "chunks_failed": 0}
            seen.clear()
            r = await indexer.retire_sqlite_index(pool)
            check("R1: once all index, the old file and its -wal/-shm are removed",
                  r["retired"] and not any((tmp / n).exists()
                                           for n in ("codesearch.db", "codesearch.db-wal", "codesearch.db-shm")), r)
            seen.clear()
            r = await indexer.retire_sqlite_index(pool)
            check("R1: with no old file it does nothing", r == {"retired": False, "reason": "no old index"}
                  and seen == [], r)

            # R2: the lane is asked once before any walk; down = nothing walked, file kept.
            make_legacy()
            seen.clear()
            lane["up"] = False
            r = await indexer.retire_sqlite_index(pool)
            check("R2: with the embedding lane down no project is walked and the old file stays",
                  seen == [] and legacy.exists() and not r["retired"] and sorted(r.get("pending") or []) == ["owner/r", "p1"],
                  (seen, r))
            lane["up"] = True

            # R3: a project index_project refuses (a link, not a directory) holds nothing back.
            outcome["owner/r"] = {"error": "not a directory (a link?): /x"}
            seen.clear()
            r = await indexer.retire_sqlite_index(pool)
            check("R3: a project that can never be indexed (a link) does not keep the old file forever",
                  r.get("retired") is True and not legacy.exists(), r)
            outcome["owner/r"] = {"files_changed": 0, "chunks_written": 0, "chunks_failed": 0}

            # R5: the start-up task tries again once the lane answers — not at the next pod start.
            make_legacy()
            seen.clear()
            lane["up"] = False
            probes = {"n": 0}

            async def _probe_then_up(text, *, space=None):
                probes["n"] += 1
                return vec(1, 0, 0) if probes["n"] >= 3 else None
            bridge_embed.embed = _probe_then_up
            try:
                r = await asyncio.wait_for(
                    indexer.retire_sqlite_index_until_done(pool, retry_s=0.01, retry_max_s=0.02), 10)
            except Exception as e:  # noqa: BLE001 — a missing retry is this check's failure
                r = {"error": repr(e)}
            check("R5: a lane down at start is retried in the background until the old file is retired",
                  r.get("retired") is True and probes["n"] == 3 and not legacy.exists(), (r, probes))
            bridge_embed.embed = _probe
        finally:
            (store.LEGACY_SQLITE_PATH, indexer.PROJECTS_ROOT, indexer.REPOS_ROOT, indexer.index_project,
             bridge_embed.embed, bridge_embed.embed_many) = saved_idx

        # R4/R6/R7/R8: the real indexer, the real store. A file is replaced
        # whole or not at all: never half (some chunks unembedded), never
        # absent while its new version is being embedded, never lost to a
        # failed write.
        from codesearch import chunker
        proj = P + "partial"
        root = tmp / "partial"
        root.mkdir()
        lines = [f"line {i} of the notes about the code search store" for i in range(150)]
        lines[140] = "FAILME this window does not embed on the first pass"
        (root / "notes.md").write_text("\n".join(lines) + "\n")
        (root / "other.md").write_text("\n".join(f"other line {i} with enough words" for i in range(20)) + "\n")
        pre = chunker.chunk_file("notes.md", (root / "notes.md").read_text())
        check("R4 precondition: notes.md is several chunks and only a later one carries the marker",
              len(pre) >= 2 and "FAILME" not in pre[0].content and any("FAILME" in c.content for c in pre),
              [len(c.content) for c in pre])
        mode = {"fail": True, "dim": DIM}
        seen_mid: list = []

        async def notes_rows():
            return [(r["content"], r["file_hash"]) for r in await pool.fetch(
                "SELECT content, file_hash FROM code_chunks WHERE project = $1 AND file_path = 'notes.md' "
                "ORDER BY chunk_index", proj)]

        async def _embed_many(texts, *, space=None):
            seen_mid.append(await notes_rows())     # what a search sees while this pass embeds
            if mode["dim"] != DIM:
                return [[0.1] * mode["dim"] for _ in texts]
            return [None if (mode["fail"] and "FAILME" in t) else vec(0, 1, 0) for t in texts]
        bridge_embed.embed_many = _embed_many
        try:
            # R4: one chunk of a new file fails -> nothing of it is written.
            r1 = await indexer.index_project(pool, project=proj, project_root=str(root))
            h1 = await store.get_file_hashes(pool, project=proj)
            mode["fail"] = False
            r2 = await indexer.index_project(pool, project=proj, project_root=str(root))
            v1 = await notes_rows()
            h2 = await store.get_file_hashes(pool, project=proj)
            # R4: an edit whose embedding partly fails keeps the old version whole.
            mode["fail"] = True
            (root / "notes.md").write_text("\n".join(lines) + "\nan edit\n")
            r3 = await indexer.index_project(pool, project=proj, project_root=str(root))
            v_after_fail = await notes_rows()
            # R6: an edit embedding normally — mid-pass a search sees the old version.
            mode["fail"] = False
            lines2 = [l.replace("FAILME", "fixed") for l in lines]
            (root / "notes.md").write_text("\n".join(lines2) + "\n")
            post = chunker.chunk_file("notes.md", (root / "notes.md").read_text())
            seen_mid.clear()
            await indexer.index_project(pool, project=proj, project_root=str(root))
            mid, v2 = list(seen_mid), await notes_rows()
            # R7: the write fails (a vector of the wrong size) -> the old version stays.
            (root / "notes.md").write_text("\n".join(lines2) + "\nanother edit\n")
            mode["dim"] = 3
            try:
                await indexer.index_project(pool, project=proj, project_root=str(root))
                raised = False
            except Exception:  # noqa: BLE001 — the halfvec cast refuses 3 dimensions
                raised = True
            mode["dim"] = DIM
            v_after_bad_write = await notes_rows()
            # R8: a file emptied of all content loses its chunks.
            (root / "other.md").write_text("   \n")
            await indexer.index_project(pool, project=proj, project_root=str(root))
            n_other = await pool.fetchval(
                "SELECT count(*) FROM code_chunks WHERE project = $1 AND file_path = 'other.md'", proj)
            # Gone from disk -> gone from the index.
            (root / "notes.md").unlink()
            await indexer.index_project(pool, project=proj, project_root=str(root))
            n_gone = await pool.fetchval(
                "SELECT count(*) FROM code_chunks WHERE project = $1 AND file_path = 'notes.md'", proj)
        finally:
            bridge_embed.embed_many = saved_idx[5]
        old_version = [(c.content, c.file_hash) for c in pre]
        check("R4: the pass reports the chunk that did not embed", r1.get("chunks_failed") == 1, r1)
        check("R4: a new file with a chunk unembedded is not written half — none of it, and it reads as new",
              "notes.md" not in h1 and bool(h1.get("other.md")), h1)
        check("R4: the next pass indexes that file whole",
              r2.get("files_changed") == 1 and v1 == old_version and h2.get("notes.md") == pre[0].file_hash,
              (r2, len(v1), len(pre), h2))
        check("R4: an edit whose embedding partly fails leaves the old version whole (and retried next pass)",
              r3.get("chunks_failed") == 1 and v_after_fail == old_version, (r3, len(v_after_fail)))
        check("R6: while the new version is embedding, a search sees the old version — never an absent file",
              bool(mid) and all(m == old_version for m in mid), [len(m) for m in mid])
        check("R6: once embedded, the new version replaces it — no old chunk left behind",
              v2 == [(c.content, c.file_hash) for c in post], (len(v2), len(post)))
        check("R7: a write that fails mid-file rolls back — the previous version stays whole",
              raised and v_after_bad_write == v2, (raised, len(v_after_bad_write)))
        check("R8: a file emptied of content loses its chunks", n_other == 0, n_other)
        check("R4: a file gone from disk is dropped from the index", n_gone == 0, n_gone)
    finally:
        bridge_embed.active_space = saved_space
        await wipe()
        await pool.close()


def main() -> int:
    asyncio.run(db())
    if FAILS:
        print(f"FAIL {len(FAILS)} of {len(FAILS) + PASSED}: " + "; ".join(FAILS))
        return 1
    print(f"OK {PASSED} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
