-- v093_debug_lane.sql — trusted-reporter debug lane (2026-07-17)
--
-- RETIRED 2026-08-29. Superseded by the Tickets support lane (cseries
-- migrations/v122_support_tickets.sql on the entity side; privatae
-- bridge/migration-support-tickets.sql on the platform side, which DROPS the
-- two tables below). Kept in the immutable migration history; the entrypoint
-- still applies it by glob (idempotent, harmless — two unused tables on a
-- fresh entity, nothing reads or writes them).
--
-- Dev-stage bug-report intake for hand-picked trusted entities. An enrolled
-- entity's host files structured reports via the report_bug tool
-- (bridge op platform.report_bug); the mgmt watcher dispatches the patch
-- pipeline and writes status back. Enrollment is operator-side INSERT only —
-- there is no self-serve path, and rows expire. Emptying/deactivating
-- debug_reporters is the lane's kill switch.

CREATE TABLE IF NOT EXISTS debug_reporters (
    entity_id   BIGINT PRIMARY KEY REFERENCES entity_registry(id),
    enrolled_by TEXT NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    -- Cap on concurrently-open reports. Protects the dispatch pipeline
    -- (each open report can hold an agent run), not the reporter.
    max_open    INT NOT NULL DEFAULT 20,
    -- NULL = no expiry, but the lane is dev-stage: enrollments should set one.
    expires_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bug_reports (
    id                 BIGSERIAL PRIMARY KEY,
    reporter_entity_id BIGINT NOT NULL REFERENCES entity_registry(id),
    title              TEXT NOT NULL,
    surface            TEXT NOT NULL DEFAULT '',
    repro_steps        TEXT NOT NULL DEFAULT '',
    expected           TEXT NOT NULL DEFAULT '',
    actual             TEXT NOT NULL DEFAULT '',
    logs               TEXT NOT NULL DEFAULT '',
    severity           TEXT NOT NULL DEFAULT 'normal'
                       CHECK (severity IN ('low','normal','high')),
    -- new → in_progress → (needs_info | rejected | pr_open → shipping →
    -- shipped_canary → resolved). The mgmt watcher owns every transition
    -- after 'new'.
    status             TEXT NOT NULL DEFAULT 'new'
                       CHECK (status IN ('new','in_progress','needs_info',
                                         'pr_open','shipping',
                                         'shipped_canary','resolved','rejected')),
    resolution         TEXT NOT NULL DEFAULT '',
    pr_url             TEXT NOT NULL DEFAULT '',
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS bug_reports_status_idx   ON bug_reports(status);
CREATE INDEX IF NOT EXISTS bug_reports_reporter_idx ON bug_reports(reporter_entity_id);
