-- v011: projects.design_system_slug
--
-- Adds an optional reference from a Software Factory project to a
-- design system installed via the catalog (kind='design_system', see
-- v009 + v010). When set, the orchestrator's step-prompt builder
-- injects the system's DESIGN.md into each build step's system prompt.
--
-- TEXT (nullable). No FK to catalog_entries — the slug is a stable
-- identifier; renames are vanishingly rare for OpenDesign systems and
-- a FK would complicate uninstall.

ALTER TABLE projects
    ADD COLUMN IF NOT EXISTS design_system_slug TEXT;

CREATE INDEX IF NOT EXISTS idx_projects_design_system_slug
    ON projects (design_system_slug) WHERE design_system_slug IS NOT NULL;
