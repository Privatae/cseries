-- v090 — Create user_context (close the lazy-create gap).
--
-- ROOT CAUSE: user_context has only ever had two creators — init-db.sql (runs
-- ONLY on a Postgres data dir's first-ever boot) and server/routes/context.py
-- ensure_table() / USER_CONTEXT_SCHEMA, which the code documents as "created
-- LAZILY at runtime" but which HAD NO CALLER. So on any entity whose pgdata was
-- initialized before user_context landed in init-db.sql (e.g. one provisioned
-- 2026-04-01), the table never got created and nothing ever created
-- it: init-db.sql never re-runs, and v049/v071/v078/v084 all no-op behind a
-- `to_regclass` guard (and record themselves applied, so they never re-run),
-- each trusting a runtime self-heal that never fired.
--
-- SYMPTOM: reads guard with user_context_exists() → empty corpus (looks fine),
-- but every WRITE hit a bare INSERT INTO user_context. The Context Manager's
-- file/binary upload lane (_ingest_binary_file) has no try/except, so a photo
-- upload surfaced as a raw HTTP 500 ("Something went wrong on the Host"); the
-- text lane dropped every row with imported=0. This migration creates the table
-- fleet-wide at next boot. context.ensure_table is now also wired into boot
-- (server/main.py) and the file lane self-heals before its insert.
--
-- SHAPE: the full runtime schema. Because v049/v071/v078/v084 are already
-- recorded-applied on affected entities and will not re-run, this must produce
-- the complete final shape in one shot — it mirrors server/routes/context.py
-- USER_CONTEXT_SCHEMA verbatim (keep the two in lockstep). Every statement is
-- IF NOT EXISTS / additive: on an entity that DOES already have the table (fresh
-- provision), this is a clean no-op.

BEGIN;

CREATE TABLE IF NOT EXISTS user_context (
    id              SERIAL PRIMARY KEY,
    entity_id       INTEGER NOT NULL,
    content         TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'manual',
    source_id       TEXT,
    title           TEXT,
    embedding       vector(1024),
    embedding_768   vector(768),
    embedding_2048  vector(2048),
    embedding_4096  vector(4096),
    tags            TEXT[] DEFAULT '{}',
    metadata        JSONB DEFAULT '{}',
    status          TEXT NOT NULL DEFAULT 'raw',
    promoted_to     INTEGER,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

-- Multi-dim embedding columns (mirror entity_memory), idempotent.
ALTER TABLE user_context ADD COLUMN IF NOT EXISTS embedding_768  vector(768);
ALTER TABLE user_context ADD COLUMN IF NOT EXISTS embedding_2048 vector(2048);
ALTER TABLE user_context ADD COLUMN IF NOT EXISTS embedding_4096 vector(4096);
ALTER TABLE user_context ADD COLUMN IF NOT EXISTS embedding_2560 halfvec(2560);
CREATE INDEX IF NOT EXISTS idx_user_context_embedding_2560_hnsw
    ON user_context USING hnsw (embedding_2560 halfvec_cosine_ops)
    WITH (m=16, ef_construction=64);

-- Context Explorer lexical leg (written at insert time from the real bytes).
ALTER TABLE user_context ADD COLUMN IF NOT EXISTS search_tsv tsvector;
CREATE INDEX IF NOT EXISTS ix_uc_search_tsv ON user_context USING gin (search_tsv);

-- Backend parity marker (pg | s3 | bastion); pg = inline content, the default.
ALTER TABLE user_context ADD COLUMN IF NOT EXISTS storage_backend TEXT NOT NULL DEFAULT 'pg';

CREATE INDEX IF NOT EXISTS ix_uc_entity    ON user_context(entity_id);
CREATE INDEX IF NOT EXISTS ix_uc_status    ON user_context(entity_id, status);
CREATE INDEX IF NOT EXISTS ix_uc_source    ON user_context(entity_id, source);
CREATE INDEX IF NOT EXISTS ix_uc_created   ON user_context(entity_id, created_at DESC);

COMMIT;
