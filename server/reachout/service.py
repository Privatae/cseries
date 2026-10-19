"""Reach-out — the rules. Validation, the closed vocabulary, CRUD, one fire.

Everything that decides WHETHER and WHAT lives here; server/reachout/
transports.py decides HOW. The host tool (`reachout`), the HTTP routes
(/api/reachout) and the dispatcher loop all call these functions, so the tool
and the pane cannot drift into two different sets of rules.

Tables: migration v152 (reachout_channels / reachout_triggers / reachout_log).
No DDL runs here — the kernel does not create its own schema.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, time as _time, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from server.deep_link import app_link
from server.reachout.transports import TRANSPORTS, UNBUILT_ACTIONS, TransportUnavailable

log = logging.getLogger("privatae.reachout")


class ReachoutError(ValueError):
    """A refusal the user or the host should read — never a stack trace."""


# ── Numbers, and where they come from ─────────────────────────────────────
#
# Both are the design's, not invented here:
#   cooldown  120 s per channel, adjustable per channel, and explicitly NOT a cap on how
#             many triggers a user may have.
#   quiet     the platform ships NO quiet window. The columns
#             exist and are enforced; they are NULL until the user sets one.
DEFAULT_COOLDOWN_S = 120

# The dispatcher's poll cadence, matching offline_task_runner.POLL_INTERVAL_S
# (30 s) for the same reason it has that value: fast enough that a nudge
# follows the work within a tick, cheap enough to run forever.
POLL_INTERVAL_S = 30

CHANNEL_KINDS = ("email", "sms", "voice", "webhook", "runbook", "log")
CREATED_BY = ("host", "user", "platform")
LOG_STATUSES = ("pending", "sent", "skipped_cooldown", "skipped_quiet", "failed")

# How long a `pending` row may stay pending before it is written off.
#
# `pending` is the row the idempotency gate writes before the attempt resolves,
# so a pod that stops between the INSERT and the UPDATE leaves one behind — and
# because the gate has already consumed that (trigger_id, message_id), it will
# never be retried. Keeping no-retry is deliberate; leaving the loss INVISIBLE
# was not. After this window the sweep marks it `failed` with
# `detail.interrupted`, so "the doorbell did not ring" has a row behind it in
# the log and in the Circles view instead of a silent gap.
#
# Five minutes, and the derivation rather than a taste: the longest an attempt
# can legitimately still be in flight is the transport's own timeout, which is
# the vault broker's 30 s default (server/vault/authz.py _do_http_call reads
# `request.get("timeout") or 30`). Five minutes is an order of magnitude above
# that, so the sweep cannot race a live attempt, and it is short enough that a
# user asking "did it not ring?" gets the answer within one sitting.
PENDING_INTERRUPTED_AFTER_S = 300

# ── The closed template vocabulary ────────────────────────────────────────
#
# This set IS design principle 1. A reach-out payload is derived from the inbox
# row's identifiers and a link and NEVER from a conversation body, and the way
# that is kept true is that `msg.body` is not a name the renderer knows — so a
# channel referencing it is refused when it is WRITTEN, not quietly emptied
# when it fires. `user.*` is open on purpose: a phone number is the user's, it
# lives on the channel row in their own entity DB, and the platform never holds
# one.
CLOSED_VARS = (
    "msg.subject", "msg.kind", "msg.link", "msg.project_id", "msg.decision_id",
    "host.name", "user.email",
)
_VAR_RX = re.compile(r"\{\{\s*([a-z_]+(?:\.[a-z_0-9]+)*)\s*\}\}")
_USER_PREFIX = "user."

# ── The events a trigger can match ────────────────────────────────────────
#
# LIVE — something writes these today:
#   build_finished / compare_finished / app_proven / agent_finished
#       brain/mastercode/runtime/session.py, in deliver_agent_result
#   site_published
#       server/routes/applications.py, in publish_site_service
#   oversight_critical / oversight_high
#       brain/mastercode/runtime/oversight.py, in notify_new_findings — the
#       management lane's ledger. `oversight_critical` is written by
#       the heartbeat wake the instant a machine reports its first critical;
#       `oversight_high` only by the 5-minute standing pass.
#
# DECLARED AND UNEMITTED — nothing writes these yet, and a trigger on one is
# accepted and simply never fires until something does. Kept as a named,
# possibly-empty tuple rather than deleted: it is EMPTY today because the
# management lane landed, and the next declared-ahead-of-its-writer
# event belongs here rather than in a comment. The list is surfaced to the host
# (`reachout` op='list') so a user asking for "tell me when something serious
# happens" is never told the vocabulary has no word for it.
UNEMITTED_EVENTS: tuple[str, ...] = ()
KNOWN_EVENTS = (
    "build_finished", "compare_finished", "app_proven", "agent_finished",
    "site_published", "oversight_critical", "oversight_high",
) + UNEMITTED_EVENTS


# ── Validation ────────────────────────────────────────────────────────────

def _check_template(value: Any) -> None:
    """Refuse an unknown variable at WRITE time."""
    if not isinstance(value, str):
        return
    for name in _VAR_RX.findall(value):
        if name in CLOSED_VARS or name.startswith(_USER_PREFIX):
            continue
        raise ReachoutError(
            f"{{{{{name}}}}} is not a reach-out variable. The set is closed: "
            + ", ".join(CLOSED_VARS)
            + ", and user.<field> for values you put on the channel. There is "
              "no msg.body — a reach-out carries a subject and a link, never "
              "the conversation."
        )


def _walk_templates(node: Any) -> None:
    if isinstance(node, dict):
        for v in node.values():
            _walk_templates(v)
    elif isinstance(node, list):
        for v in node:
            _walk_templates(v)
    else:
        _check_template(node)


async def validate_action(pool, entity_id: int, action: Any) -> dict:
    """Return the action, or raise ReachoutError saying exactly what is wrong.

    Checks, in the order a person would ask them: is it one of the shapes; is
    the transport built; are its required fields there; does every template in
    it stay inside the closed vocabulary; and — for http_call — does the named
    credential actually exist and is it active on THIS host, so a channel is
    never created half-built.
    """
    if not isinstance(action, dict):
        raise ReachoutError("action must be an object with a 'type'")
    atype = (action.get("type") or "").strip()
    if atype in UNBUILT_ACTIONS:
        raise ReachoutError(UNBUILT_ACTIONS[atype])
    if atype not in TRANSPORTS:
        raise ReachoutError(
            f"action.type must be one of {sorted(TRANSPORTS)} (got {atype!r})")

    _walk_templates(action)

    if atype == "http_call":
        cred = (action.get("cred_name") or "").strip()
        if not cred:
            raise ReachoutError("an http_call channel needs cred_name — the "
                                "name of a credential in this host's vault")
        url = (action.get("url") or "").strip()
        if not url:
            raise ReachoutError("an http_call channel needs a url")
        scheme = (action.get("auth_scheme") or "bearer").lower()
        if scheme not in ("bearer", "header", "basic"):
            raise ReachoutError(
                f"auth_scheme must be bearer, header or basic (got {scheme!r})")

        # THE DESTINATION IS PINNED AT CREATION, and this is the wall.
        #
        # A channel binds a credential to a place. The vault broker will send
        # that credential wherever the channel's url points
        # (server/vault/authz.py _do_http_call injects it server-side), and the
        # governed egress PROXY does not bound where that is: the entity squid
        # (controller/manifests/0012_entity_egress_proxy.yaml) denies
        # platform-internal CIDRs and non-80/443 ports and then says
        # `http_access allow entity_clients` (:92) — any public host — with
        # `access_log none` (:99). So the only place a destination can be
        # bounded is here.
        #
        # THE HOST DOES KEEP A RECORD, THOUGH, and an earlier draft of this
        # comment said flatly that nothing did, which was wrong in the
        # direction that matters: it would have had someone conclude a
        # brokered call is untraceable. The PROXY keeps none — that is the
        # sovereign-network egress doctrine, and it is about the platform not
        # watching the user. The pod keeps its own: every brokered use writes a
        # `use:http_call` row to vault_audit carrying the destination netloc
        # (server/vault/authz.py:156-158, `urlparse(...).netloc`), plus the
        # method and the status. So after the fact the USER can see which
        # credential went where, from their own host's audit — and the platform
        # still cannot.
        #
        # https only: a bearer token over http is the secret in cleartext to
        # anything on the path, and there is no reach-out endpoint worth having
        # that cannot do TLS.
        #
        # The pin is SCHEME + HOST + PORT, not the hostname alone. Hostname
        # alone was the first cut and it leaked: a templated port
        # (`https://api.test:{{user.port}}/send`) renders past a hostname
        # comparison, so the credential reaches a different service on the same
        # machine than the one the user approved.
        origin = _url_origin(url, require_https=True)
        # FROZEN onto the row now; the transport re-derives it from the
        # rendered url at fire time and refuses a mismatch. Templates are
        # substituted at fire time, so "the url I validated" and "the url that
        # gets called" are not the same string — without the pin, a
        # `{{user.x}}` in the authority half moves the destination after the
        # user approved it.
        #
        # `pinned_host` is kept beside `pinned_origin` as the human-readable
        # half: it is what the step-up prompt and the add_channel result show
        # the user, and what the log row records.
        action = {**action, "pinned_origin": origin,
                  "pinned_host": _origin_display(origin)}
        if pool is not None:
            # Asked through the vault's own reader rather than with SQL of our
            # own: the envelope table is keyed on (owner_identity,
            # logical_name), not on entity_id, and `active` there means
            # superseded_at IS NULL AND deactivated_at IS NULL — two columns a
            # hand-written query gets wrong in exactly the direction that
            # builds a channel against a revoked key (server/vault/store.py
            # list_names, :108-121; identity.host_identity, :39).
            from server.vault import identity as _identity, store as _store
            owner = _identity.host_identity(int(entity_id))
            names = await _store.list_names(pool, owner_identity=owner)
            live = any(r.get("logical_name") == cred and r.get("deactivated_at") is None
                       for r in names)
            if not live:
                raise ReachoutError(
                    f"no active credential named {cred!r} in this host's vault "
                    "— add it in Keys → Vault first, then build the channel")

    if atype == "runbook":
        tid = action.get("template_id")
        try:
            tid = int(tid)
        except (TypeError, ValueError):
            raise ReachoutError("a runbook channel needs template_id — the id "
                                "of one of this host's task_templates")
        # Existence checked at WRITE time, for the same reason the credential
        # is: a channel is never created half-built. Without this the only
        # signal that the template id was wrong arrived as a `failed` log row
        # the first time something finished, which is the worst possible moment
        # to discover the doorbell was never wired.
        if pool is not None:
            live = await pool.fetchval(
                "SELECT 1 FROM entity_task_templates WHERE id=$1 AND entity_id=$2",
                tid, int(entity_id))
            if not live:
                raise ReachoutError(
                    f"no task_template with id {tid} for this host — create it "
                    "with task_template op='create' first, then build the channel")

    if atype == "gmail_send":
        action = await _validate_gmail_send(action)

    return action


# Recipient-shaped fields. A gmail_send channel writes to the connection's own
# address and nowhere else (transports.gmail_connection), so any of these on
# the action is either a mistake or an attempt to point the user's mailbox at
# somebody else — refused at WRITE time, by name, rather than silently ignored.
_RECIPIENT_FIELDS = ("to", "cc", "bcc", "recipient", "recipients", "from",
                     "reply_to", "reply-to", "address", "email")


async def _validate_gmail_send(action: dict) -> dict:
    """Pin the channel to ONE Gmail connection and to nothing else.

    `connection_id` may be omitted when the host holds exactly one active Gmail
    connection — the common case and the one the default channel is built on —
    and is then filled in, so the row always names the connection it sends
    through. `pinned_to` is the masked address, kept for the pane and the log the
    way http_call keeps `pinned_host`: display only; the transport re-reads the
    address from the connection at send time and never from this.
    """
    from server.reachout.transports import (TransportUnavailable,
                                            gmail_connection, mask_address)
    for k in _RECIPIENT_FIELDS:
        if k in action:
            raise ReachoutError(
                f"a gmail_send channel takes no {k!r}: it always writes from the "
                "user's own Gmail to that same address, and nowhere else")
    extra = sorted(set(action) - {"type", "connection_id", "pinned_to"})
    if extra:
        raise ReachoutError(
            f"a gmail_send channel takes only connection_id (got {extra}) — its "
            "subject and body are fixed: what happened, and the link")
    conn_id = str(action.get("connection_id") or "").strip()
    if not conn_id:
        from server.integrations.core import storage
        from server.integrations.core.models import ConnectionStatus
        live = [r for r in await storage.list_connections(provider_key="gmail")
                if r.get("status") == ConnectionStatus.ACTIVE.value]
        if not live:
            raise ReachoutError(
                "there is no connected Gmail on this host — connect Gmail first "
                "(one Google consent screen); the channel is built with it")
        if len(live) > 1:
            raise ReachoutError(
                "more than one Gmail is connected — name the one to use with "
                f"connection_id ({', '.join(r['id'] for r in live)})")
        conn_id = live[0]["id"]
    try:
        _row, addr = await gmail_connection(conn_id)
    except TransportUnavailable as e:
        raise ReachoutError(str(e)) from None
    return {"type": "gmail_send", "connection_id": conn_id,
            "pinned_to": mask_address(addr)}


_DEFAULT_PORTS = {"https": 443, "http": 80}


def _url_origin(url: str, *, require_https: bool) -> str:
    """The ORIGIN a url points at — scheme://host:port — normalised.

    Scheme and port are in here, not just the host, because the pin is a
    promise about where a credential goes and "the same hostname" is not that
    promise: `https://api.test:{{user.port}}/send` renders past a hostname
    comparison and reaches a different service on the same machine.

    The default port is normalised in rather than left implicit, so
    `https://api.test` and `https://api.test:443` pin identically and a user
    who wrote one is not refused for the other at fire time.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        raise ReachoutError(f"{url!r} is not a url this host can call")
    scheme = (parsed.scheme or "").lower()
    if require_https and scheme != "https":
        raise ReachoutError(
            "a reach-out channel must use https — the credential travels on "
            f"that request, and {scheme or 'a scheme-less url'} puts it in "
            "cleartext to everything on the path")

    # USERINFO IS REFUSED OUTRIGHT, and this is a readability defence, not a
    # protocol one. `https://api.good.com@evil.com/send` pins evil.com and
    # READS as api.good.com to a person skimming it — so the host could narrate
    # a destination the user recognises while the second factor authorises a
    # different one. There is no reach-out endpoint that wants credentials
    # embedded in its url (the whole point is that the credential comes from
    # the vault), so nothing legitimate is lost by refusing the shape entirely
    # rather than trying to render it unambiguously.
    if parsed.username is not None or parsed.password is not None or "@" in (parsed.netloc or ""):
        raise ReachoutError(
            "a reach-out url may not carry a username or password before the "
            "host (the part before an '@'). It reads as one destination and "
            "resolves to another, and the credential for this channel comes "
            "from your vault — never from the url.")

    host = (parsed.hostname or "").lower()
    if not host:
        raise ReachoutError(f"{url!r} names no host")
    try:
        port = parsed.port
    except ValueError:
        raise ReachoutError(f"{url!r} has a port this host cannot read")
    if port is None:
        port = _DEFAULT_PORTS.get(scheme)
    if port is None:
        raise ReachoutError(f"{url!r} names no port and {scheme!r} has no default")
    return f"{scheme}://{host}:{port}"


