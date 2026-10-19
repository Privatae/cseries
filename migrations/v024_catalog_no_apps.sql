-- Migration v024: forbid kind='app'/'application' in catalog_entries.
--
-- Apps and the generic catalog have separate lifecycles, separate tables,
-- and separate publishers. Apps live in `app_store_apps` / `app_store_versions`
-- (central, platform PG) and `installed_applications` (per-entity, this DB),
-- with their own signing trust store and tier model. catalog_entries is for
-- non-app kinds — design_system, artifact_skill, future skill_pack, codebook,
-- etc. Distribution path for non-apps is asset bytes via the bridge into
-- ContextStore; for apps it's signed .cseries bundles into the filesystem.
--
-- Historically those concerns drifted toward overlap (catalog_installs +
-- installed_applications both modeling "what's installed"), which made
-- uninstall paths race. Locking the boundary at the database is the
-- structural fix of the app-store cleanup. After this migration:
--
--   * /api/applications is the only surface for app install/uninstall.
--   * /api/catalog/* handles the other kinds only.
--   * uninstall can never leak between the two.
--
-- Pre-flight: count of kind IN ('app','application') was 0 on every live
-- entity PG before this landed. The constraint would abort otherwise.
-- Idempotent — DROP IF EXISTS before ADD.

BEGIN;

ALTER TABLE catalog_entries
    DROP CONSTRAINT IF EXISTS catalog_entries_no_app_kind;

ALTER TABLE catalog_entries
    ADD CONSTRAINT catalog_entries_no_app_kind
    CHECK (kind NOT IN ('app', 'application'));

COMMIT;
