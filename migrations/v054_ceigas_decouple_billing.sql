-- CEIGAS billing decouple (Phase C): the cross-domain authorization default used to
-- read entity_registry.license_tier (a BILLING value) via domain_registry.tier_access_
-- defaults. That wired billing into security. We make it grants-only: before the code
-- stops consulting the tier-default, materialize whatever access the tier-default
-- currently grants into EXPLICIT entity_domain_access grants — so effective access is
-- unchanged (lossless), and "tier" no longer touches the security path.
--
-- Additive + idempotent: NOT EXISTS guard never overrides an existing grant and never
-- reduces access; existence-guarded so a pod missing these tables (fresh boot order)
-- no-ops. Runs in every pod at boot, before the grants-only controller serves.
-- (Verified no-op for the current live fleet — pods see no domain relying on the
-- tier-default — but this keeps any pod/provision lossless.)

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'entity_domain_access')
       AND EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'domain_registry')
       AND EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'entity_registry') THEN
        INSERT INTO entity_domain_access (entity_id, domain_id, access_level)
        SELECT er.id, dr.id, dr.tier_access_defaults->>er.license_tier
        FROM entity_registry er
        CROSS JOIN domain_registry dr
        WHERE er.license_tier IS NOT NULL
          AND dr.tier_access_defaults->>er.license_tier IS NOT NULL
          AND dr.tier_access_defaults->>er.license_tier <> 'none'
          AND NOT EXISTS (SELECT 1 FROM entity_domain_access eda
                          WHERE eda.entity_id = er.id AND eda.domain_id = dr.id);
    END IF;

    -- dashboard_tier: a duplicate of the billing plan with ZERO readers anywhere.
    -- Safe to drop now (no reader, old or new). license_tier itself is dropped in a
    -- later contract migration, AFTER every pod runs the grants-only code that no
    -- longer reads it (expand-contract; a live pre-roll pod still reads license_tier).
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'entity_registry' AND column_name = 'dashboard_tier') THEN
        ALTER TABLE entity_registry DROP COLUMN dashboard_tier;
    END IF;
END $$;
