-- v177 (2026-09-24): one OPEN phase-gate ticket per phase, enforced.
--
-- A phase gated on the user's approval raises its ticket in Circles
-- (server/routes/tickets.py raise_phase_gate_ticket) from three call sites:
-- the build-door refusal and both idle explanations. Two of them can run at
-- the same moment (two parallel phases landing), both read "no open card",
-- and both insert. The service's read-then-insert cannot close that; this
-- index does. The loser's insert fails with a unique violation and it adopts
-- the winner's row.
--
-- Partial on purpose: decided/dismissed cards for the same phase are history
-- and may accumulate (a phase sent back and re-asked after a rewrite).
CREATE UNIQUE INDEX IF NOT EXISTS uq_entity_tickets_open_phase_gate
    ON entity_tickets (entity_id, (metadata->>'phase_id'))
    WHERE status = 'open' AND metadata->>'kind' = 'phase_gate';