def _origin_display(origin: str) -> str:
    """The origin as the user should READ it — the default port dropped.

    What the step-up prompt and the channel row show. `https://api.test:443`
    is the same fact as `api.test` and the second one is what a person checks
    a domain against.
    """
    try:
        scheme, _, rest = origin.partition("://")
        host, _, port = rest.rpartition(":")
        if host and _DEFAULT_PORTS.get(scheme) == int(port):
            return host
        return rest or origin
    except Exception:
        return origin


# ── Rendering ─────────────────────────────────────────────────────────────

def render(template: str, ctx: dict[str, Any]) -> str:
    """Substitute the closed variable set. An unknown name cannot reach here —
    validate_action refused it at write time — so a miss renders empty rather
    than leaking the brace syntax into a text message."""
    def _sub(m):
        return str(ctx.get(m.group(1), "") or "")
    return _VAR_RX.sub(_sub, template or "")


def doorbell_link(link: str, host_origin: str) -> str:
    """The one link a reach-out carries — and it always opens THIS host.

    Inbox payloads carry links relative to the host (server/deep_link.app_link),
    which is right inside the host and useless in a mail client, so a relative
    link gains the host's own origin, and a link already on that origin is kept.
    ANYTHING ELSE becomes the host's own address: another site, a
    protocol-relative `//x` or a backslashed `/\\x`, `javascript:` or `data:`, a
    link with whitespace in it, or no link at all.

    WHY SO STRICT. The inbox is written by more than first-party code: the
    host's own inbox_send tool picks the message kind and the whole payload, so a
    turn steered by something the host read can write a "decision" whose link
    points anywhere — and the default channel mails it from the user's own
    address to the user. The subject is the host's words, as its chat is; the
    LINK is what a person clicks without reading, so it is the part made
    structural. (A published site is reached through the Sites app for the same
    reason, not linked directly.)

    With no origin (the handle could not be resolved) a relative link goes out
    as it is — degraded, never pointed anywhere else — and anything else goes
    out as nothing.
    """
    link = str(link or "").strip()
    origin = str(host_origin or "").rstrip("/")
    if any(c.isspace() or ord(c) < 32 for c in link):
        link = ""
    if link.startswith("/") and not link.startswith("//") and "\\" not in link:
        return origin + link if origin else link
    if origin and _on_origin(link, origin):
        return link
    return origin + "/" if origin else ""


