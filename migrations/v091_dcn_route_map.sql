-- v091 — DCN ROUTE-MAP GROUNDWORK (CN-0): the Distributed Context Network's own
--        index layer — stores / files / chunks — for context that lives on the
--        USER's machines and buckets (store classes, grants, tiers; the schema
--        approved with the CN-0 greenlight).
--
-- WHAT THIS IS NOT: placement. The existing placement layer (context_backend_meta /
-- context_backend_locations / VALID_BACKENDS pg|s3|bastion) answers "where do the
-- HOST's context bytes go" and its ContextBackend contract is store/retrieve/REMOVE.
-- The route map answers "what exists in the user's granted stores, where, and who
-- may touch it" — and its surface has NO delete operation, by design (DoR §7).
-- 'dcn' therefore NEVER enters VALID_BACKENDS or the v077 backend CHECK: keeping
-- the DCN off the placement contract is what makes "an agent cannot rm through
-- this surface because the operation does not exist" STRUCTURALLY true, not policy.
-- The two layers meet only as a read-side VIEW ("two systems, one route map view",
-- DoR §3).
--
-- TIER HONESTY (DoR §8, §14): machine-class stores are indexed as LOCATIONS ONLY —
-- no content bytes on C-Series, ever. Enforced at the SCHEMA level, not policy:
--   * dcn_chunks carries NO content/preview/snippet column at all;
--   * previews live in a separate dcn_chunk_previews table;
--   * a trigger REFUSES preview rows whose chunk belongs to a machine-class store.
-- Cabinet (S3) and platform-class chunks may carry previews — data the user
-- deliberately filed on/through C-Series gets the full filing-cabinet index.
--
-- NO-DELETE, IN SCHEMA: dcn_stores.mask can never contain 'delete' (CHECK below).
-- There is no handler to gate — the vocabulary itself refuses the word.
--
-- Embedding vectors are deliberately ABSENT: they land at CN-2 with the configured
-- embedding lane (embeddings are config-driven — no dimension pins in groundwork).
--
-- Shipped inert, v077-style: nothing writes these tables yet. CN-1 (grants) and
-- CN-2 (indexers) fill them; CN-0 renders derived store cards (absorbed portal
-- folders, the existing S3 placement config) without needing rows here.

BEGIN;

-- ── dcn_stores — one row per granted store (DoR §7) ──────────────────────────
CREATE TABLE IF NOT EXISTS dcn_stores (
    id                BIGSERIAL   PRIMARY KEY,
    entity_id         INTEGER     NOT NULL,
    class             TEXT        NOT NULL,
    -- machine-class location (a directory on a box the user runs)
    server            TEXT,                       -- bastion hostname
    root              TEXT,                       -- granted directory root
    -- cabinet-class location (a bucket/prefix the user owns)
    bucket            TEXT,
    prefix            TEXT,
    endpoint          TEXT,                       -- NULL = AWS; R2/B2/MinIO otherwise
    cred_name         TEXT,                       -- vault credential (cabinet)
    -- grant surface (rows are created ONLY by the CN-1 NAC grant flow)
    mask              JSONB       NOT NULL DEFAULT '["read","search","index"]'::jsonb,
    sensitive         BOOLEAN     NOT NULL DEFAULT FALSE,  -- CN-3: access-time 2FA
    excludes          JSONB       NOT NULL DEFAULT '[]'::jsonb,
    sweep             TEXT        NOT NULL DEFAULT 'hourly',
    status            TEXT        NOT NULL DEFAULT 'draft',
    display_name      TEXT,
    legacy_portal_id  TEXT,                       -- absorption pointer: the portal
                                                  -- sources.yaml id this store
                                                  -- supersedes (DoR §2, decided)
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Constrain the vocabulary (guarded so re-applying is a no-op).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'dcn_stores_class_check') THEN
        ALTER TABLE dcn_stores ADD CONSTRAINT dcn_stores_class_check
            CHECK (class IN ('machine', 'cabinet', 'platform'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'dcn_stores_sweep_check') THEN
        ALTER TABLE dcn_stores ADD CONSTRAINT dcn_stores_sweep_check
            CHECK (sweep IN ('15m', 'hourly', 'daily', 'manual'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'dcn_stores_status_check') THEN
        ALTER TABLE dcn_stores ADD CONSTRAINT dcn_stores_status_check
            CHECK (status IN ('draft', 'granted', 'suspended', 'revoked', 'legacy'));
    END IF;
    -- Location coherence: a machine store names a box+root; a cabinet names a bucket.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'dcn_stores_machine_location_check') THEN
        ALTER TABLE dcn_stores ADD CONSTRAINT dcn_stores_machine_location_check
            CHECK (class <> 'machine' OR (server IS NOT NULL AND root IS NOT NULL));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'dcn_stores_cabinet_location_check') THEN
        ALTER TABLE dcn_stores ADD CONSTRAINT dcn_stores_cabinet_location_check
            CHECK (class <> 'cabinet' OR bucket IS NOT NULL);
    END IF;
    -- DoR §7: 'delete' is not in the mask vocabulary, for any store class. The
    -- schema refuses the word — there is nothing downstream to bypass.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'dcn_stores_mask_no_delete_check') THEN
        ALTER TABLE dcn_stores ADD CONSTRAINT dcn_stores_mask_no_delete_check
            CHECK (NOT (mask ? 'delete'));
    END IF;
