-- v164 — a containment order records what became of it.
--
-- nac_agent_contain (v065) is the queue for "terminate this agent": the host
-- writes a (token_id, pid) row, the bastion's next heartbeat carries it, and
-- the reconcile in topology.upsert_heartbeat marks it cleared when the pid no
-- longer appears in the beat's agent list. Two things that queue could not
-- say, and now must:
--
--   signature — the signature the agent wore when the order was placed. A pid
--     is a number the OS reuses; without this the reconcile cannot tell the
--     process the user pointed at from a later one holding the same number.
--     NULL when the activity row had none; the reconcile then relies on the
--     age bound alone.
--
--   outcome — why the row was cleared: 'done' (the box looked and the agent
--     is gone), 'expired' (not carried out within topology.CONTAIN_PENDING_TTL
--     — whether the box could not see, or was sent the order on every beat
--     and the agent is still there; a pid an hour old is a stale target
--     either way), 'pid_reused' (the pid now belongs
--     to a different signature, so the order was dropped rather than
--     delivered). NULL while pending, and on every row cleared before this
--     migration.
--
--   'superseded' is written once, here: the old INSERT had no guard, so a
--     pid clicked twice holds two open rows. The older duplicates are closed
--     so the partial unique index below can exist; the newest stays open.
--
-- One open order per (token_id, pid) is then a property of the table, not of
-- the INSERT that hopes for it: request_contain inserts ON CONFLICT on this
-- index and reports "open" when nothing was added.
ALTER TABLE nac_agent_contain ADD COLUMN IF NOT EXISTS signature TEXT;
ALTER TABLE nac_agent_contain ADD COLUMN IF NOT EXISTS outcome   TEXT;

UPDATE nac_agent_contain c
   SET cleared_at = now(), outcome = 'superseded'
 WHERE c.cleared_at IS NULL
   AND EXISTS (SELECT 1 FROM nac_agent_contain d
                WHERE d.token_id = c.token_id AND d.pid = c.pid
                  AND d.cleared_at IS NULL AND d.id > c.id);

CREATE UNIQUE INDEX IF NOT EXISTS nac_agent_contain_open_pid
    ON nac_agent_contain (token_id, pid) WHERE cleared_at IS NULL;
