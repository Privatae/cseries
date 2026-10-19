-- Ship 12 (2026-05-24) — Invariant 19 extension to codebook_registry.
--
-- Ship 10 wired SHA256 directive_hash validation on entity_registry
-- via handle_synaptive_identity. Ship 12 extends the same shape to
-- codebook_registry — the canonical codebook definitions. The table
-- is functionally read-only at runtime (writes happen only via
-- migration v028 seeds), so this ship adds:
--
--   1. codebook_digest VARCHAR(64) column,
--   2. Backfills the digest for every existing row using a stable
--      canonical JSON shape (sorted keys, no whitespace) over the
--      semantic fields — mutable timestamps + status are excluded.
--
-- The codebook_engine.load() read path (separate file in this ship)
-- recomputes the digest on load, compares to the stored value, and
-- denies-skip on mismatch with an audit row invariant19:codebook.

DO $$
BEGIN
    -- Idempotent column add — re-runs safely on already-migrated DBs.
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'codebook_registry'
           AND column_name = 'codebook_digest'
    ) THEN
        ALTER TABLE codebook_registry
            ADD COLUMN codebook_digest VARCHAR(64);
    END IF;
END $$;

-- Backfill the digest for every row that doesn't have one yet.
-- Stable canonical JSON: jsonb_build_object with keys in a fixed
-- order. Postgres jsonb already normalizes whitespace and sorts top-
-- level keys lexically, so the bytes are deterministic. encode(digest()
-- ,'hex') yields a 64-character lowercase hex string identical to
-- Python's hashlib.sha256().hexdigest() over the same input.
UPDATE codebook_registry
   SET codebook_digest = encode(
       digest(
           jsonb_build_object(
               'codebook_name',          codebook_name,
               'brain_region',           brain_region,
               'brain_system',           brain_system,
               'hierarchy_level',        hierarchy_level,
               'processing_type',        processing_type,
               'processing_rules',       COALESCE(processing_rules, '{}'::jsonb),
               'governed_params',        COALESCE(governed_params, '{}'::jsonb),
               'neurotransmitter_profile', neurotransmitter_profile,
               'depends_on',             COALESCE(to_jsonb(depends_on), '[]'::jsonb),
               'feeds_into',             COALESCE(to_jsonb(feeds_into), '[]'::jsonb),
               'version',                version
           )::text,
           'sha256'
       ),
       'hex'
   )
 WHERE codebook_digest IS NULL;
