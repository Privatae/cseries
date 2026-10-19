"""The transports a reach-out channel can actually use.

One registry keyed by `action.type`. A transport takes an already-RENDERED
message (the closed variable set, resolved) and does one thing with it, then
returns a small redacted detail dict for the `reachout_log` row. It never sees
a secret and never decides whether it was allowed to run — service.py owns
validation and the log row; this file owns delivery.

    log        record the nudge in reachout_log and nothing else. Complete.
    http_call  ONE brokered outbound request through vault.authz.use_credential
               — the channel row carries the credential's NAME, the broker
               opens the envelope in-pod, injects it, and returns status+body.
    runbook    stamp out one of the host's task templates to fire NOW, so the
               reach-out runs through the governed turn pipeline the host
               already has rather than a second one built here.
    gmail_send ONE mail from the user's own Gmail TO the user's own Gmail
               address — the design's default channel. The
               recipient is the connection's own account and nothing else.
"""
from __future__ import annotations

import base64
import logging
import re
from email.message import EmailMessage
from typing import Any, Awaitable, Callable

log = logging.getLogger("privatae.reachout.transports")


class TransportUnavailable(RuntimeError):
    """This transport cannot deliver — stated, never faked."""


# ── log ───────────────────────────────────────────────────────────────────
async def _send_log(ctx: dict[str, Any]) -> dict[str, Any]:
    """The in-host channel: the nudge lands in reachout_log and stops there.

    It delivers nothing outward, and that is the point of having it — it is the
    channel that exercises the whole path (trigger match, idempotency, cooldown,
    quiet window, the log row) with no credential, no egress and no provider.
    A host with no channel built yet can still be given one that records what it
    WOULD have said.
    """
    log.info("reachout: %s", ctx.get("text"))
    return {"transport": "log", "text": ctx.get("text")}


# ── http_call ─────────────────────────────────────────────────────────────
async def _send_http_call(ctx: dict[str, Any]) -> dict[str, Any]:
    """One brokered request. The secret is named, never carried.

    The action's `form` becomes aiohttp's form body and `json` becomes its JSON
    body — the broker's own two shapes (server/vault/authz._do_http_call passes
    `json=` and `data=` straight through), so a provider that wants
    application/x-www-form-urlencoded (Twilio's Messages.json, the design's
    worked example) needs nothing special here.
    """
    action = ctx["action"]
    pool = ctx["pool"]
    entity_id = int(ctx["entity_id"])
    cred_name = action["cred_name"]

    # THE PIN, CHECKED AGAINST THE URL THAT IS ACTUALLY ABOUT TO BE CALLED.
    # The url is a template and is rendered HERE, so validation-time and
    # fire-time are different strings; the host recorded at creation is what
    # the user approved, and a rendered url pointing anywhere else does not go.
    # A row written before this pin existed has no `pinned_host` and is refused
    # rather than grandfathered — there is no such row on any host yet (nothing
    # has shipped), and "refuse" is the right default for the one that is.
    url = ctx["render"](action["url"])
    pinned = (action.get("pinned_origin") or "").strip().lower()
    shown = (action.get("pinned_host") or pinned or "").strip()
    if not pinned:
        raise TransportUnavailable(
            f"{cred_name}: this channel has no pinned destination — rebuild it "
            "so the place it may reach is recorded")
    # The SAME derivation that produced the pin, applied to the url that is
    # about to be called: scheme, host and port, default port normalised. A
    # hostname-only comparison was the first cut and a templated port rendered
    # straight past it. `require_https=False` here because the scheme is part
    # of what is compared — an http render simply will not equal an https pin,
    # and that reads better than a second, separate refusal.
    from server.reachout.service import ReachoutError, _url_origin
    try:
        actual = _url_origin(url, require_https=False)
    except ReachoutError as e:
        raise TransportUnavailable(f"{cred_name}: {e}")
    if actual != pinned:
        raise TransportUnavailable(
            f"{cred_name}: this channel may only reach {shown}, and the "
            f"message renders a url at {actual} — refused")

    request: dict[str, Any] = {
        "url": url,
        "method": (action.get("method") or "POST").upper(),
        "auth_scheme": (action.get("auth_scheme") or "bearer"),
        # A pin the destination can undo is not a pin. Measured on
        # aiohttp 3.14.3: across a cross-origin redirect it strips
        # `Authorization` but forwards a CUSTOM header verbatim — and
        # `auth_scheme='header'` with a caller-named `auth_header` is exactly
        # that shape, so a 307 from the pinned host would hand the key to
        # whatever host it named. See server/vault/authz.py:_do_http_call.
        "allow_redirects": False,
    }
    if action.get("auth_header"):
        request["auth_header"] = action["auth_header"]
    if action.get("headers"):
        request["headers"] = {k: ctx["render"](v) for k, v in action["headers"].items()}
    if action.get("form"):
        request["body"] = {k: ctx["render"](v) for k, v in action["form"].items()}
    if action.get("json"):
        request["json"] = {k: ctx["render"](v) for k, v in action["json"].items()}

    from server.vault.authz import use_credential
    result = await use_credential(
        pool, entity_id=entity_id, name=cred_name,
        action="http_call", request=request,
    )
    status = int(result.get("status") or 0)
    if 300 <= status < 400:
        # With redirects refused, a 3xx comes back as the response. Named
        # separately from a plain failure because "your provider is trying to
        # send this somewhere else" is a different thing to tell the user than
        # "your provider is down".
        raise TransportUnavailable(
            f"{cred_name}: {shown} answered {status} — a redirect, which this "
            "channel does not follow, because the credential would travel to "
            "wherever it pointed")
    if not (200 <= status < 300):
        raise TransportUnavailable(
            f"{cred_name}: the channel's endpoint answered {status}")
    # status + which credential, never the body: a provider's response can echo
    # the message back, and the log row is the one artifact a support path
    # reads.
    return {"transport": "http_call", "cred_name": cred_name, "status": status,
            "host": shown}


