-- v182 — the mirror list on a service reach grant (bastion 0.38.0 service_mirror.go).
--
-- A slot server has no network, so the repos the person asks it to mirror are
-- fetched by THEIR machine, over their own connection, and pushed into it. What
-- may be fetched is part of the reach grant the person approves: an array of
-- {url, repo} — an https upstream and the server repo it lands in. Empty = none.

BEGIN;

ALTER TABLE service_reach_grants ADD COLUMN IF NOT EXISTS mirrors JSONB NOT NULL DEFAULT '[]'::jsonb;

COMMIT;
