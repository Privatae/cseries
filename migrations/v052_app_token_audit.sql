-- App-token audit — a loud, queryable record of every /api request that WOULD be
-- (audit mode) or WAS (strict mode) denied for lacking a valid application token, and
-- wasn't on the pre-login allow-list. This drives the app-token lockdown by EVIDENCE,
-- not guesswork: turn on audit, exercise the whole dashboard, read this table to see
-- exactly which routes/callers don't yet carry a token — then wire them and flip to
-- strict. Post-lockdown it keeps surfacing any real denial loudly (never a silent 401).
-- Aggregated by (method, normalized path, reason) so it stays small. Pod-local.
-- See server/applications/middleware.py.
CREATE TABLE IF NOT EXISTS app_token_audit (
    method     text        NOT NULL,
    path       text        NOT NULL,          -- normalized: numeric / long-hex segments → ':id'
    reason     text        NOT NULL,          -- 'no_token' | 'invalid_token'
    mode       text        NOT NULL,          -- 'audit' | 'strict' (mode of the latest hit)
    hits       bigint      NOT NULL DEFAULT 1,
    first_seen timestamptz NOT NULL DEFAULT now(),
    last_seen  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (method, path, reason)
);