# ── runbook ───────────────────────────────────────────────────────────────
async def _send_runbook(ctx: dict[str, Any]) -> dict[str, Any]:
    """Fire one of the host's own task templates, now.

    Deliberately NOT a second execution engine: it writes a scheduled_task
    calendar event due immediately, and `offline_task_runner` — the loop that
    already runs every standing task through the governed turn pipeline — picks
    it up on its next tick. So a reach-out runbook is governed, budgeted and
    audited exactly like a task the user scheduled by hand.
    """
    action = ctx["action"]
    pool = ctx["pool"]
    entity_id = int(ctx["entity_id"])
    template_id = int(action["template_id"])

    row = await pool.fetchrow(
        "SELECT name, prompt, steps, recurrence, type_id FROM entity_task_templates "
        "WHERE id=$1 AND entity_id=$2",
        template_id, entity_id,
    )
    if row is None:
        raise TransportUnavailable(
            f"no task_template with id {template_id} for this host")

    import json as _json
    steps = row["steps"]
    if isinstance(steps, str):
        try:
            steps = _json.loads(steps)
        except Exception:
            steps = []

    from datetime import datetime, timezone
    from server.bridge.memory_handlers import handle_calendar_append
    res = await handle_calendar_append({
        "event_type": "scheduled_task",
        "status": "scheduled",
        "event_at": datetime.now(timezone.utc).isoformat(),
        "summary": f"Reach-out: {row['name']}",
        "payload": {
            "prompt": ctx["render"](row["prompt"] or ""),
            "steps": [ctx["render"](str(s)) for s in (steps or [])],
            "reachout": {"message_id": ctx.get("message_id")},
            # Turn types step 5: the type the channel was built under, else
            # the template's — the runner fires the task under it and refuses
            # it if that type is no longer active. Neither: untyped, as before.
            **({"type_id": str(action.get("type_id") or row["type_id"])}
               if (action.get("type_id") or row["type_id"]) else {}),
        },
    })
    if not (res or {}).get("ok"):
        raise TransportUnavailable(
            f"runbook not scheduled: {(res or {}).get('error') or 'unknown error'}")
    return {"transport": "runbook", "template_id": template_id,
            "event_id": res.get("event_id")}


