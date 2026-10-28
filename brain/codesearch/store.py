"""
In-pod code-search store — the Host's own Postgres: pgvector (vectors) +
tsvector (lexical), in one table, code_chunks (migrations/v200).

Single-tenant: lives IN the entity pod and indexes the entity's OWN named
projects (under /var/lib/cseries/projects) and the repositories it cloned. Until
2026-10-03 this was a SQLite file beside the pod's Postgres (sqlite-vec +
FTS5); there is no need to keep a second database. The index
now sits in the database the Host's memory, backups and audits already cover.

Namespace is `project`, not container_image. Embeddings + rerank are NOT computed
here — they go out on the entity's configured lane (bridge_embed); this module
only stores + retrieves. The searcher fuses the two arms (RRF) and reranks.

Every function takes `db`: the pod's asyncpg pool (connect()).
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger("codesearch.store")

# One vector column per embedding size, the sizes the Host's memory stores
# (mastercode.memory_engine.EMBEDDING_COLUMN_BY_DIM): migrations/v231. The 2560
# one is v200's `embedding`, kept under its name. Which column a pass writes
# and a search reads is the Host's CONFIGURED embedding model's size
# (bridge_embed.active_space); there is no default model and no default size.
# research_chunks has the same columns under the same names (v231;
# mastercode.runtime.research_store). scripts/test_code_index_follows_
# embedding_config_pg.py holds this map to memory's sizes and to the columns
# both tables have.
COLUMN_BY_DIM = {
    768: "embedding_768",      # vector(768)    + HNSW  (v231)
    1024: "embedding_1024",    # vector(1024)   + HNSW  (v231)
    2048: "embedding_2048",    # halfvec(2048)  + HNSW  (v231)
    2560: "embedding",         # halfvec(2560)  + HNSW  (v200)
    4096: "embedding_4096",    # halfvec(4096), no index: over HNSW's 4000 (v231)
}
# Sizes stored as halfvec: over 2000, where pgvector's HNSW stops for `vector`.
HALFVEC_DIMS = {2048, 2560, 4096}
# The SQLite index this module used to keep. Read once for its project list and
# removed (indexer.retire_sqlite_index); nothing writes it any more.
LEGACY_SQLITE_PATH = os.environ.get("CODESEARCH_DB", "/var/lib/cseries/codesearch.db")
# A project this size or smaller is ranked exactly (search_vector). 20,000
# chunks of 2560 halfvecs is ~100 MB compared per query at the top end (a
# smaller model's vectors, less); a Workbench project or a mid-size repository
# sits well under it.
EXACT_MAX = int(os.environ.get("CODESEARCH_EXACT_MAX", "20000"))
# Bigger projects walk the HNSW index; this bounds how far the iterative scan
# goes looking for `limit` rows of the project.
HNSW_MAX_SCAN = int(os.environ.get("CODESEARCH_HNSW_MAX_SCAN", "200000"))
# The HNSW walk's candidate list. pgvector's default (40) is what this used for
# every search (the searcher asks for 30). Measured 2026-10-03 on the fleet's
# cseries-postgres (pgvector 0.8.5, 512Mi, shared_buffers 32MB), one 50,000-chunk
# project of clustered 2560-dim vectors in a 70,000-row table, 40 queries,
# recall@30 against the exact ranking: ef 40 — mean 0.92, and 3 queries came
# back with 30 rows from the wrong cluster (recall 0, distance 0.95 where the
# true nearest sat at 0.31); ef 100/200/400 — mean 0.998, worst 0.97. At 200
# the query took p50 11 ms, p95 22 ms; the exact ranking of the same project
# took ~190 ms.
HNSW_EF_SEARCH = int(os.environ.get("CODESEARCH_HNSW_EF_SEARCH", "200"))


@dataclass
class Chunk:
    project: str
    file_path: str
    chunk_index: int
    content: str
    language: str
    chunk_kind: str
    symbol: Optional[str]
    start_line: int
    end_line: int
    file_hash: str


@dataclass(frozen=True)
class Space:
    """Where one embedding model's vectors live: the model the Host has
    configured and its size, which names the column (COLUMN_BY_DIM) and the
    type a vector is cast to. Built only for a size that has a column, so
    `column` and `cast` are always one of a fixed set — safe in SQL text."""
    model: str
    dim: int

    def __post_init__(self):
        if self.dim not in COLUMN_BY_DIM:
            raise ValueError(f"no code index column for {self.dim}-dimension vectors "
                             f"(sizes: {sorted(COLUMN_BY_DIM)})")

    @property
    def column(self) -> str:
        return COLUMN_BY_DIM[self.dim]

    @property
    def cast(self) -> str:
        return f"halfvec({self.dim})" if self.dim in HALFVEC_DIMS else f"vector({self.dim})"


def _lit(vec: list[float]) -> str:
    """pgvector's text form, cast to the space's type in the statement."""
    return "[" + ",".join(f"{float(x):.7f}" for x in vec) + "]"


def connect():
    """The pod's Postgres pool — the store has no connection of its own.
    Raises when the brain pool is not up yet, rather than handing callers a
    None they would fail on further in."""
    from server import brain_service
    pool = brain_service.get_pool()
    if pool is None:
        raise RuntimeError("code search: the Host's database is not ready yet")
    return pool


_CHUNK_COLS = ("project, file_path, chunk_index, content, language, chunk_kind, symbol, "
               "start_line, end_line, file_hash")


def _upsert_sql(space: Optional[Space]) -> str:
    """The write of one chunk, with its vector in `space`'s column (none
    when `space` is None). The unique key (project, file_path, chunk_index)
    refreshes a chunk in place, and every vector column follows one rule: the
    vector given here, else the one already there if the chunk's text is the
    same, else none. So a vector another model left in its own column stays
    while its text is unchanged (a model switch never drops one, as memory's
    never does), and a vector of text that changed never outlives it."""
    vec_col = f", {space.column}" if space is not None else ""
    vec_val = f", $11::{space.cast}" if space is not None else ""
    keep = ",\n        ".join(
        f"{c} = COALESCE(EXCLUDED.{c}, CASE WHEN code_chunks.content = EXCLUDED.content "
        f"THEN code_chunks.{c} END)" for c in COLUMN_BY_DIM.values())
    return f"""
    INSERT INTO code_chunks ({_CHUNK_COLS}{vec_col})
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10{vec_val})
    ON CONFLICT (project, file_path, chunk_index) DO UPDATE SET
        {keep},
        content = EXCLUDED.content, language = EXCLUDED.language,
        chunk_kind = EXCLUDED.chunk_kind, symbol = EXCLUDED.symbol,
        start_line = EXCLUDED.start_line, end_line = EXCLUDED.end_line,
        file_hash = EXCLUDED.file_hash, updated_at = now()
