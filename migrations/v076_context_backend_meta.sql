-- v076 — PLUGGABLE context CONTENT-storage backends (Phase 7).
--
-- Intent: user context has S3 and local bastion storage options. Users store
-- their context on their home bastion, retrievable by the host and owned by the
-- user; the S3 context store is metered.
--
-- The context CONTENT layer (the bytes of an entity's context files) lives in
-- entity_context.content by default ('pg' backend — UNCHANGED). Phase 7 makes the
-- content layer PLUGGABLE: a host may select an 's3' or 'bastion' backend
-- (stored per-host at behavioral_params.context_backend — no schema change for
-- that, it's an existing JSONB blob like coding_harnesses).
--
-- When a non-pg backend is selected, the entity_context ROW still exists (so
-- list / search / status / content_type / metadata are all UNCHANGED) but its
-- `content` column holds a small pointer marker, and the REAL byte location is
-- recorded HERE, keyed by (entity_id, file_path):
--
--   backend     'pg' | 's3' | 'bastion' — which store holds the bytes
--   object_key  the S3 key / the bastion remote path
--   size_bytes  the stored byte count (drives S3 metering + reporting)
--
-- The 'pg' default never writes a row here, so the default path has ZERO
-- dependency on this table. The application also CREATE-IF-NOT-EXISTS's this
-- table defensively (context_backends._ensure_meta) so a fresh pod self-heals if
-- the migration hasn't run yet.
--
-- Idempotent: re-running is a no-op.

BEGIN;

CREATE TABLE IF NOT EXISTS context_backend_meta (
    entity_id   INTEGER NOT NULL,
    file_path   TEXT    NOT NULL,
    backend     TEXT    NOT NULL,
    object_key  TEXT    NOT NULL,
    size_bytes  BIGINT  NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (entity_id, file_path)
);

-- Constrain backend to the known set without breaking existing rows. Guarded so
-- re-applying is a no-op.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'context_backend_meta_backend_check'
    ) THEN
        ALTER TABLE context_backend_meta
            ADD CONSTRAINT context_backend_meta_backend_check
            CHECK (backend IN ('pg', 's3', 'bastion'));
    END IF;
END$$;

-- Per-entity usage rollup (total bytes per backend) for the metering/usage view.
CREATE INDEX IF NOT EXISTS idx_context_backend_meta_entity_backend
    ON context_backend_meta (entity_id, backend);

COMMIT;
