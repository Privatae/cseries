-- v158 — model_route_log: every route the routing table has held, and who
-- set it (2026-09-18).
--
-- WHY. A host that can rewrite its own routing table is
-- choosing its own successor, and until now nothing structural stood between
-- the host's `model_routing op='set'` and the table — only a sentence in a
-- skill ("get the user's agreement"). The rule the platform already applies
-- to network grants — widening needs approval, tightening is free — now
-- applies here: the host may route a role that has NO route (nothing runs
-- there to break) and may REVERT a role to a route the user approved before;
-- moving a working role anywhere else becomes a decision the user resolves
-- (server/decisions_service.py, `route_change_proposal`). The user's own
-- writes in the Orchestrator are unchanged.
--
-- The "revert is free" half needs a record of what was approved. The routing
-- table itself keeps only the current entry per role (entity_registry.
-- behavioral_params.model_routing), so this ledger is the memory of every
-- entry it has held: one row per apply, with `set_by` saying whose hand it
-- was — 'user' (the Orchestrator's PUT), 'decision' (a proposal the user
-- approved), 'host' (an unrouted role the host filled), 'host_revert' (the
-- host reverting to a previously approved route). Only 'user' and
-- 'decision' rows count as approved for the revert rule; the host cannot
-- launder a route into "approved" by writing it itself.
--
-- Append-only. Nothing updates or deletes a row; the current route is the
-- routing table's business, this is its history.

CREATE TABLE IF NOT EXISTS model_route_log (
    id                BIGSERIAL    PRIMARY KEY,
    at                TIMESTAMPTZ  NOT NULL DEFAULT now(),
    entity_id         INTEGER      NOT NULL,
    role              TEXT         NOT NULL,
    provider          TEXT         NOT NULL,
    model             TEXT         NOT NULL,
    effort            TEXT,
    fallback_provider TEXT,
    fallback_model    TEXT,
    -- 'canary' (runtime/route_canary.py): the platform reverted the role to
    -- the previous approved route after an invented or off-list tool name
    -- inside the window following a switch. Not approved, and it starts no
    -- canary of its own.
    set_by            TEXT         NOT NULL
                          CHECK (set_by IN ('user', 'decision', 'host', 'host_revert', 'canary')),
    decision_id       BIGINT,
    -- On a 'canary' row: the (provider, model) the canary reverted FROM —
    -- recorded explicitly so the rejection is never inferred from which row
    -- happens to sit next to it (a concurrent user apply could interleave).
    -- NULL on every other row.
    rejected_provider TEXT,
    rejected_model    TEXT
);

-- The revert rule's one read: "has this (role, provider, model) ever been
-- approved on this entity" — newest first.
CREATE INDEX IF NOT EXISTS idx_model_route_log_role_at
    ON model_route_log (entity_id, role, at DESC);

DO $$
BEGIN
    IF to_regclass('public.model_route_log') IS NOT NULL THEN
        COMMENT ON TABLE model_route_log IS
            'Append-only history of every route the routing table has held, '
            'with set_by = user | decision | host | host_revert. Written by '
            'server/routes/inference.apply_role_route; read by the host''s '
            'model_routing tool to decide whether a set is a free revert or '
            'a proposal the user must approve.';
        COMMENT ON COLUMN model_route_log.set_by IS
            'Whose hand: user (Orchestrator PUT), decision (an approved '
            'route_change_proposal), host (the host filled an unrouted role), '
            'host_revert (the host reverted to a previously approved route), '
            'canary (the platform reverted after a disqualifying tool call '
            'inside the window following a switch). Only user and decision '
            'rows count as approved.';
    END IF;
END $$;
