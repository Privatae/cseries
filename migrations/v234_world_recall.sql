-- v234 — recall over what the Host builds, and recall it can fix.
--
-- world_catalog_vectors: the embedding of each object's purpose line in the
-- Host's own schema `world`, stamped with the model and a hash of the text, so
-- recall's `world` store can search by meaning and re-embed only what changed.
-- The same stamp-and-refresh shape the project search uses
-- (server/routes/projects.py, search_embedding / _model / _hash). REAL[] rather
-- than a vector column: the Host's embedder can be any size, and a catalog is
-- small enough to compare in Python. Written by the kernel's recall code; the
-- object names are the Host's, the rows are the kernel's.
--
-- memory_recall_aliases: words the Host adds to one of its memories so recall
-- finds it by them next time (memory_curate op='alias'). Searched by the memory
-- store's lexical arm beside the memory's own text; the memory row itself is
-- untouched. Goes with its memory.

CREATE TABLE IF NOT EXISTS world_catalog_vectors (
    object_name  TEXT        PRIMARY KEY,
    text_hash    TEXT        NOT NULL,
    model        TEXT        NOT NULL,
    vector       REAL[]      NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS memory_recall_aliases (
    alias_id        BIGSERIAL   PRIMARY KEY,
    memory_id       INTEGER     NOT NULL REFERENCES entity_memory(id) ON DELETE CASCADE,
    alias_text      TEXT        NOT NULL CHECK (length(btrim(alias_text)) BETWEEN 1 AND 300),
    alias_tsv       tsvector    GENERATED ALWAYS AS (to_tsvector('english', alias_text)) STORED,
    alias_added_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (memory_id, alias_text)
);

CREATE INDEX IF NOT EXISTS idx_memory_recall_aliases_tsv
    ON memory_recall_aliases USING gin (alias_tsv);
CREATE INDEX IF NOT EXISTS idx_memory_recall_aliases_memory
    ON memory_recall_aliases (memory_id);
