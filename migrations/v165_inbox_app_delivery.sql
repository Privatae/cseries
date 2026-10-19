-- v165 — an installed app can deliver into the host's inbox, and the row says so.
--
-- WHY. A mail-routing app ships a routing engine the
-- user drives: rules match incoming mail and hand the selected messages to the
-- host to summarise, answer or file. That last hop was never built. The app's own
-- README states the reason plainly — "Adding delivery requires the host's verified
-- API contract and a deduplicating adapter" — so matches sit in a local queue
-- marked "Circles connection pending" and the host never sees them. The routing
-- feature is the one the user actually asked for, and it is inert.
--
-- Nothing in the inbox was missing except a NAME for this. The table already
-- admits local app writes (a self-send passes check_inbound_allowed by
-- from_entity_id = self, domain_policy.py:198), and the INSERT is already
-- idempotent on a caller-supplied message_id (ON CONFLICT (message_id) DO
-- NOTHING). What it could not do is distinguish "my mail app routed this to me
-- under a rule I wrote" from an ordinary 'message' — which matters twice over:
--
--   * the user auditing what their host was shown needs to see, per row, that an
--     app put it there and which app. Circles already filters by kind
--     (routes/inbox.py list `kind=`), so a kind is the surface that exists.
--   * 'message' is the kind governed by the host-to-host permission slip. Routed
--     mail is neither host-to-host nor slip-bearing, and filing it under the same
--     name would blur the one kind whose admission rule is load-bearing.
--
-- So: one new kind. No new columns — provenance (which app, under which rule) and
-- the idempotency key ride in the existing `payload` jsonb, stamped server-side
-- from the app's window token so an app cannot claim to be another one.
ALTER TABLE inbox_messages DROP CONSTRAINT IF EXISTS inbox_messages_message_kind_check;

ALTER TABLE inbox_messages ADD CONSTRAINT inbox_messages_message_kind_check
    CHECK (message_kind = ANY (ARRAY[
        'message',
        'permission_grant',
        'permission_request',
        'endpoint_request',
        'endpoint_grant',
        'endpoint_revoke',
        'software_order',
        'software_status',
        'contract_proposal',
        'contract_accept',
        'contract_reject',
        'system_notification',
        'decision',
        'decision_review',
        'machine_request',
        'group_join',
        'rendezvous_intro',
        -- New in v165. Written only by POST /api/inbox/deliver, which stamps
        -- payload.delivered_by_app from the caller's token.
        'app_delivery'
    ]));
