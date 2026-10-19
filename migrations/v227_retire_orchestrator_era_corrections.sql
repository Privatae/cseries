-- v227 (2026-10-13): five learned rules from the orchestrator era are switched
-- off on every Host.
--
-- The Host calls an agent only for untrusted work outside
-- the platform; everything in the platform it does itself, code included.
-- These rules say the opposite. Training Memory distilled them from the
-- operator's own coding sessions while the Host was still an orchestrator
-- that dispatched the coding; they carry no origin and role 'coding'. Each
-- phrase below matched exactly one active row on the Host it was read on
-- (ids are per-Host, so the text is what is matched):
--
--   "...Entities facilitate and coordinate development processes, they
--         do not write code themselves."
--   "...immediately offer to escalate to another agent or provide an
--         alternative path forward..."
--   "...Do not continue with planned dispatches or changes until
--         restoration is complete and confirmed."
--   "...For coding agents, specify only role-based instructions and
--         semantic retrieval parameters..."
--   "...Do not conflate orchestrator design constraints with entity
--         response quality..."
--
-- They are gentle rules and reach a turn only through the similarity lane
-- (constitution/context.py), which today matches nothing there because
-- those rules carry no vector at the Host's embedding size. A backfill of those
-- vectors, or a switch to a model of the size they do carry, would bring them
-- back; so they go first.
--
-- Switched off, not deleted: a rule the person taught stays on record, and
-- `active` is outside the row's tamper digest (behavioral_correction_digest),
-- so the rest of the row still verifies. Only rows with no origin are
-- touched, and each phrase is long enough that a rule the person wrote in
-- other words does not match. Running it again changes nothing.

UPDATE behavioral_corrections
   SET active = false, updated_at = NOW()
 WHERE active
   AND origin IS NULL
   AND (correction ILIKE '%orchestration of software factories%they do not write code themselves%'
        OR correction ILIKE '%immediately offer to escalate to another agent%'
        OR correction ILIKE '%Do not continue with planned dispatches or changes until restoration%'
        OR correction ILIKE '%For coding agents, specify only role-based instructions%'
        OR correction ILIKE '%Do not conflate orchestrator design constraints with entity response quality%');
