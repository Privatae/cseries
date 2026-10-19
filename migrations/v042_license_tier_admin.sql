-- v042 (2026-05-25): license_tier vocabulary refresh.
--
-- The existing CHECK constraint allowed
--   ('free','personal','professional','enterprise',
--    'veteran_free','social_benefit','standard','builder')
-- which is a hybrid of original product tiers + ad-hoc additions
-- and doesn't match what the runtime actually gates on.
--
-- The new canonical vocab (mastercode/license_tiers.py LICENSE_TIERS):
--   standard, pro, team, enterprise, cloud, air-gap, admin
--
-- We keep the legacy values in the constraint allowlist so any row
-- carrying one of them doesn't break — sunsetting them is a separate,
-- audited migration once we know nothing reads those values.

BEGIN;

ALTER TABLE entity_registry DROP CONSTRAINT IF EXISTS entity_registry_license_tier_check;

ALTER TABLE entity_registry ADD CONSTRAINT entity_registry_license_tier_check CHECK (
    license_tier IN (
        -- New canonical vocabulary.
        'standard',
        'pro',
        'team',
        'enterprise',
        'cloud',
        'air-gap',
        'admin',
        -- Legacy values preserved for back-compat. Sunset separately.
        'free',
        'personal',
        'professional',
        'veteran_free',
        'social_benefit',
        'builder',
        'network'
    )
);

COMMIT;
