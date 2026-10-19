-- v077 — HOST-LOCAL CONTEXT INDEX: record WHICH machine/bucket holds each file,
--        and support a future per-file LIST of >1 location (replicate policy).
--
-- BACKGROUND (v076): context_backend_meta(entity_id, file_path, backend, object_key,
-- size_bytes, updated_at), PK (entity_id, file_path), records ONLY non-pg files. It
-- knows the BACKEND (pg|s3|bastion) and the object_key, but NOT *which* machine or
-- bucket holds the bytes (the server/bucket only lived in the host's CURRENT
-- behavioral_params.context_backend selection — lost the moment the host switched
-- backends), and it holds exactly ONE row per file (no replication).
--
-- This migration closes both gaps WITHOUT disturbing the pg default path or the
-- existing v076 primary-location index:
--
--   (1) ADDITIVE COLUMNS on context_backend_meta — server / bucket / endpoint —
--       so each PRIMARY-location row records WHICH machine (bastion `server`) or
--       WHICH bucket+endpoint (s3) holds the bytes. PK (entity_id, file_path) is
--       UNCHANGED, so every existing query (_record_meta upsert, _read_meta,
--       the context_backend usage rollup) keeps working byte-for-byte. The new
--       columns are NULLable and default NULL — old rows and the pg default path
--       are completely unaffected.
--
--   (2) A NEW CHILD TABLE context_backend_locations for the FUTURE replicate /
--       availability policy: 1..N locations per (entity_id, file_path), each with
--       its own backend + server/bucket/endpoint + object_key + size + is_primary.
--       context_backend_meta stays the authoritative SINGLE primary-location index
--       (the byte the retrieve path reads); context_backend_locations is the
--       superset the replicate policy writes additional copies into. Today nothing
--       writes the child table — it's the seam for the replicate policy, shipped
--       inert so the schema is ready and the index op can UNION it in when present.
--
-- WHY two shapes instead of relaxing the PK: relaxing context_backend_meta's PK to
-- (entity_id,file_path,backend,coalesce(server,bucket,'')) would force a synthetic
-- key (coalesce isn't usable in a PK without a generated column) and rewrite every
-- ON CONFLICT in context_backends.py — high blast radius on the live retrieve path
-- for zero immediate benefit. Keeping the proven single-primary index AND adding a
-- purpose-built child table is the clean, low-risk split: the primary path is
-- untouched, and multi-location is a first-class table when the policy lands.
--
-- Idempotent + additive: ADD COLUMN IF NOT EXISTS, CREATE TABLE/INDEX IF NOT EXISTS.
-- No destructive ALTER, no data rewrite. The application also self-heals these
-- shapes defensively at runtime (context_backends._ensure_meta) for a fresh pod
-- whose migration hasn't run yet.

BEGIN;

-- (1) Location columns on the v076 primary-location index ----------------------
--   server    bastion host id (a box in the user's mesh) that holds the bytes
--   bucket    s3 bucket name that holds the bytes
--   endpoint  s3 endpoint_url (R2/B2/MinIO) — disambiguates a bucket name across
--             providers; NULL = AWS default endpoint
ALTER TABLE context_backend_meta ADD COLUMN IF NOT EXISTS server   TEXT;
ALTER TABLE context_backend_meta ADD COLUMN IF NOT EXISTS bucket   TEXT;
ALTER TABLE context_backend_meta ADD COLUMN IF NOT EXISTS endpoint TEXT;

-- (2) Child table: 1..N locations per file (replicate / availability policy) ---
-- context_backend_meta remains the SINGLE primary-location index; this is the
-- superset a future replicate policy writes extra copies into. A synthetic id PK
-- (not (entity_id,file_path,...)) keeps multi-location truly open-ended.
CREATE TABLE IF NOT EXISTS context_backend_locations (
    id          BIGSERIAL PRIMARY KEY,
    entity_id   INTEGER     NOT NULL,
    file_path   TEXT        NOT NULL,
    backend     TEXT        NOT NULL,
    server      TEXT,                          -- bastion host id (bastion backend)
    bucket      TEXT,                          -- s3 bucket (s3 backend)
    endpoint    TEXT,                          -- s3 endpoint_url (R2/B2/MinIO); NULL = AWS
    object_key  TEXT        NOT NULL,
    size_bytes  BIGINT      NOT NULL DEFAULT 0,
    is_primary  BOOLEAN     NOT NULL DEFAULT TRUE,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Constrain backend to the known set (guarded so re-applying is a no-op).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'context_backend_locations_backend_check'
    ) THEN
        ALTER TABLE context_backend_locations
            ADD CONSTRAINT context_backend_locations_backend_check
            CHECK (backend IN ('pg', 's3', 'bastion'));
    END IF;
END$$;

-- One row per (file, physical location). A (server,bucket,endpoint) tuple pins the
-- exact machine/bucket; COALESCE to '' so NULLs don't defeat the uniqueness
-- (postgres treats NULLs as distinct in a plain UNIQUE). Expression uniqueness MUST
-- be a UNIQUE INDEX (a table-level UNIQUE constraint can't hold expressions).
CREATE UNIQUE INDEX IF NOT EXISTS uq_context_backend_locations_phys
    ON context_backend_locations (
        entity_id, file_path, backend,
        COALESCE(server, ''), COALESCE(bucket, ''), COALESCE(endpoint, ''));

-- Index by (entity_id, file_path) — the per-file location lookup — and by
-- (entity_id, backend) — the per-backend usage rollup.
CREATE INDEX IF NOT EXISTS idx_context_backend_locations_entity_file
    ON context_backend_locations (entity_id, file_path);
CREATE INDEX IF NOT EXISTS idx_context_backend_locations_entity_backend
    ON context_backend_locations (entity_id, backend);

COMMIT;
