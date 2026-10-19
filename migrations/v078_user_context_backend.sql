-- v078 — USER_CONTEXT backend parity (Part 4, Phase 8 decentralization).
--
-- BACKGROUND: Phase 7 (v076/v077) made the ENTITY context CONTENT layer pluggable
-- (pg | s3 | home bastion) and host-local-indexed. The v076 intent covered the
-- USER's corpus too (users store their context in their home bastion, owned by
-- the user and retrievable by the host), but only entity_context got wired. user_context.content
-- was always inline pg.
--
-- This migration gives user_context the SAME backend model with a ONE-COLUMN
-- marker (storage_backend), reusing context_backend_meta (+ the v077 locations
-- table) under the synthetic key context_backends.user_context_key(id). The pg
-- default path is byte-for-byte unchanged (NOT NULL DEFAULT 'pg').
--
-- ORDERING GUARD: user_context is created LAZILY at RUNTIME by
-- the application (server/routes/context.ensure_table → USER_CONTEXT_SCHEMA, which
-- ALREADY includes storage_backend). At boot-time migration, on a host that has
-- never used the context corpus, user_context DOES NOT EXIST yet — a bare
-- `ALTER TABLE user_context` there ERRORs and CRASHES the boot (caught on a
-- real-pod roll). So everything here is wrapped in a `to_regclass` existence guard:
-- if the table is absent, this migration is a clean NO-OP and the runtime self-heal
-- adds the column when it first creates the table. If it's present (a host that has
-- a user_context corpus), we add the column/constraint/index idempotently.
--
-- Idempotent + additive: ADD COLUMN IF NOT EXISTS, guarded CHECK, CREATE INDEX
-- IF NOT EXISTS. No data rewrite, no destructive ALTER.

BEGIN;

DO $$
BEGIN
    IF to_regclass('public.user_context') IS NULL THEN
        RAISE NOTICE 'v078: user_context absent at migration time — runtime self-heal will add storage_backend; skipping (no-op).';
    ELSE
        ALTER TABLE user_context
            ADD COLUMN IF NOT EXISTS storage_backend TEXT NOT NULL DEFAULT 'pg';

        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'user_context_storage_backend_check'
        ) THEN
            ALTER TABLE user_context
                ADD CONSTRAINT user_context_storage_backend_check
                CHECK (storage_backend IN ('pg', 's3', 'bastion'));
        END IF;

        CREATE INDEX IF NOT EXISTS ix_uc_storage_backend
            ON user_context (entity_id, storage_backend)
            WHERE storage_backend <> 'pg';
    END IF;
END$$;

COMMIT;
