-- v125_mgmt_grant_deny_intent.sql — plain-text management grants
--
-- Permission prompts get in the way. NAC management grants should let a user
-- say "do xyz" and "do NOT do xyz" in plain text, converted into machine
-- permissions.
--
-- Until now a grant was an allow list only ({server, ops}). "Do NOT" had no
-- home, so the only way to forbid something was to never grant it — and a
-- broad allow in one grant could not be narrowed by another. This adds:
--
--   deny    JSONB  the tokens the host may NOT run on the server, whatever any
--                  other grant allows. Deny beats allow: the gate consults every
--                  approved grant's deny list for the server BEFORE any allow
--                  (server/network/nac_mgmt.py mgmt_denied_by / mgmt_decision /
--                  harness_image_allowed). A token is an op id, an allowlist
--                  token ("image:<ref>", "mode:lab"), or '*' (everything).
--   intent  JSONB  {want, do_not_want} — the user's own sentences, verbatim.
--                  The Network app reads them back on the approval card, and a
--                  refusal quotes do_not_want so the host says WHO said no.
--
-- Both ride inside the SIGNED terms on approval ({entity, server, ops, deny,
-- intent, expires}); the gate trusts the signature, never these columns. A
-- grant signed before this migration has no deny term and behaves as before.
-- A deny-only grant (empty ops) is a tighten and approves without a step-up,
-- the same rule as an egress deny.
--
-- The runtime also adds these columns defensively (nac_mgmt.ensure_grants_table)
-- so a pod mid-migration never 500s; this file is the authoritative record.

ALTER TABLE nac_mgmt_grants
    ADD COLUMN IF NOT EXISTS deny   JSONB NOT NULL DEFAULT '[]',
    ADD COLUMN IF NOT EXISTS intent JSONB;