"""


def _rows(chunks, space: Optional[Space], *, keep_unembedded: bool = False) -> list[tuple]:
    """Rows for _upsert_sql(space). A chunk with no vector is skipped, or —
    with keep_unembedded — written with none (v224): the lexical arm finds
    it, the vector arm does not, and a later index pass embeds it. With no
    space there is no vector to write: every row is one without."""
    out = []
    for ch, vec in chunks:
        if (vec is None or space is None) and not keep_unembedded:
            continue
        row = (ch.project, ch.file_path, ch.chunk_index, ch.content, ch.language, ch.chunk_kind,
               ch.symbol, ch.start_line, ch.end_line, ch.file_hash)
        if space is not None:
            row += (_lit(vec) if vec is not None else None,)
        out.append(row)
    return out


async def upsert_chunks(db, *, chunks: list[tuple[Chunk, Optional[list[float]]]],
                        space: Space) -> int:
    """Upsert (chunk, embedding) pairs, each vector into `space`'s column.
    The unique key (project, file_path, chunk_index) refreshes a touched
    chunk in place. Skips null vectors. Returns count written. (The indexer
    replaces whole files: replace_file.)"""
    rows = _rows(chunks, space)
    if not rows:
        return 0
    await db.executemany(_upsert_sql(space), rows)
    return len(rows)


async def replace_file(db, *, project: str, file_path: str,
                       chunks: list[tuple[Chunk, Optional[list[float]]]],
                       space: Optional[Space], unembedded: bool = False) -> int:
    """One file's new version in place of its old one, in ONE transaction:
    every new chunk written, every old chunk the new version does not have
    out. A search running meanwhile sees the old version or the new one,
    never neither, never half; a failed write leaves the old version as it
    was. Returns chunks written.

    Vectors go into `space`'s column. A vector another model left in its own
    column stays on a chunk whose text did not change (_upsert_sql): writing
    an unchanged file again with the new model's vectors fills that model's
    column and keeps every other.

    unembedded=True writes chunks that have no vector (the embedder did not
    answer, or the Host has none set up) with none, so the file is searchable
    by its words."""
    rows = _rows(chunks, space, keep_unembedded=unembedded)
    keep = [r[2] for r in rows]
    async with db.acquire() as conn:
        async with conn.transaction():
            if rows:
                await conn.executemany(_upsert_sql(space), rows)
            await conn.execute(
                "DELETE FROM code_chunks WHERE project = $1 AND file_path = $2 "
                "AND chunk_index <> ALL($3::int[])", project, file_path, keep)
    return len(rows)


async def delete_file(db, *, project: str, file_path: str) -> int:
    """Remove every chunk for a file. Returns rows deleted."""
    tag = await db.execute("DELETE FROM code_chunks WHERE project = $1 AND file_path = $2",
                           project, file_path)
    return _count(tag)


async def get_file_hashes(db, *, project: str) -> dict[str, str]:
    """{file_path: file_hash} — the incremental diff: re-index changed files,
    delete vanished ones. A file's chunks are written together (replace_file),
    so they share one hash; one whose chunks do not (two passes at once, or
    rows from before that) answers '' — never equal to a real hash, so it is
    indexed again, and still listed, so it is dropped once gone from disk."""
    return {r["file_path"]: r["file_hash"] for r in await db.fetch("""
        SELECT file_path,
               CASE WHEN bool_and(file_hash IS NOT NULL) AND count(DISTINCT file_hash) = 1
                    THEN min(file_hash) ELSE '' END AS file_hash
          FROM code_chunks WHERE project = $1 GROUP BY file_path
    """, project)}


async def get_file_state(db, *, project: str,
                         space: Optional[Space]) -> dict[str, tuple[str, bool]]:
    """{file_path: (file_hash, embedded)} — get_file_hashes plus whether every
    chunk of the file has its vector in `space`'s column, the configured
    model's. A file written while no embedder answered (v224), or before the
    Host switched to this model, has its current content and hash but is not
    embedded here: the indexer embeds it once a lane answers, and leaves it
    alone (still lexically searchable) while none does. With no space (no
    model set up) no file is embedded."""
    embedded = f"bool_and({space.column} IS NOT NULL)" if space is not None else "false"
    return {r["file_path"]: (r["file_hash"], bool(r["embedded"])) for r in await db.fetch(f"""
        SELECT file_path,
               CASE WHEN bool_and(file_hash IS NOT NULL) AND count(DISTINCT file_hash) = 1
                    THEN min(file_hash) ELSE '' END AS file_hash,
               {embedded} AS embedded
          FROM code_chunks WHERE project = $1 GROUP BY file_path
    """, project)}


async def missing_vectors(db, *, project: str, space: Optional[Space]) -> int:
    """How many of the project's chunks have no vector in `space`'s column —
    every chunk when there is no space. 0 means a search by meaning sees the
    whole project with the configured model."""
    if space is None:
        return int(await db.fetchval(
            "SELECT count(*) FROM code_chunks WHERE project = $1", project) or 0)
    return int(await db.fetchval(
        f"SELECT count(*) FROM code_chunks WHERE project = $1 AND {space.column} IS NULL",
        project) or 0)


async def vector_coverage(db, *, project: str | None, space: Space) -> tuple[int, int]:
    """(chunks without a vector in `space`'s column, chunks) in one project,
    or with project None in every one — what a search by meaning with the
    configured model cannot see. After a switch to a model, every chunk
    indexed before it is in the first number until an index pass fills its
    column (a repo's next sync pass; a Workbench project's next index)."""
    r = await db.fetchrow(
        f"SELECT count(*) FILTER (WHERE {space.column} IS NULL) AS missing, count(*) AS total "
        "FROM code_chunks WHERE ($1::text IS NULL OR project = $1)", project)
    return int(r["missing"] or 0), int(r["total"] or 0)


async def search_vector(db, *, project: str | None, query_embedding: list[float],
                        space: Space, limit: int = 30) -> list[dict]:
    """Vector KNN within one project — or, with project None, across every
    project and repo in the index — nearest first, in `space`'s column (the
    configured model's; the query was embedded with that model). relevance =
    cosine similarity (1 - cosine distance). Each hit names its `project`. A
    chunk with no vector in that column yet (v224: written while no embedder
    answered; or before a model switch, until the next index pass) is never a
    vector hit; the lexical arm still finds it.

    A project of up to EXACT_MAX chunks is ranked exactly: every one of its
    rows is compared (found through the (project, ...) unique index), which
    for a few thousand rows is cheap and always right. The HNSW index is for
    the big ones, and only there, because HNSW cannot filter by project inside
    its walk: a plain scan takes ef_search candidates from the WHOLE table and
    drops the other projects' after, so a small project under a big one nearer
    the query came back short or empty. Measured on pgvector 0.8.5
    (scripts/test_codesearch_pg.py): iterative_scan=strict_order returned a
    small project's rows without its nearest one; relaxed_order returns the
    right rows unordered, so the big-project path uses it and sorts the rows
    here — and when even that walk comes back short (a project far from the
    query under other projects nearer it returned nothing at all), that one
    query is ranked exactly. The index makes big projects fast; it never
    decides what is returned. A search of everything has no filter for the
    walk to fall short on: it is the case HNSW answers directly."""
    q = _lit(query_embedding)
    vc, cast = space.column, space.cast
    cols = "project, file_path, chunk_index, content, language, chunk_kind, symbol, start_line, end_line"
    if project is None:
        scope, where, args = "TRUE", f"{vc} IS NOT NULL", [q, int(limit)]
        qp, lp = "$1", "$2"
    else:
        scope, where = "project = $1", f"project = $1 AND {vc} IS NOT NULL"
        args = [project, q, int(limit)]
        qp, lp = "$2", "$3"
    async with db.acquire() as conn:
        async with conn.transaction():
            # Every row in scope, with a vector or not: the count only picks
            # the exact path or the walk, and stays an index-only count.
            n = await conn.fetchval(
                f"SELECT count(*) FROM code_chunks WHERE {scope}", *args[:-2])
            exact_sql = f"""
                SELECT {cols}, {vc} <=> {qp}::{cast} AS distance
                  FROM code_chunks
                 WHERE {where}
                 ORDER BY ({vc} <=> {qp}::{cast}) + 0, id
                 LIMIT {lp}
            """   # `+ 0` keeps the planner off the HNSW index: this order is exact
            if n <= EXACT_MAX:
                rows = await conn.fetch(exact_sql, *args)
            else:
                await conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
                await conn.execute(f"SET LOCAL hnsw.max_scan_tuples = {HNSW_MAX_SCAN}")
                await conn.execute(
                    f"SET LOCAL hnsw.ef_search = {min(max(HNSW_EF_SEARCH, int(limit)), 1000)}")
                # The walk's own filter is the project only: HNSW holds no row
                # without a vector, so `{vc} IS NOT NULL` adds nothing here.
                # (4096 has no index: this is then an exact scan, still right.)
                rows = await conn.fetch(f"""
                    SELECT {cols}, {vc} <=> {qp}::{cast} AS distance
                      FROM code_chunks
                     WHERE {scope}
                     ORDER BY {vc} <=> {qp}::{cast}
                     LIMIT {lp}
                """, *args)
                # relaxed_order hands rows back only roughly ordered, and an
                # outer ORDER BY distance is planned away (the index order
                # looks like it already satisfies it) — so sort here.
                rows = sorted(rows, key=lambda r: float(r["distance"]))
                if len(rows) < min(int(limit), n):
                    # The walk gave up before finding `limit` rows of this
                    # project (other projects nearer the query filled it):
                    # rank this one query exactly rather than answer short.
                    rows = await conn.fetch(exact_sql, *args)
    return [{
        "project": r["project"],
        "file_path": r["file_path"], "chunk_index": r["chunk_index"], "content": r["content"],
        "language": r["language"], "chunk_kind": r["chunk_kind"], "symbol": r["symbol"],
        "start_line": r["start_line"], "end_line": r["end_line"],
        "relevance": max(0.0, 1.0 - float(r["distance"])),
    } for r in rows]


async def search_lexical(db, *, project: str | None, query: str, limit: int = 30) -> list[dict]:
    """Lexical arm — every term of the query OR-ed, each matched as a phrase
    (an identifier like _ticket_project is the words ticket, project in that
    order). Ranked by ts_rank_cd; the searcher RRF-fuses it with the vector
    arm. One project, or every one with project None; each hit names its
    `project`. Needs no vector, so it answers with no embedder at all. Empty
    on no term."""
    terms = [t for t in re.findall(r"[A-Za-z0-9_]+", query) if len(t) > 1]
    if not terms:
        return []
    rows = await db.fetch("""
        WITH q AS (
            SELECT (SELECT string_agg('(' || phraseto_tsquery('simple', t)::text || ')', ' | ')
                      FROM unnest($2::text[]) AS t
                     WHERE phraseto_tsquery('simple', t)::text <> '')::tsquery AS tsq
        )
        SELECT c.project, c.file_path, c.chunk_index, c.content, c.language, c.chunk_kind, c.symbol,
               c.start_line, c.end_line, ts_rank_cd(c.search_tsv, q.tsq) AS rank
          FROM code_chunks c, q
         WHERE q.tsq IS NOT NULL AND (c.project = $1 OR $1::text IS NULL) AND c.search_tsv @@ q.tsq
         ORDER BY rank DESC, c.id
         LIMIT $3
    """, project, terms, int(limit))
    return [{
        "project": r["project"],
        "file_path": r["file_path"], "chunk_index": r["chunk_index"], "content": r["content"],
        "language": r["language"], "chunk_kind": r["chunk_kind"], "symbol": r["symbol"],
        "start_line": r["start_line"], "end_line": r["end_line"],
        "lex_rank": float(r["rank"]),  # higher = better
    } for r in rows]


async def stats(db) -> list[dict]:
    rows = await db.fetch("""
        SELECT project, COUNT(DISTINCT file_path) AS files, COUNT(*) AS chunks,
               EXTRACT(EPOCH FROM MAX(updated_at))::float8 AS newest
          FROM code_chunks GROUP BY project ORDER BY chunks DESC
    """)
    return [{"project": r["project"], "files": r["files"], "chunks": r["chunks"],
             "newest": r["newest"]} for r in rows]


async def projects(db) -> list[str]:
    """The names of the projects the index holds, in name order. One probe of the
    (project, ...) unique index per project, never a pass over the chunks:
    stats' GROUP BY reads every row (measured on a 131,940-chunk,
    75-project index: 322 ms; this, 7 ms)."""
    rows = await db.fetch("""
        WITH RECURSIVE p AS (
            SELECT min(project) AS project FROM code_chunks
            UNION ALL
            SELECT (SELECT min(project) FROM code_chunks WHERE project > p.project)
              FROM p WHERE p.project IS NOT NULL
        )
        SELECT project FROM p WHERE project IS NOT NULL
    """)
    return [r["project"] for r in rows]


async def drop_project(db, *, project: str) -> int:
    return _count(await db.execute("DELETE FROM code_chunks WHERE project = $1", project))


def _count(tag) -> int:
    try:
        return int(str(tag).rsplit(" ", 1)[-1])
    except (ValueError, TypeError):
        return 0


def legacy_sqlite_projects(path: str | None = None) -> list[str] | None:
    """The projects the old SQLite index held, or None when there is no such
    file. Reads only its plain `chunks` table, which needs no extension. A
    file that cannot be read answers [] — it is still a leftover to remove."""
    p = Path(path or LEGACY_SQLITE_PATH)
    if not p.exists():
        return None
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            return [r[0] for r in con.execute("SELECT DISTINCT project FROM chunks ORDER BY 1")]
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        log.info("code search: could not read the old SQLite index's projects (%s)", e)
        return []


def remove_legacy_sqlite(path: str | None = None) -> None:
    """Delete the old SQLite index and its WAL/SHM/journal files."""
    path = path or LEGACY_SQLITE_PATH
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            Path(str(path) + suffix).unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("code search: could not remove %s%s (%s)", path, suffix, e)
