-- v204 (2026-10-05): the conversation a connector pairing was started in, and
-- the thread or project each chat app last had open.
--
-- 1. nac_connector_grants.asked_in — the room the Host's turn was in when it
--    proposed the pairing (propose_nac_policy kind=connector): the same shape
--    escalate writes on a ticket, {app_id, project_id, thread_id,
--    interaction_context}. The approval ticket is filed later, from the
--    connector pod's POST /api/connector/pair, outside any turn, so the grant
--    is the only place that still knows which conversation asked. The ticket
--    copies it into metadata.asked_in, so the card comes up in that chat and
--    the person's answer starts a turn there. NULL on a grant proposed before
--    this column existed, or outside a turn.
--
-- 2. ui_last_open — where Circles and Workbench reopen. One row per surface
--    on this Host, written on every thread or project change and read once at
--    boot, so the person lands where they left off on any device instead of
--    on an old conversation thread. A link (#view=thread: / #view=project:) still wins
--    over it, in the client. The read checks the ref still exists
--    (server/ui_last_open.py), so a deleted thread or project reads as none.

ALTER TABLE nac_connector_grants ADD COLUMN IF NOT EXISTS asked_in JSONB;

CREATE TABLE IF NOT EXISTS ui_last_open (
    surface     TEXT PRIMARY KEY
                    CHECK (surface IN ('circles', 'workbench')),
    kind        TEXT NOT NULL
                    CHECK (kind IN ('thread', 'project')),
    ref         TEXT NOT NULL CHECK (length(ref) BETWEEN 1 AND 64),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Circles opens threads, Workbench opens projects.
    CONSTRAINT ui_last_open_kind_fits_surface CHECK (
        (surface = 'circles' AND kind = 'thread')
        OR (surface = 'workbench' AND kind = 'project'))
);
