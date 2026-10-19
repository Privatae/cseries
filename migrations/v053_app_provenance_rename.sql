-- App SDK: rename the app trust axis off the overloaded word "tier" -> "provenance",
-- and drop the dead "premium" value. After this, "tier" means ONLY the billing/
-- subscription plan; the app's trust class is "provenance" {native,standard,community,
-- custom}. "premium" was vestigial (nothing branched on it; paid-ness is price_micros),
-- so existing premium rows collapse to standard and the value is removed from the CHECK
-- and from any signing key's allowed set.
--
-- Idempotent + existence-guarded: on a fresh pod these tables don't exist at migration
-- time (the app's ensure_schema creates them later with the new names), so the whole
-- block no-ops; on an existing pod it renames in place. Safe to re-run.
-- Code reads `provenance` with a transitional `tier` fallback for not-yet-re-signed
-- community/custom bundles; this migration handles the per-pod tables.

DO $$
BEGIN
    -- installed_applications.tier -> provenance
    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_name = 'installed_applications') THEN

        IF EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name = 'installed_applications' AND column_name = 'tier') THEN
            -- collapse the dead 'premium' value before tightening the CHECK
            UPDATE installed_applications SET tier = 'standard' WHERE tier = 'premium';
            ALTER TABLE installed_applications RENAME COLUMN tier TO provenance;
        END IF;

        -- swap the value CHECK to the cleaned set (drop the old auto-named one; the
        -- constraint name does not follow a column rename)
        ALTER TABLE installed_applications
            DROP CONSTRAINT IF EXISTS installed_applications_tier_check;
        IF NOT EXISTS (SELECT 1 FROM pg_constraint
                       WHERE conname = 'installed_applications_provenance_check') THEN
            ALTER TABLE installed_applications
                ADD CONSTRAINT installed_applications_provenance_check
                CHECK (provenance IN ('native','standard','community','custom'));
        END IF;

        ALTER INDEX IF EXISTS idx_installed_applications_tier
            RENAME TO idx_installed_applications_provenance;
    END IF;

    -- application_signing_keys.tier_allowed -> provenance_allowed (+ drop premium)
    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_name = 'application_signing_keys') THEN

        IF EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name = 'application_signing_keys' AND column_name = 'tier_allowed') THEN
            ALTER TABLE application_signing_keys RENAME COLUMN tier_allowed TO provenance_allowed;
        END IF;

        UPDATE application_signing_keys
           SET provenance_allowed = array_remove(provenance_allowed, 'premium')
         WHERE 'premium' = ANY(provenance_allowed);
    END IF;
END $$;