END$$;

-- One store per (entity, class, physical location). COALESCE to '' so NULLs
-- don't defeat uniqueness (same technique as uq_context_backend_locations_phys).
CREATE UNIQUE INDEX IF NOT EXISTS uq_dcn_stores_location
    ON dcn_stores (entity_id, class,
                   COALESCE(server, ''), COALESCE(root, ''),
                   COALESCE(bucket, ''), COALESCE(prefix, ''), COALESCE(endpoint, ''));
CREATE INDEX IF NOT EXISTS idx_dcn_stores_entity ON dcn_stores (entity_id, status);

-- ── dcn_files — one row per indexed file/object; the tombstone lives here ────
CREATE TABLE IF NOT EXISTS dcn_files (
    id            BIGSERIAL   PRIMARY KEY,
    store_id      BIGINT      NOT NULL REFERENCES dcn_stores(id) ON DELETE CASCADE,
    path          TEXT        NOT NULL,   -- relative to store root / key under prefix
    size_bytes    BIGINT      NOT NULL DEFAULT 0,
    mtime         TIMESTAMPTZ,
    content_hash  TEXT,                   -- move/rename re-resolution key (DoR §11.3)
    missing_since TIMESTAMPTZ,            -- tombstone (DoR §11.2); NULL = live
    last_seen     TIMESTAMPTZ,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (store_id, path)
);
CREATE INDEX IF NOT EXISTS idx_dcn_files_hash ON dcn_files (store_id, content_hash);
-- The per-store missing digest (DoR §11.5) reads this without scanning live rows.
CREATE INDEX IF NOT EXISTS idx_dcn_files_missing
    ON dcn_files (store_id) WHERE missing_since IS NOT NULL;

-- ── dcn_chunks — locator ranges ONLY; structurally no content column ─────────
CREATE TABLE IF NOT EXISTS dcn_chunks (
    id            BIGSERIAL PRIMARY KEY,
    file_id       BIGINT    NOT NULL REFERENCES dcn_files(id) ON DELETE CASCADE,
    seq           INTEGER   NOT NULL,
    start_line    INTEGER,
    end_line      INTEGER,
    start_byte    BIGINT,
    end_byte      BIGINT,
    content_hash  TEXT,
    -- NO preview/content/snippet column — see header. Vector column lands at CN-2.
    UNIQUE (file_id, seq)
);

-- ── dcn_chunk_previews — cabinet/platform tiers only, trigger-enforced ───────
CREATE TABLE IF NOT EXISTS dcn_chunk_previews (
    chunk_id  BIGINT PRIMARY KEY REFERENCES dcn_chunks(id) ON DELETE CASCADE,
    preview   TEXT   NOT NULL
);

CREATE OR REPLACE FUNCTION dcn_preview_tier_guard() RETURNS trigger AS $$
DECLARE
    store_class TEXT;
BEGIN
    SELECT s.class INTO store_class
    FROM dcn_chunks c
    JOIN dcn_files f  ON f.id = c.file_id
    JOIN dcn_stores s ON s.id = f.store_id
    WHERE c.id = NEW.chunk_id;
    IF store_class = 'machine' THEN
        RAISE EXCEPTION
            'DCN tier honesty (DoR #2 §8): machine-class stores are locator-only — '
            'previews are structurally refused (chunk_id=%)', NEW.chunk_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_dcn_preview_tier_guard') THEN
        CREATE TRIGGER trg_dcn_preview_tier_guard
            BEFORE INSERT OR UPDATE ON dcn_chunk_previews
            FOR EACH ROW EXECUTE FUNCTION dcn_preview_tier_guard();
    END IF;
END$$;

COMMIT;
