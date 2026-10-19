-- v127 (2026-09-04): NAC egress gains a SUBJECT and an EXIT.
--
-- v059 gave the user a destination allow/deny list. It could say "this host may reach
-- api.x.ai" but never "the BROWSER reaches it through MY machine" — there was no way to
-- name the caller, and no way to name whose connection carries the traffic. The Browser
-- app is the forcing case: its pages leave from the node's shared public address and
-- banks refuse it.
--
--   subject — who is calling:  '*' | 'pod' | 'app:<app_id>' | 'harness:<harness_id>'
--             'pod' is the entity pod's network namespace (kernel, hub, postgres — all
--             three share it, so loopback cannot tell them apart and the name must not
--             pretend otherwise).
--   exit    — whose connection carries it: 'default' | 'platform' | 'bastion:<host_name>'
--             A bastion is named by its HEARTBEAT HOSTNAME, never by token_id: every
--             re-enrolment mints a new token row (bastion_enroll.py), and an id inside
--             immutable signed terms would make the pin fail closed forever the first
--             time the user's machine came back after a week off.
--
-- BOTH DEFAULT so every existing row keeps its exact present meaning, and both are
-- carried INSIDE signed_terms by the approve path — a field outside the signature could
-- be set by a direct DB write with no user key on it, which for `exit` would mean
-- silently re-routing the user's traffic. load_egress_rules reads them from the verified
-- terms with CONSTANT defaults, never falling back to these columns.

ALTER TABLE nac_egress
    ADD COLUMN IF NOT EXISTS subject TEXT NOT NULL DEFAULT '*',
    ADD COLUMN IF NOT EXISTS exit    TEXT NOT NULL DEFAULT 'default';

-- The gate loads by (priority, id) and then filters on subject in Python; the partial
-- index from v059 still covers that scan. This one serves the UI's "what is pinned"
-- question and keeps a pinned-rule audit cheap.
CREATE INDEX IF NOT EXISTS idx_nac_egress_pinned
    ON nac_egress (exit)
    WHERE approved_at IS NOT NULL AND revoked_at IS NULL AND exit <> 'default';
