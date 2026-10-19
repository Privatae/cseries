-- v200 (2026-10-03): the Host's code search lives in the Host's own Postgres.
--
-- There is no need to keep a second database.
-- brain/codesearch kept its index in a SQLite file beside the pod's Postgres
-- (/var/lib/cseries/codesearch.db, sqlite-vec + FTS5). That file was a second
-- database with its own extension and its own SQLite version to track (vec0's
-- KNN failed on the image's SQLite 3.40.1); the Host backup carried it only
-- as an opaque file in the apps tree. The index now sits here, next to the
-- memory it is searched with, and rides the same pg_dump.
--
-- One row per chunk of one file of one project. `project` is the Workbench
-- project directory name or a cloned repository's "owner/name"
-- (codesearch.indexer.project_root_for). The pod holds one entity's data, as
-- the SQLite file did, so the rows carry no entity_id.
--
--   embedding   halfvec(2560): qwen3-embedding:4b, the code-search model
--               (bridge_embed.EMBED_MODEL), stored the way user_context's
--               2560 column is (v071/v084) — `vector` stops at 2000
--               dimensions for HNSW, halfvec goes to 4000.
--   search_tsv  the lexical arm. 'simple', not 'english': code is
--               identifiers, and stemming or stop words would cut them.
--               Generated, because content is always inline here.
--
-- The old file is removed and its projects re-indexed at the first start
-- after this ships (codesearch.indexer.retire_sqlite_index_until_done, server/main.py):
-- re-embedding is cheap and copying vectors out of sqlite-vec is not.

CREATE TABLE IF NOT EXISTS code_chunks (
    id          BIGSERIAL PRIMARY KEY,
    project     TEXT NOT NULL,
    file_path   TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    content     TEXT NOT NULL,
    language    TEXT,
    chunk_kind  TEXT,
    symbol      TEXT,
    start_line  INTEGER,
    end_line    INTEGER,
    file_hash   TEXT,
    embedding   halfvec(2560) NOT NULL,
    search_tsv  tsvector GENERATED ALWAYS AS (
                    to_tsvector('simple', left(coalesce(symbol, '') || ' ' || file_path
                                               || ' ' || content, 200000))) STORED,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project, file_path, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_code_chunks_embedding_hnsw
    ON code_chunks USING hnsw (embedding halfvec_cosine_ops)
    WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS idx_code_chunks_search_tsv
    ON code_chunks USING gin (search_tsv);