# ── gmail_send ────────────────────────────────────────────────────────────
#
# The design's default channel: the user's OWN Gmail, writing to the user's
# OWN address.
# Measured: a self-addressed Gmail fires the phone
# notification when the Gmail app is not in the foreground, so this is a
# doorbell with no provider account and no phone number anywhere.
#
# WHAT MADE IT BUILDABLE. The Gmail manifest now asks for gmail.modify
# (server/integrations/manifests/gmail.yaml), which includes send, and a
# connection's token is sealed in the user's own vault and refreshed by
# integrations/core/connection.ensure_fresh_token — the one refresh path the
# broker and the sync loops share. No new scope, no new credential store.
#
# THE RECIPIENT IS NOT A PARAMETER. It is the connection's own account address,
# recorded when the user consented (connection_config.account_email,
# providers/_google_base.py). The action names the CONNECTION, never an
# address, and validate_action refuses any recipient-shaped field, so there is
# no value a template, a host or a caller can put anywhere that makes this mail
# go to somebody else. That is what lets a channel be created with no second
# factor: it cannot widen who a credential reaches.
#
# NEVER CONTENT. Subject "[host] <what happened>", body the same two lines every
# channel sends — what happened and the link (service.default_text). The inbox
# body is not a name the renderer knows, and this transport renders nothing of
# its own, so a customer's conversation cannot reach a mail provider through it.
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"

_EMAIL_RX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def mask_address(addr: str) -> str:
    """c•••@gmail.com — enough for the user to recognise their own address in
    the log and the pane, not enough to be worth lifting from a support
    export."""
    local, _, domain = str(addr or "").partition("@")
    if not domain:
        return "•••"
    return f"{local[:1]}•••@{domain}"


def _header_safe(s: str, cap: int = 200) -> str:
    # A header value with a line break is a header injection; EmailMessage
    # refuses one with a ValueError, which here would read as a transport fault.
    # Collapse whitespace instead, so a subject that happens to carry a newline
    # still sends as one line.
    return re.sub(r"\s+", " ", str(s or "")).strip()[:cap]


async def _gmail_post(access_token: str, raw: str) -> tuple[int, dict[str, Any]]:
    """ONE POST to Gmail's send endpoint. A seam, so the suite drives the whole
    transport without the network. Redirects are refused for the reason
    http_call refuses them: a bearer token must not follow a 3xx anywhere.

    Through the pod's egress proxy (integrations/core/egress.session), because
    that is the only way out of an entity pod: a direct connection to Gmail
    never connects (measured, egress.py says where)."""
    import aiohttp

    from server.integrations.core import egress
    async with egress.session() as s:
        async with s.post(
            GMAIL_SEND_URL,
            headers={"Authorization": f"Bearer {access_token}",
                     "Content-Type": "application/json"},
            json={"raw": raw},
            timeout=aiohttp.ClientTimeout(total=30),
            allow_redirects=False,
        ) as r:
            try:
                body = await r.json(content_type=None)
            except Exception:
                body = {}
            return r.status, (body if isinstance(body, dict) else {})


async def gmail_connection(connection_id: str) -> tuple[dict[str, Any], str]:
    """(connection row, its own account address), or TransportUnavailable with
    the sentence the user needs. Shared by the transport and validate_action so
    the check at creation and the check at send are the same check."""
    from server.integrations.core import storage
    from server.integrations.core.models import ConnectionStatus
    row = await storage.get_connection(str(connection_id or ""))
    if not row:
        # Revoking keeps the row (connection.revoke_vault), so this is a
        # connection that was deleted outright, and with it the only record of
        # which mailbox the channel wrote to — nothing can move it now.
        raise TransportUnavailable(
            "the Gmail connection this channel was built on no longer exists — "
            "remove this channel; connecting Gmail again builds a new one")
    if row.get("provider_key") != "gmail":
        raise TransportUnavailable(
            f"connection {connection_id} is {row.get('provider_key')!r}, not Gmail")
    if row.get("status") != ConnectionStatus.ACTIVE.value:
        # Reconnecting moves every channel on this mailbox onto the new
        # connection (reachout/defaults.ensure_gmail_channel), so this sentence
        # is the whole fix, not the first step of one.
        raise TransportUnavailable(
            f"Gmail needs you to sign in again (the connection is "
            f"{row.get('status')}) — reconnect Gmail and this channel moves to "
            "the new connection by itself")
    cfg = row.get("connection_config") or {}
    addr = str(cfg.get("account_email") or "").strip()
    if not _EMAIL_RX.match(addr):
        raise TransportUnavailable(
            "the Gmail connection has no account address on file, so there is "
            "nobody this channel is allowed to write to — reconnect Gmail")
    return row, addr


