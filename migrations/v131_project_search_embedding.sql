-- v131 (2026-09-07): the project index — a stored embedding per project for
-- the semantic arm of GET /api/projects/search.
--
-- The Workbench's "What would you like to work on?" box ranks the entity's
-- projects against free text. The lexical arm is core Postgres full-text
-- over name / description / phase titles and needs no column. The semantic
-- arm compares the typed text against a stored per-project embedding — and
-- only when the host has an embedding model configured, because there is
-- no platform embedder (default-skills/embedding.md): unconfigured is a
-- setup state, and the search simply answers with the lexical ranking.
--
-- Stored as REAL[] rather than a pgvector column on purpose: the entity's
-- model sets the dimension (entity_memory carries one column per dim for
-- that reason), and an entity has hundreds of projects at most, so the
-- cosine ranking runs in the route over the rows it already fetched. No
-- dim-specific columns, no HNSW index, nothing to migrate when the user
-- switches models — the hash + model stamp mark a row stale and the search
-- re-embeds it in the background.
--
-- No tsvector expression index either: the document includes the phase
-- titles (an aggregate), which cannot be indexed, and the table is small.

ALTER TABLE projects ADD COLUMN IF NOT EXISTS search_embedding REAL[];
ALTER TABLE projects ADD COLUMN IF NOT EXISTS search_embedding_model TEXT;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS search_embedding_hash TEXT;
