-- v187: the thread auditor's findings.
--
-- The background memory call no longer only writes memories: it audits each
-- thread's chain of thought. A finding is one thing wrong in ONE thread (its
-- scope descriptor, the same {kind, ref} the turn rows carry):
--
--   cross_thread       another thread's own words reached this thread's prompt
--                      (checked deterministically from prompt_log)
--   contradiction      a reply contradicted what the thread already established
--   unsupported_state  a reply stated the current state of a system as fact
--                      with no live read behind it in the thread — how a Host
--                      reported dispatch broken seven weeks after the fix
--
-- A finding is shown to the Host in that thread's memory block until the next
-- audit pass sees it corrected (closed 'corrected') or it ages out ('expired').
-- The counts by close_reason are what say whether the audit pays for itself.
-- One row per thing (fingerprint), with a moving last_seen_at, never a pile.

CREATE TABLE IF NOT EXISTS memory_audit_findings (
    id              SERIAL PRIMARY KEY,
    entity_id       INTEGER NOT NULL,
    scope_kind      TEXT NOT NULL,
    scope_ref       TEXT NOT NULL,
    kind            TEXT NOT NULL
                    CHECK (kind IN ('cross_thread', 'contradiction', 'unsupported_state')),
    said            TEXT NOT NULL,           -- the reply's words (or the leaked row's), capped
    note            TEXT,                    -- why it is a finding, in one line
    evidence        JSONB,                   -- prompt_log ids / memory ids / the earlier words
    fingerprint     TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at       TIMESTAMPTZ,
    close_reason    TEXT CHECK (close_reason IN ('corrected', 'expired')),
    UNIQUE (entity_id, fingerprint)
);

CREATE INDEX IF NOT EXISTS memory_audit_findings_open
    ON memory_audit_findings (entity_id, scope_kind, scope_ref)
    WHERE closed_at IS NULL;
