-- v173: a project's place in its bucket.
--
-- The rail's buckets are the v130 folders with the status groups removed.
-- The user orders projects inside a bucket by dragging them; this is where
-- that order is kept. NULL = never placed by hand: such a project sorts after
-- the placed ones, newest activity first, which is the order the rail had
-- before anyone dragged anything. Last activity stays the rail's alternative
-- sort, so nothing here replaces it.
--
-- Written only by PATCH /api/projects/{id}/place, which renumbers the whole
-- target bucket 1..n in one transaction — a drop is one request, and the
-- numbers never need a midpoint.
--
-- Declared for fresh entities in init-db.sql (the projects block); idempotent
-- here for the long-lived ones.

ALTER TABLE projects ADD COLUMN IF NOT EXISTS sort_order INTEGER;
