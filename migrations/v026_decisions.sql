-- Migration v026: decision substrate (Ship 4d-1).
--
-- The user/host collaboration model: long-running project plans → host
-- executes → hits an ambiguity it can't reason confidently about → host
-- emits a decision proposal → user receives an INBOX message of
-- kind='decision' → user picks → submission forwards as next-turn prompt
-- and host resumes.
--
-- Auto-decision is the loop closing (later ship). When pattern confidence
-- is high enough, host writes the row with decided_by='auto' or
-- 'pattern:<id>' and ALSO fires an inbox message of kind='decision_review'
-- so the user can confirm, refactor, rollback, or update the codebook.
-- This review path is the most scrutinized data on the platform — every
-- auto-decision MUST be reviewable after the fact.
--
-- Cross-host decisions piggyback inbox's existing cross-entity addressing
-- (cross_entity_id column). No parallel surface.
--
-- This migration also extends inbox_messages.message_kind to include
-- 'decision' and 'decision_review' so the new kinds pass the CHECK.

BEGIN;

CREATE TABLE IF NOT EXISTS decisions (
    id                  BIGSERIAL PRIMARY KEY,
    entity_id           INTEGER NOT NULL,
    session_id          TEXT,

    -- Problem framing — what the host needs the user (or future-self) to pick.
    proposed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    prompt              TEXT NOT NULL,
    -- options shape: [{key, label, rationale, tradeoffs}, ...]
    options             JSONB NOT NULL,
    -- context: anything that helps explain the situation (turn_refs[],
    -- failed_run_ids[], related_project_id, signal_snapshot, etc.)
    context             JSONB DEFAULT '{}'::jsonb NOT NULL,

    -- Resolution.
    decided_at          TIMESTAMPTZ,
    chosen_key          TEXT,
    chosen_rationale    TEXT,
    -- 'user' for human pick; 'auto' for unsupervised host pick;
    -- 'pattern:<pattern_id>' for accrued-pattern auto-pick (Ship 4d-2+).
    decided_by          TEXT,

    -- After-the-fact: what happened once the chosen option ran.
    -- Lets the review surface ("4 months later") render: problem +
    -- options + chosen + outcome → confirm/refactor/rollback/codebook.
    outcome             JSONB,

    -- Review of an auto-decision. Manual decisions don't need review.
    reviewed_at         TIMESTAMPTZ,
    review_verdict      TEXT,   -- 'confirmed'|'refactor'|'rollback'|'codebook_update'
    review_notes        TEXT,

    -- Pattern accrual (Ship 4d-2). NULL until clustering catches this row.
    pattern_id          INTEGER,

    -- Lineage. If a later decision rolls back an earlier one, the new
    -- row references the superseded id. Bi-temporal-style audit trail —
    -- nothing gets deleted.
    supersedes_decision_id  BIGINT REFERENCES decisions(id),

    -- Cross-host: when the decision involves coordinating with another
    -- entity, the inbox message kind='decision' routes there via inbox's
    -- cross-entity addressing. This column denormalizes the recipient
    -- for fast pending-cross-host queries on the host side.
    cross_entity_id     INTEGER,

    CONSTRAINT decisions_decided_by_check
        CHECK (decided_by IS NULL
            OR decided_by IN ('user','auto')
            OR decided_by LIKE 'pattern:%'),
    CONSTRAINT decisions_review_verdict_check
        CHECK (review_verdict IS NULL
            OR review_verdict IN ('confirmed','refactor','rollback','codebook_update')),
    CONSTRAINT decisions_resolved_consistency
        -- if decided_at is set, chosen_key must be set (DELETE-style
        -- decisions where the answer is "do nothing" should still use a
        -- chosen_key='noop' option rather than NULL).
        CHECK ((decided_at IS NULL) = (chosen_key IS NULL))
);

CREATE INDEX IF NOT EXISTS idx_decisions_entity_pending
    ON decisions (entity_id, proposed_at DESC)
 WHERE decided_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_decisions_entity_decided
    ON decisions (entity_id, decided_at DESC)
 WHERE decided_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_decisions_session
    ON decisions (entity_id, session_id, proposed_at DESC)
 WHERE session_id IS NOT NULL;

-- Auto-decisions awaiting review — the heavily-scrutinized loop closure.
CREATE INDEX IF NOT EXISTS idx_decisions_pending_review
    ON decisions (entity_id, decided_at DESC)
 WHERE decided_by IN ('auto') OR decided_by LIKE 'pattern:%'
   AND reviewed_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_decisions_pattern
    ON decisions (pattern_id, proposed_at DESC)
 WHERE pattern_id IS NOT NULL;


-- ── Extend inbox_messages.message_kind ────────────────────────────────
-- 'decision'        — host needs user to pick from options
-- 'decision_review' — host auto-decided; surfacing for user to confirm /
--                     refactor / rollback / update codebook
ALTER TABLE inbox_messages
    DROP CONSTRAINT IF EXISTS inbox_messages_message_kind_check;

ALTER TABLE inbox_messages
    ADD CONSTRAINT inbox_messages_message_kind_check
        CHECK (message_kind IN (
            'message','permission_grant','permission_request',
            'endpoint_request','endpoint_grant','endpoint_revoke',
            'software_order','software_status',
            'contract_proposal','contract_accept','contract_reject',
            'system_notification',
            'decision','decision_review'
        ));

COMMIT;
