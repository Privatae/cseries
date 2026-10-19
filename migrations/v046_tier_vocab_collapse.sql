-- v046 (2026-05-28): tier vocabulary collapse to four values.
--
-- The platform has four tiers and only four.
--   standard   — every signup, every entity. Default.
--   pro        — reserved (future: cseries-image fork on platform network).
--                Nobody is on it today.
--   enterprise — reserved (future: custom deployments).
--                Nobody is on it today.
--   admin      — the platform operator's entities and platform-role
--                entities. The "network" / WireGuard tier
--                from the legacy vocabulary collapses into admin
--                because network = platform-internal = admin.
--
-- v042 widened the CHECK to keep legacy values (free, personal,
-- network, professional, veteran_free, social_benefit, builder, team,
-- cloud, air-gap). Every one of those is now dead. We migrate the rows
-- in this DB to the new 4-value vocabulary and tighten the CHECK.
--
-- Every existing row outside the new vocabulary becomes 'standard'.

BEGIN;

-- Stage 1: drop the existing wide CHECK (added by v042).
ALTER TABLE entity_registry
    DROP CONSTRAINT IF EXISTS entity_registry_license_tier_check;

-- Stage 2: collapse rows.
UPDATE entity_registry SET license_tier = 'standard'
 WHERE license_tier NOT IN ('standard', 'pro', 'enterprise', 'admin');

-- Stage 3: also collapse dashboard_tier so the two columns don't drift.
-- dashboard_tier had different historical values (builder, etc.) — same
-- collapse rule applies.
UPDATE entity_registry SET dashboard_tier = 'standard'
 WHERE dashboard_tier IS NOT NULL
   AND dashboard_tier NOT IN ('standard', 'pro', 'enterprise', 'admin');

-- Stage 4: lock the new CHECK. Four values, period.
ALTER TABLE entity_registry ADD CONSTRAINT entity_registry_license_tier_check CHECK (
    license_tier IN ('standard', 'pro', 'enterprise', 'admin')
);

COMMIT;
