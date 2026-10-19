-- v172 — stop a program the bastion's delta reported, named by fingerprint.
--
-- nac_agent_contain (v065, v164) can only name a pid the agent plane
-- recognised. The delta reports every process whose fingerprint is
-- not in the machine's accounting, and before this a proc_new finding could be
-- read on the host and not stopped from it. This is the design's first kill
-- rung: a finding may be contained if
-- it is a finding and not a baseline entry.
--
-- The order names a FINGERPRINT, never a pid. The box resolves it to whatever
-- is running under it at the moment the order arrives, from a fresh comparison
-- (privatae-bastion containfinding.go), so a pid reused since the host last
-- looked can never be the target.
--
-- The box answers every order on the beat under `contain_results`, echoing the
-- order's id; topology.upsert_heartbeat clears the row with the box's own
-- outcome word and how many processes the kill reached. An order the box never
-- answers (a bastion before 0.32.0 ignores the directive) lapses after
-- topology.CONTAIN_PENDING_TTL with outcome 'expired' — never read as done.
--
--   outcome — the box's word ('killed', 'not_a_finding', 'in_baseline',
--             'refused_app', 'refused_ours', 'no_authority', 'no_accounting',
--             'walk_failed', 'refused_anchor', 'kill_failed'), or 'expired'.
--             NULL while pending.
--   stopped — processes the kill reached, children included; 0 unless killed.
--
-- One open order per (token_id, fp): request_contain_finding inserts ON
-- CONFLICT on the partial unique index and reports "open" when nothing was
-- added.
CREATE TABLE IF NOT EXISTS nac_finding_contain (
    id           BIGSERIAL PRIMARY KEY,
    token_id     INTEGER     NOT NULL,
    fp           TEXT        NOT NULL,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    cleared_at   TIMESTAMPTZ,
    outcome      TEXT,
    stopped      INTEGER     NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS nac_finding_contain_open_fp
    ON nac_finding_contain (token_id, fp) WHERE cleared_at IS NULL;