def _on_origin(link: str, origin: str) -> bool:
    """Is `link` a url on exactly `origin` (scheme, host and port)? The
    origin's own scheme only: a Host served over https never links over
    http, and a home Host on http://….localhost links on http."""
    if "\\" in link:
        return False
    try:
        a, b = urlparse(link), urlparse(origin)
        a_port, b_port = a.port, b.port
    except ValueError:
        return False
    scheme = (b.scheme or "").lower()
    default = {"https": 443, "http": 80}.get(scheme)
    if default is None or (a.scheme or "").lower() != scheme:
        return False
    if a.username is not None or a.password is not None:
        return False
    return ((a.hostname or "").lower() == (b.hostname or "").lower() != ""
            and (a_port or default) == (b_port or default))


async def resolve_host_origin(entity_id: int) -> str:
    """This Host's browser address, its CSERIES_URL (server/home_origin.py),
    for the links reach-out messages carry. `entity_id` is kept for callers;
    a C-Series Host has one address."""
    from server.home_origin import HOME
    return HOME.origin


def message_vars(message: dict[str, Any], *, host_name: str = "",
                 user_email: str = "", channel_vars: dict | None = None,
                 host_origin: str = "") -> dict[str, Any]:
    """The closed variable set, resolved for one inbox row.

    `message` is an inbox_messages row (or the synthetic one `test` builds).
    Note what is NOT read: `body`. The payload contributes identifiers and a
    link and nothing else — and the link is made absolute against the host's
    own origin, and only ever opens the host (doorbell_link), because it is read
    outside the host.
    """
    payload = message.get("payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}
    out: dict[str, Any] = {
        "msg.subject": message.get("subject") or "",
        "msg.kind": message.get("message_kind") or "",
        "msg.link": doorbell_link(payload.get("link") or "", host_origin),
        "msg.project_id": payload.get("project_id"),
        "msg.decision_id": payload.get("decision_id"),
        "host.name": host_name,
        "user.email": user_email,
    }
    for k, v in (channel_vars or {}).items():
        out[f"user.{k}"] = v
    return out


def default_text(vars_: dict[str, Any]) -> str:
    """What a channel with no body of its own says: the subject and the link.

    Two lines, the design's own shape for the default channel, and the only
    two things a doorbell owes — what happened, and how to get back to it.
    """
    subject = str(vars_.get("msg.subject") or "Your Host needs you")
    link = str(vars_.get("msg.link") or "")
    return f"{subject}\n{link}".strip()


# ── Matching ──────────────────────────────────────────────────────────────

def matches(trigger: dict[str, Any], message: dict[str, Any]) -> bool:
    """Does this inbox row satisfy this trigger?

    All three predicates are ANDed and the two optional ones are skipped when
    NULL, so `match_kind` alone is the broad subscription and each extra
    column narrows it.
    """
    if (message.get("message_kind") or "") != (trigger.get("match_kind") or ""):
        return False

    payload = message.get("payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}

    want_event = trigger.get("match_event")
    if want_event and (payload.get("event") or "") != want_event:
        return False

    want_json = trigger.get("match_jsonb")
    if isinstance(want_json, str):
        try:
            want_json = json.loads(want_json)
        except Exception:
            want_json = None
    if want_json:
        return contains(payload, want_json)
    return True


def contains(haystack: Any, needle: Any) -> bool:
    """PostgreSQL `@>` containment, in Python.

    The column is JSONB and the design calls match_jsonb "an optional
    containment predicate on payload", so this is what it has to mean. The
    first cut compared top-level keys for equality, which is containment only
    by accident — it agrees on a flat payload and diverges the moment anyone
    writes a nested one, refusing to match
    `{"detect": {"level": "critical"}}` against `{"detect": {"level":
    "critical", "count": 3}}`. That divergence would have surfaced as a
    trigger that silently never fires, which is the failure mode this whole
    feature cannot afford.

    The semantics, following `@>`: an object contains an object when every key
    of the needle is present and its value is contained, recursively. An array
    contains an array when every needle element is contained by SOME haystack
    element. Scalars contain by equality.
    """
    if isinstance(needle, dict):
        if not isinstance(haystack, dict):
            return False
        return all(k in haystack and contains(haystack[k], v)
                   for k, v in needle.items())
    if isinstance(needle, list):
        if not isinstance(haystack, list):
            return False
        return all(any(contains(h, n) for h in haystack) for n in needle)
    return haystack == needle


def in_quiet_window(channel: dict[str, Any], now: datetime) -> bool:
    """Is `now` inside this channel's quiet window?

    NULL from/to = no window, which is the shipped default (decision 2). A
    window that wraps midnight (22:00→07:00) is the normal case, so it is
    handled rather than treated as invalid input.
    """
    q_from, q_to = channel.get("quiet_from"), channel.get("quiet_to")
    if not q_from or not q_to:
        return False
    tzname = channel.get("quiet_tz")
    local = now
    if tzname:
        try:
            from zoneinfo import ZoneInfo
            local = now.astimezone(ZoneInfo(tzname))
        except Exception:
            # An unknown zone must not silence a nudge forever; fall back to
            # the pod's clock and say so in the log.
            log.warning("reachout: unknown quiet_tz %r — using UTC", tzname)
    t: _time = local.timetz().replace(tzinfo=None)
    if q_from <= q_to:
        return q_from <= t < q_to
    return t >= q_from or t < q_to


# ── One fire ──────────────────────────────────────────────────────────────

async def fire(pool, entity_id: int, *, channel: dict[str, Any],
               message: dict[str, Any], trigger_id: int | None,
               now: datetime | None = None,
               host_name: str = "", user_email: str = "",
               host_origin: str = "") -> dict[str, Any]:
    """Attempt ONE reach-out and return the log row's {status, detail}.

    The order is the design's, and each step earns its place:

      1. INSERT the log row ON CONFLICT DO NOTHING. This is the idempotency
         gate, and it comes FIRST — before the quiet and cooldown checks —
         because winning the insert is what makes this tick the one that
         fires. A second tick over the same inbox row gets no row back and
         stops here.
      2. Quiet window  -> skipped_quiet.
      3. Cooldown      -> skipped_cooldown. Measured against the last SENT row
         on this channel, so the FIRST message of a burst always goes and the
         rest collapse into it.
      4. Render, then hand the rendered text to the transport.
      5. Write the outcome. Never raises: one failing channel never stops the
         loop, and there is no retry — the log row is already consumed, and a
         doorbell that rings an hour late is worse than one that did not ring.
    """
    now = now or datetime.now(timezone.utc)
    channel_id = channel.get("id")
    message_id = message.get("message_id")

    row_id = await pool.fetchval(
        """
        INSERT INTO reachout_log (entity_id, trigger_id, channel_id, message_id,
                                  fired_at, status, detail)
        VALUES ($1, $2, $3, $4::uuid, $5, 'pending', '{}'::jsonb)
        ON CONFLICT (trigger_id, message_id) DO NOTHING
        RETURNING id
        """,
        int(entity_id), trigger_id, channel_id, str(message_id), now,
    )
    if row_id is None:
        return {"status": None, "detail": {"already_fired": True}}

    async def _finish(status: str, detail: dict) -> dict:
        await pool.execute(
            "UPDATE reachout_log SET status=$2, detail=$3::jsonb WHERE id=$1",
            row_id, status, json.dumps(detail))
        return {"status": status, "detail": detail, "log_id": row_id}

    if in_quiet_window(channel, now):
        return await _finish("skipped_quiet", {"quiet_tz": channel.get("quiet_tz")})

    # `or` would be wrong here and was, until the test caught it: a channel
    # whose cool-down the user set to 0 — "always ring" — is falsy, and read
    # through `or` it silently became the 120-second default. 0 is a value.
    _cd = channel.get("cooldown_s")
    cooldown_s = DEFAULT_COOLDOWN_S if _cd is None else int(_cd)
    if cooldown_s > 0:
        last_sent = await pool.fetchval(
            "SELECT MAX(fired_at) FROM reachout_log "
            "WHERE channel_id=$1 AND status='sent'",
            channel_id)
        if last_sent is not None and (now - last_sent) < timedelta(seconds=cooldown_s):
            return await _finish("skipped_cooldown", {
                "cooldown_s": cooldown_s,
                "since_last_s": int((now - last_sent).total_seconds()),
            })

    action = channel.get("action") or {}
    if isinstance(action, str):
        try:
            action = json.loads(action)
        except Exception:
            action = {}
    atype = (action.get("type") or "").strip()
    transport = TRANSPORTS.get(atype)
    if transport is None:
        return await _finish("failed", {
            "error": UNBUILT_ACTIONS.get(atype)
            or f"no transport for action type {atype!r}"})

    cvars = action.get("vars") if isinstance(action.get("vars"), dict) else {}
    vars_ = message_vars(message, host_name=host_name, user_email=user_email,
                         channel_vars=cvars, host_origin=host_origin)
    ctx = {
        "pool": pool, "entity_id": int(entity_id), "action": action,
        "vars": vars_, "message_id": str(message_id),
        "text": default_text(vars_),
        "render": lambda s: render(s, vars_),
    }
    try:
        detail = await transport(ctx)
    except TransportUnavailable as e:
        return await _finish("failed", {"error": str(e), "transport": atype})
    except Exception as e:                      # noqa: BLE001 — never raise
        # A transport turns every fault it can name into a TransportUnavailable
        # sentence; reaching here means one it could not, so the row says it
        # was unexpected rather than presenting a raw exception as a reason.
        log.warning("reachout: channel %s (%s) failed: %s",
                    channel.get("name"), atype, e)
        return await _finish("failed", {
            "error": (f"the {atype or 'channel'} transport failed unexpectedly "
                      f"({type(e).__name__}): {str(e)[:300]}"),
            "transport": atype})
    if message.get("test"):
        detail = {**detail, "test": True}
    return await _finish("sent", detail)


# ── CRUD ──────────────────────────────────────────────────────────────────

def action_needs_stepup(action: Any) -> bool:
    """Does creating this channel WIDEN what a credential can reach?

    Exactly one shape does: `http_call`, which binds one of the host's sealed
    credentials to an outbound destination. `log` touches nothing outside the
    pod and `runbook` runs the host's own template through the pipeline that
    already governs it — neither carries a secret anywhere, so neither is a
    widen and neither should cost the user a code they will learn to type
    without reading.
    """
    return isinstance(action, dict) and (action.get("type") or "").strip() == "http_call"


def _parse_quiet(v, field: str) -> _time | None:
    """'HH:MM' (or 'HH:MM:SS', or a time) -> datetime.time; None stays None."""
    if v is None or v == "":
        return None
    if isinstance(v, _time):
        return v.replace(tzinfo=None)
    try:
        t = _time.fromisoformat(str(v).strip())
    except ValueError:
        raise ReachoutError(f"{field} must be a time like 22:00 (got {v!r})")
    if t.tzinfo is not None:
        # The column holds a wall-clock time; the zone is quiet_tz's job. An
        # offset here would be dropped silently, so it is refused instead.
        raise ReachoutError(f"{field} takes a time without an offset (like 22:00); "
                            "set the zone with quiet_tz")
    return t


async def add_channel(pool, entity_id: int, *, name: str, kind: str,
                      action: Any, cooldown_s: int | None = None,
                      quiet_from: str | None = None, quiet_to: str | None = None,
                      quiet_tz: str | None = None,
                      created_by: str = "host",
                      stepup_code: str | None = None) -> dict:
    """Build a channel. Binding a CREDENTIAL to a destination needs the user.

    THE WIDEN WALL. An `http_call` channel is a standing instruction to send a
    sealed credential to a named host, and everything downstream of creation is
    designed to run with nobody present — the dispatcher fires it, and `test`
    fires it on demand. So the moment the user is guaranteed to be there is
    creation, and that is where the second factor goes.

    It is the SAME verifier the NAC widen paths use — nac_stepup.verify_stepup
    (server/network/nac_stepup.py:180-190), reached the same way
    nac_commit.approve_policy reaches it (nac_commit.py:60-62). Not a new one:
    that function fails closed when no factor is enrolled, and BURNS the
    counter, so a replayed code is refused. Both properties are the wall here,
    and both would have had to be rebuilt (and could have been rebuilt wrong)
    in a second implementation.

    It is also what makes the unattended path structurally impossible rather
    than merely excluded: a TOTP code is time-based and comes off the user's
    authenticator, so a scheduled payload frozen at authoring time cannot carry
    one that will still verify when it fires.
    """
    if action_needs_stepup(action):
        # THE PROMPT NAMES THE DESTINATION, AND THE PLATFORM DERIVES THE NAME.
        #
        # A second factor authorises whatever the user believed they were
        # approving, so the sentence they read has to come from the same parse
        # that produces the pin — not from the host's narration of the url it
        # was handed. Until this existed the refusal said only "an outside
        # service", so the user typed a TOTP against a destination only the
        # host had described, and a url crafted to read as one host while
        # resolving to another would have been approved on the host's word.
        # (The url shape that does that is refused outright now too — see
        # _url_origin's userinfo check — but the prompt carrying the derived
        # destination is the part that does not depend on having enumerated
        # every such shape.)
        #
        # Parsed BEFORE the code is checked so a malformed or refused url
        # fails on its own terms and never burns one of the user's codes.
        try:
            _dest = _origin_display(_url_origin(
                (action.get("url") or "").strip(), require_https=True))
        except ReachoutError:
            raise
        from server.network import nac_stepup
        if not await nac_stepup.verify_stepup(pool, str(stepup_code or "")):
            raise ReachoutError(
                f"building a channel that sends the key {action.get('cred_name')!r} "
                f"to {_dest} needs your authenticator code. Read that "
                "destination back to the user in those words — it is the one "
                "the channel will be pinned to — and pass their code as "
                "stepup_code. (No second factor enrolled yet? Network app → "
                "enroll, then try again.)")
    name = (name or "").strip()
    if not name:
        raise ReachoutError("name is required")
    kind = (kind or "").strip()
    if kind not in CHANNEL_KINDS:
        raise ReachoutError(f"kind must be one of {list(CHANNEL_KINDS)} (got {kind!r})")
    if created_by not in CREATED_BY:
        raise ReachoutError(f"created_by must be one of {list(CREATED_BY)}")
    action = await validate_action(pool, entity_id, action)
    # Quiet hours bind as datetime.time: asyncpg refuses a str for `$n::time`,
    # and the tool is told to send "HH:MM", so every channel with a quiet
    # window failed to create.
    quiet_from, quiet_to = _parse_quiet(quiet_from, "quiet_from"), _parse_quiet(quiet_to, "quiet_to")
    if (quiet_from is None) != (quiet_to is None):
        raise ReachoutError("a quiet window needs both quiet_from and quiet_to")
    cd = DEFAULT_COOLDOWN_S if cooldown_s is None else int(cooldown_s)
    if cd < 0:
        raise ReachoutError("cooldown_s cannot be negative")

    row = await pool.fetchrow(
        """
        INSERT INTO reachout_channels
          (entity_id, name, kind, action, cooldown_s,
           quiet_from, quiet_to, quiet_tz, created_by)
        VALUES ($1, $2, $3, $4::jsonb, $5, $6::time, $7::time, $8, $9)
        RETURNING id, name, kind, cooldown_s, created_by, created_at
        """,
        int(entity_id), name[:200], kind, json.dumps(action), cd,
        quiet_from, quiet_to, quiet_tz, created_by,
    )
    out = dict(row)
    # WHAT WAS ACTUALLY PINNED, back to the caller. The host narrates this
    # result to the user, and without it the only account of where the channel
    # points is the url the host was handed — so the user's confirmation ("yes,
    # that's my provider") rested on the host's reading rather than on the
    # parse that will govern the fire. Derived, not echoed.
    if action.get("pinned_host"):
        out["pinned_host"] = action["pinned_host"]
        out["pinned_origin"] = action.get("pinned_origin")
    return out


async def sweep_interrupted(pool, entity_id: int,
                            now: datetime | None = None) -> int:
    """Write off `pending` rows a stopped pod left behind. Returns the count.

    Idempotent and cheap (one indexed UPDATE), so the dispatcher runs it on
    every tick. See PENDING_INTERRUPTED_AFTER_S for why the window is what it
    is.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=PENDING_INTERRUPTED_AFTER_S)
    rows = await pool.fetch(
        """
        UPDATE reachout_log
           SET status = 'failed',
               detail = detail || '{"interrupted": true, "error": "the host stopped between recording this reach-out and sending it; it was not retried"}'::jsonb
         WHERE entity_id = $1
           AND status = 'pending'
           AND fired_at < $2
        RETURNING id
        """,
        int(entity_id), cutoff)
    if rows:
        log.warning("reachout: wrote off %d interrupted attempt(s)", len(rows))
    return len(rows)


async def add_trigger(pool, entity_id: int, *, channel: str | int, label: str,
                      match_kind: str, match_event: str | None = None,
                      match_jsonb: dict | None = None,
                      created_by: str = "host") -> dict:
    label = (label or "").strip()
    if not label:
        raise ReachoutError("label is required — it is what the user reads in "
                            "the pane, so name the thing that happened")
    match_kind = (match_kind or "").strip()
    if not match_kind:
        raise ReachoutError("match_kind is required (an inbox message_kind, "
                            "e.g. system_notification or decision)")
    if created_by not in CREATED_BY:
        raise ReachoutError(f"created_by must be one of {list(CREATED_BY)}")

    ch = await _resolve_channel(pool, entity_id, channel)
    row = await pool.fetchrow(
        """
        INSERT INTO reachout_triggers
          (entity_id, channel_id, label, match_kind, match_event, match_jsonb, created_by)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
        RETURNING id, label, match_kind, match_event, created_by, created_at
        """,
        int(entity_id), ch["id"], label[:200], match_kind,
        (match_event or None),
        (json.dumps(match_jsonb) if match_jsonb else None),
        created_by,
    )
    out = dict(row)
    out["channel"] = ch["name"]
    if match_event in UNEMITTED_EVENTS:
        out["note"] = (f"{match_event} is a declared event that nothing emits "
                       "yet — the trigger is saved and will fire the moment "
                       "the management lane starts writing it.")
    return out


async def _resolve_channel(pool, entity_id: int, channel: str | int) -> dict:
    if isinstance(channel, int) or (isinstance(channel, str) and channel.isdigit()):
        row = await pool.fetchrow(
            "SELECT id, name, created_by FROM reachout_channels "
            "WHERE id=$1 AND entity_id=$2", int(channel), int(entity_id))
    else:
        row = await pool.fetchrow(
            "SELECT id, name, created_by FROM reachout_channels "
            "WHERE name=$1 AND entity_id=$2", str(channel).strip(), int(entity_id))
    if row is None:
        raise ReachoutError(f"no reach-out channel {channel!r} on this host")
    return dict(row)


def _not_the_hosts_to_remove(what: str, created_by: str | None) -> str:
    """The refusal for a host asking to remove a row it did not create."""
    whose = ("was set up when Gmail was connected"
             if created_by == "platform" else "was created by the user")
    return f"{what} {whose}, so it is the user's to remove, not the host's."


async def remove_channel(pool, entity_id: int, channel: str | int,
                         *, actor: str = "host") -> dict:
    """Delete a channel (its triggers cascade).

    The host may remove what IT created and nothing else. A row the user made
    is theirs, and so is the one the platform set up when they connected Gmail
    — the design says the USER may delete it, and a single host turn removing
    it would take the doorbell away without anyone deciding to. The refusal
    points at where they can do it rather than at a permission error. Design
    principle 4: host adds, user deletes, platform enforces.
    """
    ch = await _resolve_channel(pool, entity_id, channel)
    if actor == "host" and ch.get("created_by") != "host":
        raise ReachoutError(_not_the_hosts_to_remove(ch["name"], ch.get("created_by")))
    await pool.execute("DELETE FROM reachout_channels WHERE id=$1 AND entity_id=$2",
                       ch["id"], int(entity_id))
    return {"deleted_channel": ch["name"], "id": ch["id"]}


async def remove_trigger(pool, entity_id: int, trigger_id: int,
                         *, actor: str = "host") -> dict:
    row = await pool.fetchrow(
        "SELECT id, label, created_by FROM reachout_triggers "
        "WHERE id=$1 AND entity_id=$2", int(trigger_id), int(entity_id))
    if row is None:
        raise ReachoutError(f"no reach-out trigger with id {trigger_id} on this host")
    if actor == "host" and row["created_by"] != "host":
        raise ReachoutError(_not_the_hosts_to_remove(
            f"the trigger {row['label']!r}", row["created_by"]))
    await pool.execute("DELETE FROM reachout_triggers WHERE id=$1 AND entity_id=$2",
                       int(trigger_id), int(entity_id))
    return {"deleted_trigger": int(trigger_id), "label": row["label"]}


async def list_state(pool, entity_id: int, *, log_limit: int = 20, log_offset: int = 0) -> dict:
    """Channels, triggers and the recent log — the one read the tool and the
    pane share, so both always show the same thing."""
    channels = await pool.fetch(
        "SELECT id, name, kind, action, cooldown_s, quiet_from, quiet_to, "
        "quiet_tz, created_by, created_at, disabled_at "
        "FROM reachout_channels WHERE entity_id=$1 ORDER BY created_at",
        int(entity_id))
    triggers = await pool.fetch(
        "SELECT id, channel_id, label, match_kind, match_event, match_jsonb, "
        "created_by, created_at, disabled_at "
        "FROM reachout_triggers WHERE entity_id=$1 ORDER BY created_at",
        int(entity_id))
    recent = await pool.fetch(
        "SELECT id, trigger_id, channel_id, message_id, fired_at, status, detail "
        "FROM reachout_log WHERE entity_id=$1 ORDER BY fired_at DESC LIMIT $2 OFFSET $3",
        int(entity_id), int(log_limit), max(0, int(log_offset or 0)))

    def _chan(r):
        d = dict(r)
        a = d.get("action")
        if isinstance(a, str):
            try:
                a = json.loads(a)
            except Exception:
                a = {}
        # The vault NAME travels; there is nothing else to hide here, because
        # the row never held bytes in the first place.
        d["action"] = a
        d["enabled"] = d.pop("disabled_at", None) is None
        return _isoify(d)

    return {
        "channels": [_chan(r) for r in channels],
        "triggers": [_isoify(dict(r)) for r in triggers],
        "recent": [_isoify(dict(r)) for r in recent],
        # The log is paged: a full page may
        # have older entries behind it.
        "recent_offset": max(0, int(log_offset or 0)),
        "recent_next_offset": (max(0, int(log_offset or 0)) + len(recent))
                              if len(recent) == int(log_limit) else None,
        "known_events": list(KNOWN_EVENTS),
        "unemitted_events": list(UNEMITTED_EVENTS),
    }


def _isoify(d: dict) -> dict:
    for k, v in list(d.items()):
        if isinstance(v, datetime):
            d[k] = v.isoformat()
        elif isinstance(v, _time):
            d[k] = v.isoformat()
        elif hasattr(v, "hex") and k == "message_id":
            d[k] = str(v)
    return d


async def host_identity(pool, entity_id: int) -> tuple[str, str]:
    """(host name, the user's own email) for the closed variable set.

    Read from entity_registry — the host's own row, keyed on the entity id the
    same way server/routes/circles.py:143-159 reads the display name — rather
    than passed in by the caller, so a template cannot be pointed at somebody
    else by whoever happens to be calling.

    `email` on that row is nullable and UNVERIFIED as populated on a live host;
    an empty string is a fine answer here, because user.email is a template
    convenience and never a routing decision. Nothing addresses a message with
    it: the http_call channel addresses itself from its own action, and the
    Gmail channel pins the recipient to the connection's own
    address rather than to anything read here.
    """
    try:
        row = await pool.fetchrow(
            "SELECT display_name, email FROM entity_registry WHERE id=$1",
            int(entity_id))
    except Exception:
        return ("", "")
    if row is None:
        return ("", "")
    from server.host_display import display_name_or_host
    return (display_name_or_host(row["display_name"], entity_id), row["email"] or "")


async def test_fire(pool, entity_id: int, channel: str | int) -> dict:
    """Fire one channel once with a synthetic message.

    trigger_id is NULL, and PostgreSQL treats NULLs in a UNIQUE index as
    distinct — so a test never collides with a real fire or with a previous
    test, and `test` stays usable as the thing the host runs right after it
    builds a channel.
    """
    import uuid as _uuid
    ch = await _resolve_channel(pool, entity_id, channel)
    full = await pool.fetchrow(
        "SELECT id, name, kind, action, cooldown_s, quiet_from, quiet_to, "
        "quiet_tz, created_by, disabled_at FROM reachout_channels WHERE id=$1",
        ch["id"])
    row = dict(full)
    if row.get("disabled_at") is not None:
        raise ReachoutError(f"{row['name']} is switched off — enable it first")
    # A test bypasses the cooldown: the host runs it immediately after building
    # a channel, and being told "you nudged yourself 40 seconds ago" is not an
    # answer to "did it work".
    row["cooldown_s"] = 0
    host_name, user_email = await host_identity(pool, entity_id)
    synthetic = {
        "message_id": str(_uuid.uuid4()),
        "subject": "This is your Host — reach-out test",
        "message_kind": "system_notification",
        # The deep-link contract (server/deep_link.py): the test's link lands
        # where a real one does — in Circles, where the channels are managed.
        "payload": {"event": "reachout_test", "link": app_link("circles")},
        "test": True,
    }
    return await fire(pool, entity_id, channel=row, message=synthetic,
                      trigger_id=None, host_name=host_name, user_email=user_email,
                      host_origin=await resolve_host_origin(entity_id))
