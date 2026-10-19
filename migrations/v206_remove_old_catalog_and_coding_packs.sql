-- v206: the old asset catalog and the three coding-agent packs
-- come out of the Host.
--
-- The design system was replaced by the Host's own desktop UI and a platform
-- library users can submit to and share from, so the old open-design catalog
-- goes. The coding packs are skills the Host never needs or uses. Designs made
-- with the retired "design from a website" feature go with the catalog.
--
-- The migrations that put this in (v004, v005, v007, v008, v009, v010, v011,
-- v012, v024) stay as they are: shipped migrations are never edited
-- (migrations/README.md). This one undoes their effect, on every Host and on a
-- fresh one alike (a fresh Host runs them all, then this).
--
-- 1. The coding packs' hard rules. v004 seeded 'external:karpathy', v005
--    'external:superpowers', v008 'external:ecc'. A hard rule reaches any turn
--    whose keywords match it (constitution/context.py), so these were steering
--    Host turns. No other writer uses these origins.
-- 2. The coding packs' skill files. v007 (seven Superpowers files) and v008
--    (skills/deep_research.md) wrote each with an 'Origin: external:
--    superpowers/<slug>' or 'Origin: external:ecc/<slug>' line in its body;
--    that line is what is matched, so a file the Host or the person wrote at
--    the same path (the seeds were ON CONFLICT DO NOTHING) stays. No other
--    skills/ row is touched: step A moves the rest into host_skills.
-- 3. Every file a catalog install wrote into the context store, and only
--    those. In every version of server/routes/catalog.py (removed here) an
--    install wrote factory-assets/<kind>s/<slug>/ plus a fixed file list per
--    kind: DESIGN.md, tokens.css and components.html for a design system,
--    SKILL.md for an artifact skill. Uninstall deleted exactly those files and
--    left anything else under factory-assets/ alone, because the Host or the
--    person could write there too (the context tool's own example path was
--    one once). This keeps that contract: a file of theirs under
--    factory-assets/ stays.
-- 4. The catalog itself: every catalog_entries row (both kinds, every origin,
--    the url-import designs included) and every catalog_installs row, by
--    dropping both tables. Nothing reads them after this change.
-- 5. projects.design_system_slug (v011) and its index. No reader is left;
--    projects that named an OpenDesign system follow the Host's own desktop.

DELETE FROM behavioral_corrections
 WHERE origin IN ('external:superpowers', 'external:karpathy', 'external:ecc');

DELETE FROM entity_context
 WHERE file_path LIKE 'skills/%'
   AND (content LIKE '%Origin: external:superpowers/%'
        OR content LIKE '%Origin: external:ecc/%');

DELETE FROM entity_context
 WHERE file_path ~ '^factory-assets/design_systems/[^/]+/(DESIGN\.md|tokens\.css|components\.html)$'
    OR file_path ~ '^factory-assets/artifact_skills/[^/]+/SKILL\.md$';

DROP TABLE IF EXISTS catalog_installs;
DROP TABLE IF EXISTS catalog_entries;

DROP INDEX IF EXISTS idx_projects_design_system_slug;
ALTER TABLE projects DROP COLUMN IF EXISTS design_system_slug;