def _google_error(body: dict[str, Any]) -> tuple[str, str]:
    """(reason, message) out of a Google API error body; either may be "".

    Google's shape is {"error": {"code", "message", "status", "errors":
    [{"reason", ...}], "details": [{"reason", ...}]}}; the reason is looked for
    in that order of specificity. The message is Google's own sentence, kept
    because it is the one thing that says WHY — a quota, a domain policy, a
    missing scope — and the user cannot act on a bare status code.
    """
    err = body.get("error") if isinstance(body, dict) else None
    if not isinstance(err, dict):
        return "", ""
    reason = ""
    for key in ("errors", "details"):
        for item in err.get(key) or []:
            if isinstance(item, dict) and item.get("reason"):
                reason = str(item["reason"])
                break
        if reason:
            break
    reason = _header_safe(reason or err.get("status") or "", 60)
    return reason, _header_safe(err.get("message") or "", 200)


# The refusals that mean the grant itself lacks the right to send. Only these,
# and a 401, tell the user to sign in again: a 403 is also how Google refuses
# for a sending quota or a Workspace domain policy, and "sign in again" would
# send the user round a reconnect that cannot fix either.
_GMAIL_SCOPE_REASONS = frozenset({
    "insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT", "insufficient_scope"})


async def _send_gmail(ctx: dict[str, Any]) -> dict[str, Any]:
    """One self-addressed mail. Returns the masked recipient and Gmail's id."""
    from server.integrations.core import connection as conn_mod
    action = ctx["action"]
    conn_id = str(action.get("connection_id") or "")
    _row, addr = await gmail_connection(conn_id)
    try:
        token = await conn_mod.ensure_fresh_token(conn_id)
    except Exception as e:  # noqa: BLE001 — becomes the user's sentence below
        raise TransportUnavailable(
            f"the Gmail sign-in could not be refreshed ({type(e).__name__}) — "
            "this mail was not sent and is not retried") from None
    if not token:
        raise TransportUnavailable(
            "Gmail needs you to sign in again — the connection has no usable "
            "token (ensure_fresh_token marked it for re-authorization)")

    vars_ = ctx.get("vars") or {}
    host = _header_safe(vars_.get("host.name") or "") or "Your Host"
    what = _header_safe(vars_.get("msg.subject") or "") or "Your Host needs you"
    msg = EmailMessage()
    msg["From"] = addr
    msg["To"] = addr
    msg["Subject"] = f"[{host}] {what}"
    msg.set_content(str(ctx.get("text") or what))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")

    try:
        status, body = await _gmail_post(token, raw)
    except TransportUnavailable:
        raise
    except Exception as e:  # noqa: BLE001 — a network fault is the user's sentence
        raise TransportUnavailable(
            f"Gmail could not be reached from this host ({type(e).__name__}) — "
            "this mail was not sent and is not retried") from None
    if 300 <= status < 400:
        raise TransportUnavailable(
            f"Gmail answered {status} — a redirect, which this channel does not "
            "follow, because the token would travel to wherever it pointed")
    reason, why = _google_error(body)
    if status == 401 or (status == 403 and reason in _GMAIL_SCOPE_REASONS):
        raise TransportUnavailable(
            f"Gmail refused the send ({status}{', ' + reason if reason else ''}) — "
            "the connection needs you to sign in again and allow sending; "
            "reconnecting Gmail moves this channel to the new connection")
    if not (200 <= status < 300):
        raise TransportUnavailable(
            f"Gmail refused the send ({status}{', ' + reason if reason else ''}): "
            f"{why or 'Gmail gave no reason'}")
    # The masked address and Gmail's own message id — never the body, for the
    # reason http_call returns no body: the log row is what a support path reads.
    return {"transport": "gmail_send", "to": mask_address(addr), "status": status,
            "gmail_id": str(body.get("id") or "") or None}


TRANSPORTS: dict[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]] = {
    "log": _send_log,
    "http_call": _send_http_call,
    "runbook": _send_runbook,
    "gmail_send": _send_gmail,
}

# Action types the design names but that deliver nothing yet, each with the
# sentence that says why. EMPTY since gmail_send landed, and kept:
# a name in TRANSPORTS is a promise that something is delivered, so the next
# transport that is designed before it is built goes here, with its reason,
# rather than into TRANSPORTS with a stub.
UNBUILT_ACTIONS: dict[str, str] = {}
