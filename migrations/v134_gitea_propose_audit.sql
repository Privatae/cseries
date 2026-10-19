-- v134: audit of gitea op='propose' — the host's fork-and-PR lane
--
-- Numbered 134, not 133: feat/pairing-grants claims v133_pairing_grants.sql.
--
-- One row per call to POST /api/gitea/propose (server/routes/gitea_propose.py),
-- including refusals, the way nac_mgmt_audit records every management-lane
-- op. `files` (the JSON list of repo paths) and `commit` (the sha pushed to
-- the fork) are what make a proposal reconstructable after its fork branch
-- is deleted — the PR body alone does not say which files it carried.
-- Not operator_audit_log: that table is gateway-fed ("entity never writes",
-- init-db.sql) with a NOT NULL operator_id the pod does not hold.
CREATE TABLE IF NOT EXISTS gitea_propose_audit (
  id          BIGSERIAL     PRIMARY KEY,
  entity_id   INTEGER,
  op          TEXT          NOT NULL DEFAULT 'gitea:propose',
  repo        TEXT,
  fork        TEXT,
  branch      TEXT,
  pr_number   INTEGER,
  pr_url      TEXT,
  files       JSONB         NOT NULL DEFAULT '[]'::jsonb,
  file_count  INTEGER       NOT NULL DEFAULT 0,
  commit      TEXT,
  outcome     TEXT          NOT NULL,          -- opened | updated | refused | error
  detail      TEXT,
  at          TIMESTAMPTZ   NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_gitea_propose_audit_repo
  ON gitea_propose_audit (repo, at DESC);
CREATE INDEX IF NOT EXISTS idx_gitea_propose_audit_recent
  ON gitea_propose_audit (at DESC);
