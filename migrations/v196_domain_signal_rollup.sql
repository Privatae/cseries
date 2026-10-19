-- v196 (2026-10-01): a daily rollup of allowed READ crossings, beside the audit.
--
-- domain_signal_log keeps one row per CEIGAS crossing, and nothing ever bounded
-- it: measured 2026-10-01 it held 0.2-1.6M rows (81-612 MB) on seven of the
-- eight live Hosts, almost all of them allowed reads (every desktop poll is
-- one). Roll up, then remove. Allowed crossings of a
-- read-class operation (list, query, read, status) older than 30 days, made by
-- a GET or HEAD (or with no HTTP method) and not in the keep-raw set (reads with
-- an effect: ceigas_domain_controller.ROLLUP_KEEP_RAW_ROUTES / _TOOLS), become
-- one row here per (day, entity, crossing, route, app, operation) with their
-- count and first/last time, and their raw rows are deleted IN THE SAME
-- STATEMENT that counts them (ceigas_domain_controller.rollup_allowed_reads), so
-- a crash can never drop a row that was not counted. Denials, writes, deletes,
-- stops, modulated, rate-limited and error rows, and every non-read crossing,
-- stay in domain_signal_log as before. Signed audit exports include this table
-- (privatae controller/audit_export.py), so an old window still shows its
-- counts.
--
--   route  the request path with id-like segments as ':id' (the same rule as
--          app_token_audit), or 'tool:<name>' for an MCP crossing, or '' for a
--          crossing with neither
--   app    the app whose token made the request ('host' for the Host's own
--          kernel_op calls), or '' when there was none or the row predates
--          the gate recording it

CREATE TABLE IF NOT EXISTS domain_signal_rollup (
    day           DATE        NOT NULL,
    entity_id     INTEGER     NOT NULL,
    source_domain TEXT        NOT NULL,
    target_domain TEXT        NOT NULL,
    route         TEXT        NOT NULL DEFAULT '',
    app           TEXT        NOT NULL DEFAULT '',
    operation     TEXT        NOT NULL,
    count         BIGINT      NOT NULL CHECK (count > 0),
    first_seen    TIMESTAMPTZ NOT NULL,
    last_seen     TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (day, entity_id, source_domain, target_domain, route, app, operation)
);
