-- v096: entity_memory lexical arm.
--
-- entity_memory retrieval was embeddings-or-nothing: with the embed lane down
-- the only fallback was an ILIKE substring scan (retired with this migration's
-- companion code change). This adds the FTS column the hybrid recall engine's
-- lexical arm reads.
--
-- GENERATED column, deliberately: entity_memory has 15+ INSERT sites across
-- 8 files. Per-writer stamping is the pattern that produced user_context's
-- forgotten-write-site class (v084 added the column, v095 repaired the rows
-- the writers had indexed wrong) — one schema-level definition covers every
-- writer, present and future, structurally.
--
-- The second, separator-normalized pass mirrors v095's lesson: the FTS parser
-- treats 'budget.csv' or 'x0000.ledger' as single file tokens, so a search
-- for "budget" could never match them lexically. Normalizing [._-/] to
-- spaces makes the embedded words real lexemes. 100k cap per pass keeps a
-- pathological row inside tsvector limits (memory entries are KB-scale).
--
-- Backfill is implicit: ADD COLUMN ... GENERATED STORED rewrites the table
-- and computes every existing row (thousands per entity — fine at boot).
-- init-db.sql carries the same definition for fresh pods (the v090 lesson:
-- a migration must produce exactly what init-db produces).

BEGIN;

ALTER TABLE entity_memory ADD COLUMN IF NOT EXISTS search_tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('english'::regconfig,
        left(coalesce(content, ''), 100000) || ' ' ||
        left(regexp_replace(coalesce(content, ''), '[._\-/]+', ' ', 'g'), 100000)
    )) STORED;

CREATE INDEX IF NOT EXISTS idx_entity_memory_search_tsv
    ON entity_memory USING gin (search_tsv);

COMMIT;
