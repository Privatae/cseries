-- v178 (2026-09-24): one OPEN support card per platform ticket, enforced.
--
-- The operator Host's collect (runtime/support_collect.py) raises one Circles
-- card per support ticket another Host sent the platform. The standing op and
-- a "look now" call can both read "no open card" and both insert; the
-- service's read-then-insert cannot close that, this index does — the same
-- shape as v177 for phase-gate cards. The loser's insert fails with a unique
-- violation and the pass skips the ticket.
--
-- Partial on purpose: decided cards for the same platform ticket are history
-- and accumulate (a reply from the user raises a fresh card).
CREATE UNIQUE INDEX IF NOT EXISTS uq_entity_tickets_open_support
    ON entity_tickets (entity_id, (metadata->>'platform_ticket_id'))
    WHERE status = 'open' AND metadata->>'kind' = 'support';
