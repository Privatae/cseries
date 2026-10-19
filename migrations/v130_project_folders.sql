-- v130 (2026-09-07): project FOLDERS — the grouping layer above status.
--
-- The Workbench rail was one flat list with an Archived fold. A user running a
-- few hundred projects as customized trackers has no way to group them, and
-- `status` cannot be that grouping: a folder must hold archived and active
-- projects TOGETHER (the one hard requirement), so grouping and lifecycle are
-- two orthogonal axes — folder_id here, status where it always was.
--
-- The model, chosen for that user and for a viewer that must filter by folder
-- without becoming a tree widget:
--   - a project sits in exactly ONE folder (projects.folder_id) or in none;
--   - folders are FLAT — no parent_id, no path. One level is what a rail can
--     render collapsed and a filter can offer as a single pick; nesting can be
--     added later with a parent_id without changing this shape;
--   - "unfiled" is folder_id IS NULL, never a reserved folder row, so a fresh
--     entity has zero folders and nothing to seed;
--   - deleting a folder NEVER deletes a project: ON DELETE SET NULL sends its
--     projects back to unfiled.
-- Names are unique per entity case-insensitively so "Clients" and "clients"
-- cannot both exist in one rail.
--
-- Declared for fresh entities in init-db.sql (same block as projects);
-- idempotent here for the long-lived ones.

CREATE TABLE IF NOT EXISTS project_folders (
    id         SERIAL PRIMARY KEY,
    entity_id  INTEGER NOT NULL,
    name       TEXT NOT NULL,
    color      TEXT,                         -- optional swatch for the rail/viewer
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_project_folders_entity_name
    ON project_folders (entity_id, lower(name));

ALTER TABLE projects ADD COLUMN IF NOT EXISTS folder_id INTEGER
    REFERENCES project_folders(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_projects_folder ON projects (entity_id, folder_id);
