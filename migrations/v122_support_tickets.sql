-- v122_support_tickets.sql — Tickets: the platform-support lane (2026-08-29)
--
-- The Circles "Reviews" tab becomes "Tickets": one queue with two kinds.
--   * decisions — entity_tickets, unchanged: the host asks the USER to decide.
--   * support   — this table: the host (or the user, through the host) asks the
--                 PLATFORM for help — a bug, a question, a feature, billing,
--                 access. Categorized, identified by project.
--
-- The privacy shape is the FreeAgent one: the host assembles a context blob
-- (versions, installed apps, recent inference errors, redacted excerpts of the
-- files it names) into `shared_context`, the user sees EXACTLY that blob in the
-- Tickets pane and approves, trims, or discards it, and only the approved copy
-- crosses the bridge (platform.ticket op='open'). Until approval nothing leaves
-- the pod; this table lives in the entity's own Postgres.
--
-- Replaces the trusted-reporter debug lane (v093: debug_reporters/bug_reports,
-- report_bug tool). That lane was an operator allowlist of two hand-picked
-- entities and never received a report. This one is open to every host,
-- rate-limited at the bridge, and the platform side mirrors into the
-- operator's own ledger so the operator answers it on the same surface as
-- everything else.

CREATE TABLE IF NOT EXISTS support_tickets (
    id              SERIAL      PRIMARY KEY,
    ticket_uuid     UUID        NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    entity_id       INTEGER     NOT NULL,

    category        TEXT        NOT NULL
                    CHECK (category IN ('bug', 'question', 'feature', 'billing', 'access')),
    -- Identified by project: a Workbench project, or NULL = the Host itself.
    project_id      INTEGER,
    project_name    TEXT        NOT NULL DEFAULT '',
    title           TEXT        NOT NULL,
    severity        TEXT        NOT NULL DEFAULT 'normal'
                    CHECK (severity IN ('low', 'normal', 'high')),

    -- The host's own card, in the same four-part shape the operator reads:
    -- problem / context / recommendation / pushback. Plain English.
    problem         TEXT        NOT NULL DEFAULT '',
    context         TEXT        NOT NULL DEFAULT '',
    recommendation  TEXT        NOT NULL DEFAULT '',
    pushback        TEXT        NOT NULL DEFAULT '',
    repro_steps     TEXT        NOT NULL DEFAULT '',
    expected        TEXT        NOT NULL DEFAULT '',
    actual          TEXT        NOT NULL DEFAULT '',

    -- The context blob the user is asked to approve. Items are
    -- {key, label, kind, content, included} — the pane toggles `included`;
    -- only included items are sent. Redacted at assembly (secret_shapes).
    shared_context  JSONB       NOT NULL DEFAULT '[]'::jsonb,

    -- Local lifecycle (the user's side):
    --   draft      — assembled, waiting for the user to approve what is shared
    --   sent       — crossed the bridge; platform_status tracks the far side
    --   discarded  — the user dropped it before sending
    --   closed     — resolved on the platform side (fixed/rejected) or by the user
    status          TEXT        NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft', 'sent', 'discarded', 'closed')),

    -- Platform side, mirrored on every list/get:
    --   received -> reproducing -> pr_open -> approved -> fixed
    --   needs_info (a question the user answers by reply) | rejected
    platform_id     BIGINT,
    platform_status TEXT        NOT NULL DEFAULT '',
    platform_ref    TEXT        NOT NULL DEFAULT '',   -- the operator's ticket ref, informational
    resolution      TEXT        NOT NULL DEFAULT '',
    pr_url          TEXT        NOT NULL DEFAULT '',

    -- Conversation: [{author: 'host'|'user'|'platform', body, at}]
    thread          JSONB       NOT NULL DEFAULT '[]'::jsonb,

    opened_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sent_at         TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at       TIMESTAMPTZ,
    metadata        JSONB       NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_support_tickets_entity_status
    ON support_tickets (entity_id, status, opened_at DESC);
CREATE INDEX IF NOT EXISTS idx_support_tickets_platform
    ON support_tickets (platform_id) WHERE platform_id IS NOT NULL;

COMMENT ON TABLE support_tickets IS
    'Platform-support tickets the host raises for the user. Nothing leaves the pod until the user approves shared_context; the approved copy is sent over the bridge (platform.ticket).';
