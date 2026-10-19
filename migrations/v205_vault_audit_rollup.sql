-- v205 (2026-10-05): a daily rollup of routine credential reads, beside vault_audit.
--
-- vault_audit keeps one row per credential read, use and change, and nothing
-- ever bounded it: measured 2026-10-05 on one Host it held 80,759 rows (19 MB)
-- since 2026-06-25, 78,492 of them get_envelope reads by the kernel's own
-- lanes (harness key sync, inference routing). A messaging connector adds a
-- get_envelope and a use:http_call row per platform poll. The rule for the
-- CEIGAS audit (migrations/v196) applies here too: routine reads
-- older than 30 days roll up into daily counts, then the raw rows go.
--
-- What rolls up (ceigas_domain_controller.rollup_vault_reads, which counts and
-- deletes in ONE statement per batch, so a crash can never drop a row that was
-- not counted):
--   get_envelope   a kernel lane fetched a sealed envelope (ciphertext); the
--                  row is written after the read, and records no hit or miss
--   use:http_call  a kernel lane made an authenticated READ (GET or HEAD) that
--                  returned a 2xx or 3xx status (the status is recorded only
--                  when the call came back; a call that raised has none) — or
--                  a messaging connector (actor 'connector:<app_id>') did,
--                  whatever the method: its poll is a POST, and each message
--                  it sends is kept one per row in connector_messages
-- Everything else stays raw forever: put, revoke, deactivate, activate,
-- add_recipient (writes), any other POST, PUT, PATCH or DELETE (a write with
-- an effect outside the Host), a use that failed or has no status, use:sign and
-- use:browser_type (the secret signs something or is typed into a page), and
-- every row a PERSON made through the Keys app (actor 'user:<email> ip:<ip>
-- <action>', routes/vault.py _actor) — a reveal returns the plaintext.
--
-- The key keeps every field the raw row says about what happened except its
-- id and its time: the actor verbatim (the kernel's lanes are a short fixed
-- set), the op, the owner and the name, and for a use the HTTP method, host
-- and status. A raw use row never recorded the request path, so a poll and a
-- send to the same host were already one shape; the messages a connector sends
-- are recorded one per row in connector_messages (migrations/v203).

CREATE TABLE IF NOT EXISTS vault_audit_rollup (
    day            DATE        NOT NULL,
    actor          TEXT        NOT NULL,
    op             TEXT        NOT NULL,
    owner_identity TEXT        NOT NULL DEFAULT '',
    logical_name   TEXT        NOT NULL DEFAULT '',
    http_method    TEXT        NOT NULL DEFAULT '',
    http_host      TEXT        NOT NULL DEFAULT '',
    http_status    TEXT        NOT NULL DEFAULT '',
    count          BIGINT      NOT NULL CHECK (count > 0),
    first_seen     TIMESTAMPTZ NOT NULL,
    last_seen      TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (day, actor, op, owner_identity, logical_name,
                 http_method, http_host, http_status)
);
