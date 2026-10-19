-- v211: the research store — whole sources, saved, chunked and
-- searched after.
--
-- Research results are written directly to the database and reasoned over
-- afterwards, because the best research comes from complete results; research
-- pages stay out of the repo, as folders and files.
--
-- `research` fetches every source WHOLE and saves it three ways: a markdown
-- file under the Host's research folder (/var/lib/cseries/research, a sibling
-- of projects/, never inside a project's repo), the task record's kept text
-- (task_sources, in a project), and here: one research_pages row per saved
-- page and its chunks in research_chunks, searched by meaning and by words.
--
-- A page belongs to a project, or (outside one) to the conversation it was
-- researched in (thread_key). Only the newest read of a source is searched:
-- a new read replaces the page row and its chunks.
--
-- research_chunks is its own table, not code_chunks (v200), because:
--   embedding   may be NULL — text search still works with no embedder
--               (code_chunks.embedding is NOT NULL);
--   search_tsv  is 'english' — prose, stemmed (code_chunks is 'simple', for
--               identifiers);
--   each row is tied to its page, and through it to a project or thread, so
--               the 30-day sweep and the task record line up.
-- There is no HNSW index: one project's pages are ranked exactly (an index
-- that cannot filter by project inside its walk drops a small project's
-- rows under a big one; codesearch/store.search_vector measured that).

BEGIN;

CREATE TABLE IF NOT EXISTS research_pages (
    id            BIGSERIAL PRIMARY KEY,
    entity_id     INTEGER NOT NULL REFERENCES entity_registry(id) ON DELETE CASCADE,
    project_id    INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    thread_key    TEXT,
    source_ref    TEXT NOT NULL,
    version       TEXT NOT NULL,
    title         TEXT,
    path          TEXT NOT NULL,
    chars         INTEGER NOT NULL,
    whole         BOOLEAN NOT NULL,
    via           TEXT,
    read_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT research_pages_scope CHECK (project_id IS NOT NULL OR thread_key IS NOT NULL)
);
-- One page per source per scope (a project, or a thread outside one).
CREATE UNIQUE INDEX IF NOT EXISTS uq_research_pages_project
    ON research_pages (entity_id, project_id, source_ref) WHERE project_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_research_pages_thread
    ON research_pages (entity_id, thread_key, source_ref) WHERE project_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_research_pages_last_used ON research_pages (last_used_at);

CREATE TABLE IF NOT EXISTS research_chunks (
    id           BIGSERIAL PRIMARY KEY,
    page_id      BIGINT NOT NULL REFERENCES research_pages(id) ON DELETE CASCADE,
    chunk_index  INTEGER NOT NULL,
    content      TEXT NOT NULL,
    embedding    halfvec(2560),
    search_tsv   tsvector GENERATED ALWAYS AS (
                     to_tsvector('english', left(content, 200000))) STORED,
    UNIQUE (page_id, chunk_index)
);
CREATE INDEX IF NOT EXISTS idx_research_chunks_search_tsv
    ON research_chunks USING gin (search_tsv);

COMMIT;
