"""Reach-out — the host reaches its person on a channel the user owns.

A doorbell, not a conversation: a short
message on a channel the user built, carrying a link back into the exact
thread that needs them. The content of the conversation never travels.

THE INBOX IS THE BUS. Everything that wants the user's attention already
lands in `inbox_messages` through one handler (server/bridge/memory_handlers
.handle_inbox_append). Reach-out is a subscription on that table, and the
three tables migration v152 declares are the whole model:

    reachout_channels   HOW to reach them  (one row per way)
    reachout_triggers   WHEN               (a predicate on an inbox row)
    reachout_log        WHAT HAPPENED      (one row per attempt)

WHAT THIS PACKAGE IS:

    service.py      the rules — validation, the closed template vocabulary,
                    channel/trigger CRUD, and firing ONE channel once.
    transports.py   the transports themselves, in a registry keyed by
                    action.type: `log`, `http_call`, `runbook`, and
                    `gmail_send` — the user's own Gmail to their own address.
    defaults.py     the default channel: when Gmail is connected, the platform
                    builds "Your Gmail" with the design's three triggers, once.
    dispatcher.py   the background loop: poll the bus, match triggers, fire.

WHAT IT NEVER DOES. It never reads a secret: an `http_call` channel stores the
vault NAME of a credential and the broker (server/vault/authz.use_credential)
opens the envelope server-side and returns only the response. It never reads a
conversation body: the rendered text comes from a closed variable set with no
`msg.body` in it, refused at WRITE time by the renderer rather than at fire
time by a policy. And it never subscribes to another entity's inbox — the
trigger table has no column that could name one.
"""
from server.reachout.service import (  # noqa: F401
    CLOSED_VARS,
    DEFAULT_COOLDOWN_S,
    KNOWN_EVENTS,
    UNEMITTED_EVENTS,
    ReachoutError,
    add_channel,
    add_trigger,
    fire,
    list_state,
    matches,
    remove_channel,
    remove_trigger,
    render,
    validate_action,
)
from server.reachout.transports import (  # noqa: F401
    TRANSPORTS,
    TransportUnavailable,
)
